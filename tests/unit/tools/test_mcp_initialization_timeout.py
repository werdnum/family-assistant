"""Unit tests for MCP startup when some servers miss the initialization timeout."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import ListToolsResult, Tool

from family_assistant.tools.mcp import (
    MCP_SERVER_STATUS_CANCELLED,
    MCP_SERVER_STATUS_CONNECTED,
    MCPToolsProvider,
)

if TYPE_CHECKING:
    from mcp import ClientSession

    from family_assistant.tools.metadata import ToolDescriptor
    from family_assistant.tools.types import MCPServerConfig, ToolDefinition

FAST = "fast-server"
SLOW = "slow-server"


def _session() -> ClientSession:
    return cast(
        "ClientSession",
        SimpleNamespace(
            send_ping=AsyncMock(return_value=None),
            list_tools=AsyncMock(return_value=ListToolsResult(tools=[])),
        ),
    )


def _discovered(
    provider: MCPToolsProvider, server_id: str, tool_name: str
) -> tuple[ClientSession, list[ToolDefinition], list[ToolDescriptor], dict[str, str]]:
    tools = [
        Tool(
            name=tool_name,
            description="does a thing",
            inputSchema={"type": "object", "properties": {}},
        )
    ]
    definitions = provider._format_mcp_definitions_to_dicts(tools, server_id)
    descriptors = provider._build_mcp_descriptors(
        server_id=server_id, definitions=definitions, discovered_tools=tools
    )
    return _session(), definitions, descriptors, {tool_name: server_id}


@pytest.mark.asyncio
async def test_timeout_keeps_finished_servers_and_retries_the_hung_one() -> None:
    """A server that hangs past the startup timeout does not discard the others."""
    configs: dict[str, MCPServerConfig] = {
        FAST: {"transport": "stdio", "command": "echo"},
        SLOW: {"transport": "stdio", "command": "echo"},
    }
    provider = MCPToolsProvider(configs, initialization_timeout_seconds=1)
    slow_hangs = True
    never_set = asyncio.Event()

    async def fake_connect(
        server_id: str, server_conf: MCPServerConfig
    ) -> tuple[
        ClientSession | None, list[ToolDefinition], list[ToolDescriptor], dict[str, str]
    ]:
        del server_conf
        # Mirrors the real method, which marks a server connected before
        # listing its tools.
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        if server_id == SLOW and slow_hangs:
            await never_set.wait()
        return _discovered(provider, server_id, f"{server_id}_tool")

    provider._connect_and_discover_mcp = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_connect
    )
    provider._close_server_connections = AsyncMock()  # type: ignore[method-assign]

    try:
        await provider.initialize()

        tool_names = {
            definition["function"]["name"]
            for definition in await provider.get_tool_definitions()
        }
        assert tool_names == {f"{FAST}_tool"}
        assert set(provider._sessions) == {FAST}
        assert provider._server_statuses[FAST] == MCP_SERVER_STATUS_CONNECTED
        assert provider._server_statuses[SLOW] == MCP_SERVER_STATUS_CANCELLED

        slow_hangs = False
        await provider._retry_disconnected_servers([SLOW])

        tool_names = {
            definition["function"]["name"]
            for definition in await provider.get_tool_definitions()
        }
        assert tool_names == {f"{FAST}_tool", f"{SLOW}_tool"}
        assert provider._server_statuses[SLOW] == MCP_SERVER_STATUS_CONNECTED
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_cancelling_initialize_waits_for_connection_cleanup() -> None:
    """Cancelling startup does not leave connection tasks running behind it."""
    configs: dict[str, MCPServerConfig] = {
        SLOW: {"transport": "stdio", "command": "echo"},
    }
    provider = MCPToolsProvider(configs, initialization_timeout_seconds=60)
    connecting = asyncio.Event()
    cleaned_up = False

    async def fake_connect(
        server_id: str, server_conf: MCPServerConfig
    ) -> tuple[
        ClientSession | None, list[ToolDefinition], list[ToolDescriptor], dict[str, str]
    ]:
        nonlocal cleaned_up
        del server_id, server_conf
        connecting.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Cleanup that spans several loop iterations, like closing a
            # transport, so the test sees whether initialize() waited for it.
            for _ in range(5):
                # ast-grep-ignore: no-asyncio-sleep-in-tests - yields the loop, no wait
                await asyncio.sleep(0)
            cleaned_up = True
        raise AssertionError("unreachable")

    provider._connect_and_discover_mcp = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_connect
    )
    init_task = asyncio.create_task(provider.initialize())
    await connecting.wait()

    init_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await init_task

    assert cleaned_up
