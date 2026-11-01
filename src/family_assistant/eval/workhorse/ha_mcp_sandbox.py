"""Run the deployment's ha-mcp server locally against the sandbox's Home Assistant.

Production runs ha-mcp (github.com/homeassistant-ai/ha-mcp) as its own
service in front of Home Assistant, and the assistant reaches it as the
``homeassistant`` MCP server. The sandbox runs the same release, with the
same settings, pointed at the sandbox's ``hass``: tool search on, the
household's timezone, the MCP endpoint at ``/mcp``. Production puts an
nginx bearer-token proxy in front; locally ha-mcp listens on loopback only
and is reached directly, so the deployment entry's token is unused.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time as wall
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["HA_MCP_RELEASE", "HaMcpServer", "ha_mcp_server"]

# The release the deployment runs; kept in step with its manifest.
HA_MCP_RELEASE = "8.6.0"


@dataclass(frozen=True)
class HaMcpServer:
    mcp_url: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def ha_mcp_server(
    hass_url: str,
    hass_token: str,
    work_dir: Path,
    *,
    cache_dir: Path,
    timezone: str = "Australia/Sydney",
) -> Iterator[HaMcpServer]:
    """Run ha-mcp for the block, serving ``hass_url`` over streamable HTTP."""
    uvx = shutil.which("uvx")
    if uvx is None:
        raise RuntimeError("The ha-mcp sandbox needs uvx on PATH")
    work_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(work_dir),
        "UV_CACHE_DIR": str(cache_dir / "uv"),
        "HOMEASSISTANT_URL": hass_url,
        "HOMEASSISTANT_TOKEN": hass_token,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_SECRET_PATH": "/mcp",
        "ENABLE_TOOL_SEARCH": "true",
        "TZ": timezone,
    }
    process = subprocess.Popen(
        [uvx, "--from", f"ha-mcp=={HA_MCP_RELEASE}", "ha-mcp-web"],
        cwd=work_dir,
        env=environment,
    )
    try:
        deadline = wall.monotonic() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError("ha-mcp exited during startup")
            with (
                contextlib.suppress(OSError),
                socket.create_connection(("127.0.0.1", port), timeout=0.5),
            ):
                break
            if wall.monotonic() > deadline:
                raise RuntimeError("ha-mcp did not start listening in time")
            wall.sleep(0.25)
        yield HaMcpServer(mcp_url=f"http://127.0.0.1:{port}/mcp")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
