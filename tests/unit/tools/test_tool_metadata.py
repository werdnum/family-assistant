from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_DESCRIPTORS,
    LOCAL_TOOL_METADATA_BY_NAME,
    LOCAL_TOOL_REGISTRATIONS,
    TOOLS_DEFINITION,
)
from family_assistant.tools.infrastructure import LocalToolsProvider
from family_assistant.tools.metadata import (
    ToolTag,
    build_local_tool_registrations,
    derive_mcp_annotation_tags,
    make_local_tool_metadata,
    normalize_mcp_tool_metadata,
    resolve_mcp_tool_tags,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


def test_local_tool_catalog_has_complete_metadata_coverage() -> None:
    """Every local tool in the exported catalog should have metadata and descriptors."""
    definition_names = [tool["function"]["name"] for tool in TOOLS_DEFINITION]
    registration_names = [
        registration.name for registration in LOCAL_TOOL_REGISTRATIONS
    ]
    descriptor_names = [descriptor.name for descriptor in LOCAL_TOOL_DESCRIPTORS]

    assert (
        len(TOOLS_DEFINITION)
        == len(AVAILABLE_FUNCTIONS)
        == len(LOCAL_TOOL_REGISTRATIONS)
    )
    assert definition_names == registration_names == descriptor_names
    assert set(LOCAL_TOOL_METADATA_BY_NAME) == set(definition_names)
    assert all(registration.tags for registration in LOCAL_TOOL_REGISTRATIONS)

    descriptor_map = {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }
    assert ToolTag.STATE_PERSISTING in descriptor_map["add_or_update_note"].tags
    assert ToolTag.DELEGATION in descriptor_map["delegate_to_service"].tags
    assert ToolTag.CODE_EXECUTION in descriptor_map["execute_script"].tags


def test_build_local_tool_registrations_rejects_missing_metadata() -> None:
    """Registration building should fail closed when metadata is missing."""

    async def example_tool() -> str:
        return "ok"

    definitions: list[ToolDefinition] = [
        {
            "type": "function",
            "function": {
                "name": "example_tool",
                "description": "Example tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with pytest.raises(ValueError, match="Missing local tool metadata"):
        build_local_tool_registrations(
            definitions=definitions,
            implementations={"example_tool": example_tool},
            metadata_by_name={},
        )


def test_resolve_mcp_tool_tags_prefers_config_then_wildcard_then_annotations() -> None:
    """MCP metadata resolution should follow exact, wildcard, then annotations."""
    annotation_tags = derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=True,
    )
    tool_metadata = normalize_mcp_tool_metadata({
        "search_web": ["low_bandwidth_external", "output_untrusted"],
        "*": ["read_only", "output_trusted"],
    })

    exact_tags = resolve_mcp_tool_tags(
        tool_name="search_web",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    )
    wildcard_tags = resolve_mcp_tool_tags(
        tool_name="get_time",
        configured_tool_metadata=tool_metadata,
        annotation_tags=annotation_tags,
    )
    annotation_only_tags = resolve_mcp_tool_tags(
        tool_name="no_config",
        configured_tool_metadata=None,
        annotation_tags=annotation_tags,
    )

    assert exact_tags == {
        ToolTag.LOW_BANDWIDTH_EXTERNAL,
        ToolTag.OUTPUT_UNTRUSTED,
    }
    assert wildcard_tags == {ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED}
    assert annotation_only_tags == {ToolTag.READ_ONLY, ToolTag.OUTPUT_UNTRUSTED}


def test_resolve_mcp_tool_tags_adds_output_unspecified_when_annotations_lack_output() -> (
    None
):
    """Annotation fallback should still mark output safety as unspecified when needed."""
    annotation_tags = derive_mcp_annotation_tags(
        read_only_hint=True,
        destructive_hint=None,
        open_world_hint=None,
    )

    assert resolve_mcp_tool_tags(
        tool_name="read_only_tool",
        configured_tool_metadata=None,
        annotation_tags=annotation_tags,
    ) == {ToolTag.READ_ONLY, ToolTag.OUTPUT_UNSPECIFIED}


@pytest.mark.asyncio
async def test_local_tools_provider_exposes_descriptors_when_built_from_registrations() -> (
    None
):
    """Providers built from registrations should expose descriptors alongside definitions."""

    async def example_tool() -> str:
        return "ok"

    definitions: list[ToolDefinition] = [
        {
            "type": "function",
            "function": {
                "name": "example_tool",
                "description": "Example tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    registrations = build_local_tool_registrations(
        definitions=definitions,
        implementations={"example_tool": example_tool},
        metadata_by_name={
            "example_tool": make_local_tool_metadata([
                ToolTag.READ_ONLY,
                ToolTag.OUTPUT_TRUSTED,
            ])
        },
    )
    provider = LocalToolsProvider(registrations=registrations)

    descriptor = await provider.get_tool_descriptor("example_tool")

    assert descriptor is not None
    assert descriptor.origin == "local"
    assert descriptor.tags == {ToolTag.READ_ONLY, ToolTag.OUTPUT_TRUSTED}
