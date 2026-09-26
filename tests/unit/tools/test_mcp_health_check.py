"""Unit tests for MCP health checks and tool refresh pacing."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, ListToolsResult, Tool

from family_assistant.config_models import ToolsConfig
from family_assistant.tools import MCPServerConfig, MCPToolsProvider
from family_assistant.tools.mcp import (
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DEFAULT_TOOL_REFRESH_INTERVAL_SECONDS,
    MCP_SERVER_STATUS_CONNECTED,
    MCP_SERVER_STATUS_FAILED,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp import ClientSession

SERVER_ID = "test-server"


def _tool(name: str, description: str = "does a thing") -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
    )


def _session_with_ping(
    tools: Sequence[Tool] = (),
    *,
    ping_side_effect: Exception | None = None,
    list_tools_side_effect: Exception | None = None,
) -> tuple[ClientSession, AsyncMock, AsyncMock]:
    send_ping = AsyncMock(return_value=None, side_effect=ping_side_effect)
    list_tools = AsyncMock(
        return_value=ListToolsResult(tools=list(tools)),
        side_effect=list_tools_side_effect,
    )
    session = cast(
        "ClientSession",
        SimpleNamespace(send_ping=send_ping, list_tools=list_tools),
    )
    return session, send_ping, list_tools


def _provider(
    server_id: str = SERVER_ID,
    *,
    tool_refresh_interval_seconds: float | None = DEFAULT_TOOL_REFRESH_INTERVAL_SECONDS,
) -> MCPToolsProvider:
    configs: dict[str, MCPServerConfig] = {
        server_id: {"transport": "stdio", "command": "echo"}
    }
    provider = MCPToolsProvider(
        configs,
        tool_refresh_interval_seconds=tool_refresh_interval_seconds,
    )
    provider._initialized = True
    provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
    return provider


def _register(
    provider: MCPToolsProvider, server_id: str, tools: Sequence[Tool]
) -> None:
    definitions = provider._format_mcp_definitions_to_dicts(list(tools), server_id)
    provider._register_server_tools(
        server_id,
        definitions,
        provider._build_mcp_descriptors(
            server_id=server_id,
            definitions=definitions,
            discovered_tools=list(tools),
        ),
    )


@pytest.mark.asyncio
async def test_health_check_prefers_ping_and_does_not_call_list_tools() -> None:
    """When a session supports ping, health check uses send_ping, not list_tools."""
    provider = _provider()
    session, send_ping, list_tools = _session_with_ping([_tool("t1")])
    provider._sessions[SERVER_ID] = session
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic()

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert list_tools.await_count == 0
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED


@pytest.mark.asyncio
async def test_health_check_falls_back_to_list_tools_if_send_ping_missing() -> None:
    """Sessions or mocks lacking send_ping fall back to list_tools for health check."""
    provider = _provider()
    list_tools = AsyncMock(return_value=ListToolsResult(tools=[]))
    provider._sessions[SERVER_ID] = cast(
        "ClientSession", SimpleNamespace(list_tools=list_tools)
    )
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic()

    await provider._run_health_checks()

    assert list_tools.await_count == 1
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED


@pytest.mark.asyncio
async def test_health_check_treats_mcperror_on_ping_as_alive() -> None:
    """An McpError (e.g. MethodNotFound) confirms the JSON-RPC channel is active."""
    mcp_err = McpError(ErrorData(code=-32601, message="Method not found"))
    provider = _provider()
    session, send_ping, list_tools = _session_with_ping(ping_side_effect=mcp_err)
    provider._sessions[SERVER_ID] = session
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic()

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert list_tools.await_count == 0
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED


@pytest.mark.asyncio
async def test_health_check_drops_session_on_ping_connection_error() -> None:
    """A transport/connection failure during ping marks server failed and tears down."""
    provider = _provider()
    session, send_ping, _ = _session_with_ping(
        ping_side_effect=BrokenPipeError("Connection reset by peer")
    )
    provider._sessions[SERVER_ID] = session

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_FAILED
    assert SERVER_ID not in provider._sessions


@pytest.mark.asyncio
async def test_health_check_tolerates_timeout() -> None:
    """A timeout during ping does not immediately drop the session."""
    provider = _provider()
    session, send_ping, _ = _session_with_ping(ping_side_effect=TimeoutError())
    provider._sessions[SERVER_ID] = session

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert provider._server_statuses[SERVER_ID] == MCP_SERVER_STATUS_CONNECTED
    assert SERVER_ID in provider._sessions


@pytest.mark.asyncio
async def test_health_check_refreshes_tools_when_interval_elapsed() -> None:
    """Tool definitions are refreshed when tool_refresh_interval_seconds has elapsed."""
    provider = _provider(tool_refresh_interval_seconds=1800.0)
    _register(provider, SERVER_ID, [_tool("initial_tool")])
    session, send_ping, list_tools = _session_with_ping([_tool("updated_tool")])
    provider._sessions[SERVER_ID] = session
    # Simulate that 1801 seconds have passed since last refresh
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic() - 1801.0

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert list_tools.await_count == 1
    assert {d.name for d in provider._descriptors} == {"updated_tool"}


@pytest.mark.asyncio
async def test_health_check_skips_tool_refresh_when_interval_not_elapsed() -> None:
    """Tool definitions are not polled if tool_refresh_interval_seconds has not elapsed."""
    provider = _provider(tool_refresh_interval_seconds=1800.0)
    _register(provider, SERVER_ID, [_tool("initial_tool")])
    session, send_ping, list_tools = _session_with_ping([_tool("ignored_tool")])
    provider._sessions[SERVER_ID] = session
    # Only 30 seconds have passed since last refresh
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic() - 30.0

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert list_tools.await_count == 0
    assert {d.name for d in provider._descriptors} == {"initial_tool"}


@pytest.mark.asyncio
async def test_health_check_never_refreshes_tools_when_interval_is_none() -> None:
    """Setting tool_refresh_interval_seconds=None disables periodic tool listing."""
    provider = _provider(tool_refresh_interval_seconds=None)
    _register(provider, SERVER_ID, [_tool("initial_tool")])
    session, send_ping, list_tools = _session_with_ping([_tool("ignored_tool")])
    provider._sessions[SERVER_ID] = session
    # Even if very long time has passed
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic() - 999999.0

    await provider._run_health_checks()

    assert send_ping.await_count == 1
    assert list_tools.await_count == 0
    assert {d.name for d in provider._descriptors} == {"initial_tool"}


@pytest.mark.asyncio
async def test_on_demand_refresh_server_tools() -> None:
    """Calling refresh_server_tools fetches tools immediately regardless of interval."""
    provider = _provider(tool_refresh_interval_seconds=1800.0)
    _register(provider, SERVER_ID, [_tool("initial_tool")])
    session, _, list_tools = _session_with_ping([_tool("on_demand_tool")])
    provider._sessions[SERVER_ID] = session
    provider._last_tool_refresh_at[SERVER_ID] = time.monotonic()  # Just refreshed

    success = await provider.refresh_server_tools(SERVER_ID)

    assert success is True
    assert list_tools.await_count == 1
    assert {d.name for d in provider._descriptors} == {"on_demand_tool"}


@pytest.mark.asyncio
async def test_on_demand_refresh_fails_for_unknown_server() -> None:
    """refresh_server_tools returns False if the server has no active session."""
    provider = _provider()
    success = await provider.refresh_server_tools("unknown_server")
    assert success is False


def test_tools_config_defaults() -> None:
    """Verify ToolsConfig default values for MCP polling and health check intervals."""
    config = ToolsConfig()
    assert config.mcp_initialization_timeout_seconds == 60
    assert (
        config.mcp_health_check_interval_seconds
        == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    )
    assert (
        config.mcp_tool_refresh_interval_seconds
        == DEFAULT_TOOL_REFRESH_INTERVAL_SECONDS
    )
