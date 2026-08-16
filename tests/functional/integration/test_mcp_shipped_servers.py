"""The MCP servers shipped in defaults.yaml must actually start.

A failed MCP server is nearly silent: `MCPToolsProvider` logs it, marks the
server `failed`, and the application serves on without those tools, so
`/health` stays green. The `time` server was configured as `uvx
mcp-server-time`, which cannot work: the MCP SDK spawns stdio servers with a
whitelisted environment (HOME, LOGNAME, PATH, SHELL, TERM, USER), so
`UV_TOOL_DIR` never reaches the child, `uvx` cannot see the tool environment the
image installs, and it re-resolves the package from PyPI at every startup —
eventually onto a release that dies on import against mcp 2.x.

These tests take the shipped configuration verbatim and connect to it for real.
`scripts/check_mcp_servers.py` does the same thing inside the built image during
the container smoke test, where the entry points come from `/uv/bin`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

from family_assistant.tools import MCPToolsProvider
from family_assistant.tools.mcp import MCP_SERVER_STATUS_CONNECTED, MCPServerStatus

if TYPE_CHECKING:
    from family_assistant.tools.types import MCPServerStdIOConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "defaults.yaml"

CONNECTION_TIMEOUT_SECONDS = 30


def _shipped_stdio_servers() -> dict[str, MCPServerStdIOConfig]:
    """The stdio entries of mcp_config.mcpServers, exactly as shipped."""
    defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    return {
        server_id: cast("MCPServerStdIOConfig", config)
        for server_id, config in defaults["mcp_config"]["mcpServers"].items()
        if config.get("transport", "stdio") == "stdio"
    }


def _command_of(server_id: str, config: MCPServerStdIOConfig) -> str:
    command = config.get("command")
    assert command is not None, f"stdio MCP server '{server_id}' has no command"
    return command


async def _connect(server_id: str, config: MCPServerStdIOConfig) -> MCPServerStatus:
    """Connect to one server through the real provider and return its status."""
    provider = MCPToolsProvider(
        mcp_server_configs={server_id: config},
        initialization_timeout_seconds=CONNECTION_TIMEOUT_SECONDS,
        # Keep the background retry loop out of the way; this is a one-shot check.
        health_check_interval_seconds=CONNECTION_TIMEOUT_SECONDS * 10,
    )
    try:
        await provider.initialize()
        return provider.get_server_statuses()[server_id]
    finally:
        await provider.close()


def test_shipped_time_server_command_is_on_path() -> None:
    """The configured command must resolve without any uv indirection."""
    command = _command_of("time", _shipped_stdio_servers()["time"])

    assert shutil.which(command) is not None, (
        f"defaults.yaml launches the 'time' MCP server as '{command}', which is not on PATH. "
        "The production image installs it into /uv/bin; a development environment gets it from "
        "the dev extra in .venv/bin."
    )


@pytest.mark.asyncio
async def test_shipped_time_server_reports_connected() -> None:
    config = _shipped_stdio_servers()["time"]

    status = await _connect("time", config)

    assert status["status"] == MCP_SERVER_STATUS_CONNECTED, (
        f"The shipped 'time' MCP server failed to start: {status}"
    )
    assert set(status["tools"]) == {"get_current_time", "convert_time"}


@pytest.mark.parametrize("server_id", sorted(_shipped_stdio_servers()))
def test_shipped_stdio_servers_are_not_launched_through_uvx(server_id: str) -> None:
    """`uvx`/`uv run` cannot resolve a pre-installed tool from an MCP child process.

    The MCP SDK passes the child only HOME, LOGNAME, PATH, SHELL, TERM and USER,
    so UV_TOOL_DIR and UV_CACHE_DIR are stripped and uv re-resolves the package
    from PyPI on every connection — inside the MCP initialization timeout, and
    landing on whatever versions resolve that day. Install the server in the
    image and invoke its entry point by name instead.
    """
    command = _command_of(server_id, _shipped_stdio_servers()[server_id])

    assert Path(command).name not in {"uvx", "uv"}, (
        f"MCP server '{server_id}' is launched through "
        f"'{command}', which re-resolves the package from PyPI at every startup."
    )
