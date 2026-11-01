"""Run the deployment's code-execution MCP server locally, confined.

Production runs the server as its own container; the sandbox runs the same
source (``main.py`` and ``mcp_server.py`` from the deployment's repository)
as a local process, so the tools, their descriptions and their behaviour are
the deployment's. What the container's isolation provided, a macOS
``sandbox-exec`` profile provides here: the process may write only its own
working directory, its package cache and temporary space, and may not read
the credential stores or the source trees on the host. Network stays open,
as in production, for package installs.

The image's extra tooling (Nix, the image tools) is whatever the host has;
a case that needs a tool the host lacks fails in the trial, visibly.
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
    from collections.abc import Iterable, Iterator
    from pathlib import Path

__all__ = ["CodeExecutionServer", "code_execution_server", "sandbox_profile"]

_SERVER_FILES = ("main.py", "mcp_server.py")
_REQUIREMENTS = (
    "fastapi>=0.109.0",
    "uvicorn>=0.27.0",
    "pydantic>=2.0.0",
    "mcp==1.30.0",
)


@dataclass(frozen=True)
class CodeExecutionServer:
    sse_url: str
    workspace_base: Path


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(
    *,
    writable: Iterable[Path],
    unreadable: Iterable[Path],
    readable_within_unreadable: Iterable[Path],
) -> str:
    """A ``sandbox-exec`` profile: writes only to ``writable``, reads not ``unreadable``.

    Later rules win, so the read exceptions (the interpreter's install, say)
    come after the read denials they carve out of.
    """
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (subpath "/private/tmp") (subpath "/private/var/folders")'
        ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
    ]
    lines += [f"(allow file-write* (subpath {_quote(path)}))" for path in writable]
    lines += [f"(deny file-read* (subpath {_quote(path)}))" for path in unreadable]
    lines += [
        f"(allow file-read* (subpath {_quote(path)}))"
        for path in readable_within_unreadable
    ]
    return "\n".join(lines) + "\n"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(
    port: int, process: subprocess.Popen[bytes], deadline: float
) -> None:
    while wall.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("The code-execution server exited during startup")
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.5),
        ):
            return
        wall.sleep(0.25)
    raise RuntimeError("The code-execution server did not start listening in time")


@contextlib.contextmanager
def code_execution_server(
    source_dir: Path,
    work_dir: Path,
    *,
    cache_dir: Path,
    unreadable: Iterable[Path] = (),
    readable_within_unreadable: Iterable[Path] = (),
) -> Iterator[CodeExecutionServer]:
    """Run the server from ``source_dir`` for the block, confined to ``work_dir``.

    ``work_dir`` and ``cache_dir`` must be on a local filesystem. ``cache_dir``
    holds uv's cache and is worth sharing across trials: the server's own
    dependencies and whatever a trial installs are fetched once.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("The code-execution sandbox needs uv on PATH")
    server_dir = work_dir / "server"
    workspaces = work_dir / "workspaces"
    server_dir.mkdir(parents=True, exist_ok=True)
    workspaces.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in _SERVER_FILES:
        shutil.copy2(source_dir / name, server_dir / name)
    profile = work_dir / "sandbox.sb"
    profile.write_text(
        sandbox_profile(
            writable=[work_dir, cache_dir],
            unreadable=unreadable,
            readable_within_unreadable=readable_within_unreadable,
        ),
        encoding="utf-8",
    )
    port = _free_port()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(work_dir),
        "WORKSPACE_BASE": str(workspaces),
        "UV_CACHE_DIR": str(cache_dir / "uv"),
        "UV_TOOL_DIR": str(cache_dir / "uv-tools"),
        "npm_config_cache": str(cache_dir / "npm"),
        "TMPDIR": str(work_dir / "tmp"),
    }
    (work_dir / "tmp").mkdir(exist_ok=True)
    command = [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        uv,
        "run",
        "--no-project",
        *(
            argument
            for requirement in _REQUIREMENTS
            for argument in ("--with", requirement)
        ),
        "python",
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(command, cwd=server_dir, env=environment)
    try:
        _wait_for_port(port, process, wall.monotonic() + 180)
        yield CodeExecutionServer(
            sse_url=f"http://127.0.0.1:{port}/mcp/sse", workspace_base=workspaces
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
