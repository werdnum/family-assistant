"""Tests for the MCP server startup check used by the container smoke test.

The check is only worth running if it can fail, so the central case here is a
server that dies the way the real one did — the entry point raising on import,
before it ever speaks MCP — and the check reporting it rather than shrugging.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from family_assistant.tools.mcp import MCP_SERVER_STATUS_CONNECTED, MCPServerStatus

if TYPE_CHECKING:
    from types import ModuleType

    from family_assistant.tools import MCPServerConfig
    from family_assistant.tools.types import MCPServerStdIOConfig

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_mcp_servers.py"


def _script() -> ModuleType:
    """Import the check as a module; it is a script, not a package member."""
    spec = importlib.util.spec_from_file_location("check_mcp_servers", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _server_that_fails_on_import() -> MCPServerStdIOConfig:
    """A stdio server whose process dies before the MCP handshake, as ours did."""
    return {
        "command": sys.executable,
        "args": ["-c", "raise ImportError('cannot import name McpError')"],
    }


def _status(status: str, tool_count: int) -> MCPServerStatus:
    return MCPServerStatus(
        status=status,
        transport="stdio",
        command="mcp-server-time",
        args=[],
        url=None,
        session_active=status == MCP_SERVER_STATUS_CONNECTED,
        tool_count=tool_count,
        tools=[f"tool_{index}" for index in range(tool_count)],
        reconnect_attempts=0,
        next_reconnect_in_seconds=None,
    )


@pytest.mark.asyncio
async def test_server_that_dies_on_startup_is_reported_as_failed() -> None:
    script = _script()

    statuses = await script.check_servers(
        {"time": _server_that_fails_on_import()}, timeout_seconds=30
    )

    assert script.failure_reasons(statuses) == {
        "time": "status is 'failed', expected 'connected'"
    }


def test_a_connected_server_with_no_tools_is_a_failure() -> None:
    script = _script()

    reasons = script.failure_reasons({
        "time": _status(MCP_SERVER_STATUS_CONNECTED, tool_count=0)
    })

    assert reasons == {"time": "connected but exposed no tools"}


def test_a_connected_server_with_tools_passes() -> None:
    script = _script()

    reasons = script.failure_reasons({
        "time": _status(MCP_SERVER_STATUS_CONNECTED, tool_count=2)
    })

    assert reasons == {}


def test_naming_an_unconfigured_server_is_an_error() -> None:
    """Silently checking nothing would report success for a server that is gone."""
    script = _script()
    configured: dict[str, MCPServerConfig] = {"time": {"command": "mcp-server-time"}}

    with pytest.raises(script.CheckError, match="nosuchserver"):
        script.select_server_configs(configured, ["nosuchserver"])


def test_no_named_servers_selects_everything_configured() -> None:
    script = _script()
    configured: dict[str, MCPServerConfig] = {
        "time": {"command": "mcp-server-time"},
        "brave": {"command": "deno"},
    }

    assert script.select_server_configs(configured, []) == configured
