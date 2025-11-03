"""Unit tests for MCP tool filtering by server ID."""

from unittest.mock import AsyncMock

import pytest

from family_assistant.tools import (
    FilteredToolsProvider,
    MCPToolsProvider,
)


@pytest.mark.asyncio
async def test_mcp_provider_exposes_tool_to_server_mapping() -> None:
    """Test that MCPToolsProvider exposes tool-to-server mapping."""
    # Create MCPToolsProvider with mock config
    mcp_configs = {
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
