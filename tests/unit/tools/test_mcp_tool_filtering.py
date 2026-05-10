"""Unit tests for MCP tool filtering by server ID."""

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool, ToolAnnotations

from family_assistant.tools import (
    FilteredToolsProvider,
    MCPServerConfig,
    MCPToolsProvider,
)
from family_assistant.tools.mcp import (
    MCP_SERVER_STATUS_CANCELLED,
    MCP_SERVER_STATUS_CONNECTED,
    MCP_SERVER_STATUS_FAILED,
)
from family_assistant.tools.metadata import ToolTag, build_tool_descriptor
from family_assistant.tools.types import ToolDefinition

if TYPE_CHECKING:
    from mcp import ClientSession


@pytest.mark.asyncio
async def test_mcp_provider_exposes_tool_to_server_mapping() -> None:
    """Test that MCPToolsProvider exposes tool-to-server mapping."""
    # Create MCPToolsProvider with mock config
    mcp_configs: dict[str, MCPServerConfig] = {
        "server1": {"transport": "stdio", "command": "echo"},
        "server2": {"transport": "stdio", "command": "echo"},
    }

    provider = MCPToolsProvider(mcp_configs)

    # The method should exist and return a dict
    mapping = provider.get_tool_to_server_mapping()
    assert isinstance(mapping, dict)

    # Initially empty before initialization
    assert len(mapping) == 0


@pytest.mark.asyncio
async def test_get_server_statuses_returns_per_server_diagnostics() -> None:
    """get_server_statuses produces a snapshot suitable for the engineer profile.

    Includes status, transport, command/url, session_active, and the tool
    list for each configured server. Tokens must not leak into the snapshot.
    """
    mcp_configs: dict[str, MCPServerConfig] = {
        "code-execution": {"transport": "stdio", "command": "code-exec"},
        "remote-tools": {
            "transport": "sse",
            "url": "https://example.com/mcp",
            "token": "$REMOTE_TOKEN",
        },
    }

    provider = MCPToolsProvider(mcp_configs)
    # Simulate two states: code-execution connected with two tools, remote-tools failed.
    provider._server_statuses["code-execution"] = MCP_SERVER_STATUS_CONNECTED
    provider._server_statuses["remote-tools"] = MCP_SERVER_STATUS_FAILED
    provider._tool_map = {
        "create_workspace": "code-execution",
        "execute_shell": "code-execution",
    }
    provider._sessions = {"code-execution": cast("ClientSession", object())}

    statuses = provider.get_server_statuses()

    assert set(statuses.keys()) == {"code-execution", "remote-tools"}

    code_exec = statuses["code-execution"]
    assert code_exec["status"] == MCP_SERVER_STATUS_CONNECTED
    assert code_exec["transport"] == "stdio"
    assert code_exec["command"] == "code-exec"
    assert code_exec["url"] is None
    assert code_exec["session_active"] is True
    assert code_exec["tool_count"] == 2
    assert code_exec["tools"] == ["create_workspace", "execute_shell"]

    remote = statuses["remote-tools"]
    assert remote["status"] == MCP_SERVER_STATUS_FAILED
    assert remote["transport"] == "sse"
    assert remote["url"] == "https://example.com/mcp"
    assert remote["session_active"] is False
    assert remote["tool_count"] == 0
    assert remote["tools"] == []
    assert "token" not in remote


@pytest.mark.asyncio
async def test_reconnect_server_unknown_id_raises_key_error() -> None:
    """reconnect_server raises KeyError for unknown server ids so the
    engineer-profile tool can surface a clear error instead of silently
    no-oping."""
    provider = MCPToolsProvider({
        "server1": {"transport": "stdio", "command": "echo"},
    })
    with pytest.raises(KeyError):
        await provider.reconnect_server("does-not-exist")


@pytest.mark.asyncio
async def test_filtered_tools_provider_with_mcp_filtering() -> None:
    """Test that FilteredToolsProvider correctly filters tools including MCP tools."""

    # Create mock tool definitions
    tool_defs = [
        {"type": "function", "function": {"name": "local_tool_1"}},
        {"type": "function", "function": {"name": "local_tool_2"}},
        {"type": "function", "function": {"name": "mcp_tool_1"}},
        {"type": "function", "function": {"name": "mcp_tool_2"}},
        {"type": "function", "function": {"name": "mcp_tool_3"}},
    ]

    # Create a mock composite provider
    mock_provider = AsyncMock()
    mock_provider.get_tool_definitions.return_value = tool_defs

    # Test 1: Filter with specific allowed tools
    allowed_tools = {"local_tool_1", "mcp_tool_1", "mcp_tool_3"}
    filtered_provider = FilteredToolsProvider(mock_provider, allowed_tools)

    filtered_defs = await filtered_provider.get_tool_definitions()
    filtered_names = {d["function"]["name"] for d in filtered_defs}

    assert filtered_names == allowed_tools
    assert len(filtered_defs) == 3

    # Test 2: No filtering (None means all tools)
    unfiltered_provider = FilteredToolsProvider(mock_provider, None)
    unfiltered_defs = await unfiltered_provider.get_tool_definitions()

    assert len(unfiltered_defs) == len(tool_defs)
    assert unfiltered_defs == tool_defs


@pytest.mark.asyncio
async def test_profile_builds_correct_tool_set_with_mcp_servers() -> None:
    """Test that profile configuration correctly builds tool set including MCP tools from enabled servers."""

    # Simulate the logic from assistant.py

    # Simulate MCP tool-to-server mapping
    mcp_tool_to_server = {
        "browse_url": "browser",
        "search_web": "browser",
        "get_time": "time",
        "set_timer": "time",
        "run_python": "python",
    }

    # Profile configuration
    enable_local_tools = ["add_note", "search_notes"]  # Not delete_note
    enable_mcp_server_ids = ["browser", "python"]  # Not time server

    # Build the complete set of allowed tools (simulating assistant.py logic)
    enabled_local_tool_names = set(enable_local_tools)
    all_enabled_tool_names = enabled_local_tool_names.copy()

    # Add MCP tools from enabled servers
    for tool_name, server_id in mcp_tool_to_server.items():
        if server_id in enable_mcp_server_ids:
            all_enabled_tool_names.add(tool_name)

    # Verify the result
    expected_tools = {
        # Local tools
        "add_note",
        "search_notes",
        # MCP tools from browser server
        "browse_url",
        "search_web",
        # MCP tools from python server
        "run_python",
        # NOT included: delete_note (local), get_time, set_timer (from time server)
    }

    assert all_enabled_tool_names == expected_tools

    # Verify excluded tools are not present
    assert "delete_note" not in all_enabled_tool_names
    assert "get_time" not in all_enabled_tool_names
    assert "set_timer" not in all_enabled_tool_names


@pytest.mark.asyncio
async def test_mcp_provider_builds_descriptors_from_configured_metadata() -> None:
    """Configured MCP metadata should override annotation-derived tags."""
    provider = MCPToolsProvider({
        "browser": {
            "transport": "stdio",
            "command": "echo",
            "tool_metadata": {
                "search_web": ["browser", "output_untrusted"],
                "*": ["read_only", "output_trusted"],
            },
        }
    })
    discovered_tool = Tool(
        name="search_web",
        description="Search the web",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=False,
        ),
    )
    definition: ToolDefinition = {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    descriptors = provider._build_mcp_descriptors(
        server_id="browser",
        definitions=[definition],
        discovered_tools=[discovered_tool],
    )

    assert len(descriptors) == 1
    assert descriptors[0].origin == "mcp"
    assert descriptors[0].mcp_server_id == "browser"
    assert descriptors[0].tags == {ToolTag.BROWSER, ToolTag.OUTPUT_UNTRUSTED}


@pytest.mark.asyncio
async def test_mcp_provider_matches_descriptors_by_tool_name_not_position() -> None:
    """Descriptor annotation lookup should survive sanitized-tool omissions."""
    provider = MCPToolsProvider({
        "browser": {
            "transport": "stdio",
            "command": "echo",
        }
    })
    definition: ToolDefinition = {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    skipped_tool = Tool(
        name="broken_tool",
        description="Broken",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(
            readOnlyHint=True,
            openWorldHint=False,
        ),
    )
    matched_tool = Tool(
        name="search_web",
        description="Search the web",
        inputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=True,
        ),
    )

    descriptors = provider._build_mcp_descriptors(
        server_id="browser",
        definitions=[definition],
        discovered_tools=[skipped_tool, matched_tool],
    )

    assert len(descriptors) == 1
    assert descriptors[0].name == "search_web"
    assert descriptors[0].tags == {
        ToolTag.DESTRUCTIVE,
        ToolTag.OUTPUT_UNTRUSTED,
    }


@pytest.mark.asyncio
async def test_mcp_reconnect_preserves_existing_duplicate_tool_mapping() -> None:
    """Reconnect should not overwrite a tool already owned by another server."""
    provider = MCPToolsProvider({
        "server1": {"transport": "stdio", "command": "echo"},
        "server2": {"transport": "stdio", "command": "echo"},
    })
    existing_definition: ToolDefinition = {
        "type": "function",
        "function": {
            "name": "shared_tool",
            "description": "Shared tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    provider._tool_map = {"shared_tool": "server2"}
    provider._definitions = [existing_definition]
    provider._descriptors = [
        build_tool_descriptor(
            existing_definition,
            frozenset({ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED}),
            origin="mcp",
            mcp_server_id="server2",
        )
    ]

    async def fake_connect_and_discover_mcp(
        server_id: str,
        server_conf: MCPServerConfig,
    ) -> tuple[object, list[ToolDefinition], list, dict[str, str]]:
        assert server_id == "server1"
        assert server_conf.get("command") == "echo"
        return (
            object(),
            [existing_definition],
            [
                build_tool_descriptor(
                    existing_definition,
                    frozenset({ToolTag.DESTRUCTIVE, ToolTag.OUTPUT_UNTRUSTED}),
                    origin="mcp",
                    mcp_server_id="server1",
                )
            ],
            {"shared_tool": "server1"},
        )

    provider._connect_and_discover_mcp = fake_connect_and_discover_mcp  # type: ignore[method-assign]
    provider._close_server_connections = AsyncMock()
    provider._sessions = cast(
        "dict[str, ClientSession]", {"server1": SimpleNamespace()}
    )
    provider._connection_contexts = {"server1": AsyncMock()}

    reconnected = await provider._reconnect_server("server1")

    assert reconnected is True
    assert provider._tool_map["shared_tool"] == "server2"
    assert len(provider._definitions) == 1
    assert provider._descriptors[0].mcp_server_id == "server2"


@pytest.mark.asyncio
async def test_health_check_retries_failed_and_cancelled_servers() -> None:
    """Health check loop retries servers that failed or timed out during init."""
    all_retried = asyncio.Event()
    retried_servers: set[str] = set()

    provider = MCPToolsProvider(
        {
            "healthy": {"transport": "stdio", "command": "echo"},
            "failed_server": {"transport": "stdio", "command": "echo"},
            "cancelled_server": {"transport": "stdio", "command": "echo"},
        },
        health_check_interval_seconds=0,
    )

    async def fake_connect(
        server_id: str, server_conf: MCPServerConfig
    ) -> tuple[object | None, list[ToolDefinition], list, dict[str, str]]:
        if server_id == "healthy":
            session = SimpleNamespace(list_tools=AsyncMock())
            provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
            return session, [], [], {}
        # On first call (init), fail. On retry via _reconnect_server, succeed.
        if server_id not in retried_servers:
            provider._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            return None, [], [], {}
        session = SimpleNamespace(list_tools=AsyncMock())
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        return session, [], [], {}

    original_reconnect = provider._reconnect_server

    async def tracking_reconnect(server_id: str) -> bool:
        retried_servers.add(server_id)
        result = await original_reconnect(server_id)
        if retried_servers >= {"failed_server", "cancelled_server"}:
            all_retried.set()
        return result

    provider._connect_and_discover_mcp = fake_connect  # type: ignore[method-assign]
    provider._reconnect_server = tracking_reconnect  # type: ignore[method-assign]
    await provider.initialize()

    # Simulate one server being cancelled (as if it timed out during init)
    provider._server_statuses["cancelled_server"] = MCP_SERVER_STATUS_CANCELLED

    # Wait for the health check to retry both failed/cancelled servers
    await asyncio.wait_for(all_retried.wait(), timeout=5.0)

    assert provider._server_statuses["failed_server"] == MCP_SERVER_STATUS_CONNECTED
    assert provider._server_statuses["cancelled_server"] == MCP_SERVER_STATUS_CONNECTED
    assert "healthy" not in retried_servers

    # Clean up
    provider._health_check_enabled = False
    if provider._health_check_task:
        provider._health_check_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await provider._health_check_task


@pytest.mark.asyncio
async def test_initialize_starts_health_check_with_no_sessions() -> None:
    """initialize() starts health check even when all servers fail, enabling recovery."""
    reconnected = asyncio.Event()

    provider = MCPToolsProvider(
        {"server1": {"transport": "stdio", "command": "echo"}},
        health_check_interval_seconds=0,
    )

    async def fake_connect(
        server_id: str, server_conf: MCPServerConfig
    ) -> tuple[object | None, list, list, dict]:
        if not reconnected.is_set():
            provider._server_statuses[server_id] = MCP_SERVER_STATUS_FAILED
            return None, [], [], {}
        session = SimpleNamespace(list_tools=AsyncMock())
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
        return session, [], [], {}

    original_reconnect = provider._reconnect_server

    async def tracking_reconnect(server_id: str) -> bool:
        reconnected.set()
        return await original_reconnect(server_id)

    provider._connect_and_discover_mcp = fake_connect  # type: ignore[method-assign]
    provider._reconnect_server = tracking_reconnect  # type: ignore[method-assign]
    await provider.initialize()

    # Health check task should be started even though no sessions exist
    assert provider._health_check_task is not None
    assert not provider._health_check_task.done()
    assert len(provider._sessions) == 0

    # Wait for the health check to actually reconnect the server
    await asyncio.wait_for(reconnected.wait(), timeout=5.0)
    assert provider._server_statuses["server1"] == MCP_SERVER_STATUS_CONNECTED

    # Clean up
    provider._health_check_enabled = False
    provider._health_check_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await provider._health_check_task
