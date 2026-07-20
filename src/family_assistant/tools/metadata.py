"""Tool metadata and descriptor models for policy-aware tool handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from family_assistant.tools.types import ToolDefinition

type ToolOrigin = Literal["local", "mcp"]
type ToolImplementation = Callable[..., Awaitable[object]]

_OUTPUT_SAFETY_TAGS = {
    "output_trusted",
    "output_untrusted",
    "output_unspecified",
}


class ToolTag(StrEnum):
    """Security-relevant tags for tools."""

    READ_ONLY = "read_only"
    SENSITIVE_DATA = "sensitive_data"
    STATE_CHANGING = "state_changing"
    STATE_PERSISTING = "state_persisting"
    EXTERNAL_COMM = "external_comm"
    LOW_BANDWIDTH_EXTERNAL = "low_bandwidth_external"
    DESTRUCTIVE = "destructive"
    CODE_EXECUTION = "code_execution"
    OPEN_WORLD = "open_world"
    BROWSER = "browser"
    CAMERA = "camera"
    HOME_AUTOMATION = "home_auto"
    DELEGATION = "delegation"
    FILE_SYSTEM = "file_system"

    OUTPUT_TRUSTED = "output_trusted"
    OUTPUT_UNTRUSTED = "output_untrusted"
    OUTPUT_UNSPECIFIED = "output_unspecified"

    NOTES = "notes"
    CALENDAR = "calendar"
    DOCUMENTS = "documents"
    SCHEDULING = "scheduling"
    MEDIA = "media"
    AUTOMATION = "automation"
    WORKER = "worker"
    DATA = "data"
    SHOPPING = "shopping"
    CONNECTED_ACCOUNT_DATA = "connected_account_data"
    USER_FACING_MEDIA = "user_facing_media"


@dataclass(frozen=True, slots=True)
class LocalToolMetadata:
    """Static metadata for a locally registered tool."""

    tags: frozenset[ToolTag]
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """Registration record for a local tool."""

    definition: ToolDefinition
    implementation: ToolImplementation
    metadata: LocalToolMetadata

    @property
    def name(self) -> str:
        """Return the registered tool name."""
        return get_tool_name(self.definition)

    @property
    def tags(self) -> frozenset[ToolTag]:
        """Return the normalized tags for the tool."""
        return self.metadata.tags


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Internal tool descriptor used by policy evaluation and providers."""

    name: str
    definition: ToolDefinition
    tags: frozenset[ToolTag]
    origin: ToolOrigin
    mcp_server_id: str | None = None
    summary: str | None = None


def normalize_tool_tags(
    tags: list[str | ToolTag] | tuple[str | ToolTag, ...],
) -> frozenset[ToolTag]:
    """Convert tag strings into validated ``ToolTag`` values."""
    normalized: set[ToolTag] = set()
    for raw_tag in tags:
        try:
            normalized.add(ToolTag(raw_tag))
        except ValueError as exc:
            msg = f"Unknown tool tag: {raw_tag!r}"
            raise ValueError(msg) from exc
    return frozenset(normalized)


def make_local_tool_metadata(
    tags: list[str | ToolTag] | tuple[str | ToolTag, ...],
    *,
    summary: str | None = None,
) -> LocalToolMetadata:
    """Create validated local tool metadata."""
    return LocalToolMetadata(tags=normalize_tool_tags(tags), summary=summary)


def get_tool_name(definition: ToolDefinition) -> str:
    """Return the tool name from a tool definition."""
    tool_name = definition.get("function", {}).get("name")
    if not tool_name:
        msg = f"Tool definition is missing function.name: {definition!r}"
        raise ValueError(msg)
    return cast("str", tool_name)


def extract_tool_summary(definition: ToolDefinition) -> str:
    """Extract a short summary from a tool definition's description.

    Uses the first sentence of the description, truncated to 120 characters.
    """
    description = definition.get("function", {}).get("description", "")
    if not description:
        return get_tool_name(definition)
    first_sentence = description.split(". ")[0].split(".\n")[0]
    if len(first_sentence) > 120:
        first_sentence = first_sentence[:117] + "..."
    return first_sentence


def build_tool_descriptor(
    definition: ToolDefinition,
    tags: frozenset[ToolTag],
    *,
    origin: ToolOrigin,
    mcp_server_id: str | None = None,
    summary: str | None = None,
) -> ToolDescriptor:
    """Build a tool descriptor from a definition and tag set."""
    return ToolDescriptor(
        name=get_tool_name(definition),
        definition=definition,
        tags=tags,
        origin=origin,
        mcp_server_id=mcp_server_id,
        summary=summary or extract_tool_summary(definition),
    )


def build_local_tool_registrations(
    definitions: Sequence[ToolDefinition],
    implementations: dict[str, ToolImplementation],
    metadata_by_name: dict[str, LocalToolMetadata],
) -> list[ToolRegistration]:
    """Build validated local tool registrations from definitions and metadata."""
    registrations: list[ToolRegistration] = []
    seen_names: set[str] = set()

    for definition in definitions:
        tool_name = get_tool_name(definition)
        if tool_name in seen_names:
            msg = f"Duplicate local tool definition for {tool_name!r}"
            raise ValueError(msg)
        seen_names.add(tool_name)

        if tool_name not in implementations:
            msg = f"Missing local tool implementation for {tool_name!r}"
            raise ValueError(msg)
        if tool_name not in metadata_by_name:
            msg = f"Missing local tool metadata for {tool_name!r}"
            raise ValueError(msg)

        registrations.append(
            ToolRegistration(
                definition=definition,
                implementation=implementations[tool_name],
                metadata=metadata_by_name[tool_name],
            )
        )

    extra_implementations = set(implementations) - seen_names
    if extra_implementations:
        msg = (
            "Local tool implementations without matching definitions: "
            f"{sorted(extra_implementations)}"
        )
        raise ValueError(msg)

    extra_metadata = set(metadata_by_name) - seen_names
    if extra_metadata:
        msg = (
            "Local tool metadata without matching definitions: "
            f"{sorted(extra_metadata)}"
        )
        raise ValueError(msg)

    return registrations


def build_local_tool_descriptors(
    registrations: Sequence[ToolRegistration],
) -> list[ToolDescriptor]:
    """Build descriptors for local tool registrations."""
    return [
        build_tool_descriptor(
            registration.definition,
            registration.tags,
            origin="local",
            summary=registration.metadata.summary,
        )
        for registration in registrations
    ]


def build_local_tool_descriptors_from_definitions(
    definitions: Sequence[ToolDefinition],
    metadata_by_name: dict[str, LocalToolMetadata],
) -> list[ToolDescriptor]:
    """Build local tool descriptors from a definitions list and metadata map."""
    return [
        build_tool_descriptor(
            definition,
            metadata_by_name[get_tool_name(definition)].tags,
            origin="local",
        )
        for definition in definitions
    ]


def derive_mcp_annotation_tags(
    *,
    read_only_hint: bool | None,
    destructive_hint: bool | None,
    open_world_hint: bool | None,
) -> frozenset[ToolTag]:
    """Convert MCP annotation hints into ``ToolTag`` values."""
    tags: set[ToolTag] = set()
    if read_only_hint is True:
        tags.add(ToolTag.READ_ONLY)
        # The MCP spec defaults openWorldHint to true (mcp/types.py: "Default:
        # true"). A read-only tool whose server did not explicitly close its
        # world (openWorldHint is None or true) can still exfiltrate: the model
        # controls the query/URL sent to the external service. Mark it OPEN_WORLD
        # so the taint resolver keeps it egress-classified rather than treating it
        # as a bare local read. Only an explicit openWorldHint=false clears this.
        if open_world_hint is not False:
            tags.add(ToolTag.OPEN_WORLD)
    if destructive_hint is True and read_only_hint is not True:
        tags.add(ToolTag.DESTRUCTIVE)
    if open_world_hint is True:
        tags.add(ToolTag.OUTPUT_UNTRUSTED)
    return frozenset(tags)


def normalize_mcp_tool_metadata(
    tool_metadata: dict[str, list[str]] | None,
) -> dict[str, frozenset[ToolTag]]:
    """Validate MCP tool metadata loaded from configuration."""
    if not tool_metadata:
        return {}
    return {
        tool_name: normalize_tool_tags(tuple(raw_tags))
        for tool_name, raw_tags in tool_metadata.items()
    }


def resolve_mcp_tool_tags(
    tool_name: str,
    configured_tool_metadata: dict[str, frozenset[ToolTag]] | None,
    annotation_tags: frozenset[ToolTag],
) -> frozenset[ToolTag]:
    """Resolve MCP tool tags from config, wildcard metadata, and annotations."""
    resolved_from_config = None
    if configured_tool_metadata:
        if tool_name in configured_tool_metadata:
            resolved_from_config = configured_tool_metadata[tool_name]
        elif "*" in configured_tool_metadata:
            resolved_from_config = configured_tool_metadata["*"]

    if resolved_from_config is not None:
        return resolved_from_config

    resolved_tags = set(annotation_tags)
    if not {tag.value for tag in resolved_tags}.intersection(_OUTPUT_SAFETY_TAGS):
        resolved_tags.add(ToolTag.OUTPUT_UNSPECIFIED)
    return frozenset(resolved_tags)
