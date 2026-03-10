from __future__ import annotations

import copy

import pytest

from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_METADATA_BY_NAME,
    LOCAL_TOOL_REGISTRATIONS,
    TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    build_local_tool_registrations,
)
from family_assistant.tools.metadata import ToolTag


@pytest.mark.asyncio
async def test_runtime_builds_descriptors_without_changing_advertised_local_tools() -> (
    None
):
    """The real local tool catalog should expose descriptors while keeping advertisement stable."""
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, MCPToolsProvider(mcp_server_configs={})]
    )

    advertised_names = [
        tool["function"]["name"]
        for tool in await composite_provider.get_tool_definitions()
    ]
    descriptors = await composite_provider.get_tool_descriptors()
    descriptor_map = {descriptor.name: descriptor for descriptor in descriptors}

    assert advertised_names == [tool["function"]["name"] for tool in TOOLS_DEFINITION]
    assert set(descriptor_map) == set(advertised_names)
    assert descriptor_map["add_or_update_note"].origin == "local"
    assert ToolTag.STATE_PERSISTING in descriptor_map["add_or_update_note"].tags
    assert ToolTag.DELEGATION in descriptor_map["delegate_to_service"].tags


@pytest.mark.asyncio
async def test_modified_local_definitions_can_be_re_registered_with_metadata() -> None:
    """Assistant-style definition customization should preserve local metadata and descriptors."""
    modified_definitions = copy.deepcopy(TOOLS_DEFINITION)
    for tool_definition in modified_definitions:
        if tool_definition["function"]["name"] == "delegate_to_service":
            tool_definition["function"]["description"] = "Updated description for test"
            break

    modified_registrations = build_local_tool_registrations(
        definitions=modified_definitions,
        implementations=AVAILABLE_FUNCTIONS,
        metadata_by_name=LOCAL_TOOL_METADATA_BY_NAME,
    )
    provider = LocalToolsProvider(registrations=modified_registrations)

    descriptor = await provider.get_tool_descriptor("delegate_to_service")

    assert descriptor is not None
    assert (
        descriptor.definition["function"]["description"]
        == "Updated description for test"
    )
    assert ToolTag.DELEGATION in descriptor.tags
