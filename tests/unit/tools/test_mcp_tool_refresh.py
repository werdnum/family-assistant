"""Unit tests for refreshing MCP tool lists from the health check."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import ListToolsResult, Tool, ToolAnnotations

from family_assistant.tools import MCPServerConfig, MCPToolsProvider
from family_assistant.tools.mcp import MCP_SERVER_STATUS_CONNECTED
from family_assistant.tools.metadata import ToolTag

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp import ClientSession

SERVER_ID = "drifting-server"
OTHER_SERVER_ID = "other-server"


def _tool(
    name: str,
    description: str = "does a thing",
    annotations: ToolAnnotations | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
        annotations=annotations,
    )


def _session(tools: Sequence[Tool]) -> ClientSession:
    """A stand-in session that reports a fixed tool list."""
    list_tools = AsyncMock(return_value=ListToolsResult(tools=list(tools)))
    return cast("ClientSession", SimpleNamespace(list_tools=list_tools))


def _paginated_session(pages: Sequence[Sequence[Tool]]) -> ClientSession:
    """A stand-in session that hands out its tool list one page at a time."""
    results = [
        ListToolsResult(
            tools=list(page),
            nextCursor=str(index + 1) if index + 1 < len(pages) else None,
        )
        for index, page in enumerate(pages)
    ]

    async def list_tools(cursor: str | None = None) -> ListToolsResult:
        return results[int(cursor) if cursor else 0]

    return cast(
        "ClientSession", SimpleNamespace(list_tools=AsyncMock(wraps=list_tools))
    )


def _provider(*server_ids: str) -> MCPToolsProvider:
    configs: dict[str, MCPServerConfig] = {
        server_id: {"transport": "stdio", "command": "echo"} for server_id in server_ids
    }
    provider = MCPToolsProvider(configs)
    # These tests model a provider that has already connected; nothing here
    # should reach out to a real server.
    provider._initialized = True
    for server_id in server_ids:
        provider._server_statuses[server_id] = MCP_SERVER_STATUS_CONNECTED
    return provider


def _register(
    provider: MCPToolsProvider, server_id: str, tools: Sequence[Tool]
) -> None:
    """Seed the provider with the tools a server reported at connect time."""
    definitions = provider._format_mcp_definitions_to_dicts(list(tools))
    provider._register_server_tools(
        server_id,
        definitions,
        provider._build_mcp_descriptors(
            server_id=server_id, definitions=definitions, discovered_tools=list(tools)
        ),
    )


def _tool_names(provider: MCPToolsProvider) -> set[str]:
    return {descriptor.name for descriptor in provider._descriptors}


@pytest.mark.asyncio
async def test_tools_appear_when_a_server_starts_reporting_them() -> None:
    """A server that came up empty is not written off for the process's lifespan."""
    provider = _provider(SERVER_ID)
    provider._sessions[SERVER_ID] = _session([_tool("search")])

    await provider._run_health_checks()

    assert _tool_names(provider) == {"search"}
    assert provider.get_tool_to_server_mapping() == {"search": SERVER_ID}


@pytest.mark.asyncio
async def test_a_paginated_tool_list_is_read_to_the_end() -> None:
    """Reconciling against page one alone would retire every later page."""
    provider = _provider(SERVER_ID)
    provider._sessions[SERVER_ID] = _paginated_session([
        [_tool("search")],
        [_tool("fetch")],
    ])

    await provider._run_health_checks()

    assert _tool_names(provider) == {"search", "fetch"}


@pytest.mark.asyncio
async def test_tools_that_vanish_from_the_server_are_dropped() -> None:
    """The cache mirrors the server, so a withdrawn tool stops being advertised."""
    provider = _provider(SERVER_ID)
    _register(provider, SERVER_ID, [_tool("search"), _tool("fetch")])
    provider._sessions[SERVER_ID] = _session([_tool("search")])

    await provider._run_health_checks()

    assert _tool_names(provider) == {"search"}
    assert provider.get_tool_to_server_mapping() == {"search": SERVER_ID}


@pytest.mark.asyncio
async def test_changed_tool_schema_is_picked_up() -> None:
    """A redefined tool is refreshed, not just an added or removed one."""
    provider = _provider(SERVER_ID)
    _register(provider, SERVER_ID, [_tool("search", description="old")])
    provider._sessions[SERVER_ID] = _session([_tool("search", description="new")])

    await provider._run_health_checks()

    definitions = await provider.get_tool_definitions()
    assert [d["function"]["description"] for d in definitions] == ["new"]


@pytest.mark.asyncio
async def test_changed_tool_annotations_are_picked_up() -> None:
    """Tags drive policy, so a tool that stops being read-only has changed."""
    provider = _provider(SERVER_ID)
    _register(
        provider,
        SERVER_ID,
        [_tool("search", annotations=ToolAnnotations(readOnlyHint=True))],
    )
    provider._sessions[SERVER_ID] = _session([
        _tool("search", annotations=ToolAnnotations(destructiveHint=True))
    ])

    await provider._run_health_checks()

    descriptors = await provider.get_tool_descriptors()
    assert [descriptor.name for descriptor in descriptors] == ["search"]
    assert ToolTag.READ_ONLY not in descriptors[0].tags
    assert ToolTag.DESTRUCTIVE in descriptors[0].tags


@pytest.mark.asyncio
async def test_unchanged_tool_list_does_not_invalidate_downstream_caches() -> None:
    """Steady state must not bump the version every health check cycle."""
    provider = _provider(SERVER_ID)
    _register(provider, SERVER_ID, [_tool("search")])
    provider._sessions[SERVER_ID] = _session([_tool("search")])
    version = provider.descriptors_version

    await provider._run_health_checks()

    assert provider.descriptors_version == version


@pytest.mark.asyncio
async def test_a_reordered_tool_list_is_not_a_change() -> None:
    """A server that shuffles its list must not churn the caches every cycle."""
    provider = _provider(SERVER_ID)
    _register(provider, SERVER_ID, [_tool("search"), _tool("fetch")])
    provider._sessions[SERVER_ID] = _session([_tool("fetch"), _tool("search")])
    version = provider.descriptors_version

    await provider._run_health_checks()

    assert provider.descriptors_version == version
    assert _tool_names(provider) == {"search", "fetch"}


@pytest.mark.asyncio
async def test_refresh_does_not_steal_a_tool_name_from_another_server() -> None:
    """The first server to claim a duplicated name keeps it."""
    provider = _provider(OTHER_SERVER_ID, SERVER_ID)
    _register(provider, OTHER_SERVER_ID, [_tool("search")])
    provider._sessions[SERVER_ID] = _session([_tool("search"), _tool("fetch")])

    await provider._run_health_checks()

    assert provider.get_tool_to_server_mapping() == {
        "search": OTHER_SERVER_ID,
        "fetch": SERVER_ID,
    }


@pytest.mark.asyncio
async def test_duplicate_tool_name_does_not_churn_the_descriptor_version() -> None:
    """A name that can never be registered must not look like a change each cycle."""
    provider = _provider(OTHER_SERVER_ID, SERVER_ID)
    _register(provider, OTHER_SERVER_ID, [_tool("search")])
    provider._sessions[SERVER_ID] = _session([_tool("search")])
    await provider._run_health_checks()
    version = provider.descriptors_version

    await provider._run_health_checks()

    assert provider.descriptors_version == version


@pytest.mark.asyncio
async def test_failed_health_check_does_not_drop_the_cached_tools() -> None:
    """Tools survive a transport error until the reconnect decides otherwise."""
    provider = _provider(SERVER_ID)
    _register(provider, SERVER_ID, [_tool("search")])
    session = cast(
        "ClientSession",
        SimpleNamespace(list_tools=AsyncMock(side_effect=TimeoutError())),
    )
    provider._sessions[SERVER_ID] = session

    await provider._run_health_checks()

    assert _tool_names(provider) == {"search"}
