"""Tool metadata and descriptor models for policy-aware tool handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

    from family_assistant.security.taint import SourceTrustTier
    from family_assistant.tools.confirmation_format import ConfirmationRenderer
    from family_assistant.tools.types import ToolDefinition

type ToolOrigin = Literal["local", "mcp"]
type ToolImplementation = Callable[..., Awaitable[object]]


class ToolTag(StrEnum):
    """Security-relevant tags for tools."""

    # Opt-in: unknown tools remain independent script review boundaries.
    SCRIPT_DETERMINISTIC = "script_deterministic"
    READ_ONLY = "read_only"
    SENSITIVE_DATA = "sensitive_data"
    STATE_CHANGING = "state_changing"
    STATE_PERSISTING = "state_persisting"
    EXTERNAL_COMM = "external_comm"
    # Refines EXTERNAL_COMM: the tool communicates outward, but the server
    # validates the recipient against configured users, so the destination is
    # not model-selectable. Carry it alongside EXTERNAL_COMM rather than
    # instead of it, so tool policies matching the broader tag still apply.
    KNOWN_USER_COMM = "known_user_comm"
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
    # Structured data from a third-party service (routes, timetables, prices),
    # graded recognized_machine rather than unknown_external. Free text the
    # service relays from other people, such as reviews, is not this.
    OUTPUT_MACHINE_DATA = "output_machine_data"
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


OUTPUT_TRUST_TAGS: frozenset[ToolTag] = frozenset({
    ToolTag.OUTPUT_TRUSTED,
    ToolTag.OUTPUT_MACHINE_DATA,
    ToolTag.OUTPUT_UNTRUSTED,
    ToolTag.OUTPUT_UNSPECIFIED,
})
"""Tags grading a tool's output for taint; every tool must carry at least one."""


@dataclass(frozen=True, slots=True)
class LocalToolMetadata:
    """Static metadata for a locally registered tool."""

    tags: frozenset[ToolTag]
    summary: str | None = None
    destination_argument_paths: tuple[str, ...] = ()
    deferred_confirmation_eligible: bool = False
    # Lets the tool grade each result itself (``ToolResult.provenance``), never
    # cleaner than this. Without it the static output tag grades every result.
    cleanest_result_tier: SourceTrustTier | None = None


@dataclass(frozen=True, slots=True)
class ToolConfirmation:
    """How a plugin tool is shown to the person asked to approve a call.

    ``block_reason`` refuses arguments no prompt could describe faithfully,
    returning why, or ``None`` to let the call be rendered.
    """

    render: ConfirmationRenderer
    block_reason: Callable[[Mapping[str, object]], str | None] | None = None


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    """Registration record for a local tool."""

    definition: ToolDefinition
    implementation: ToolImplementation
    metadata: LocalToolMetadata
    confirmation: ToolConfirmation | None = None

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
    destination_argument_paths: tuple[str, ...] = ()
    deferred_confirmation_eligible: bool = False
    cleanest_result_tier: SourceTrustTier | None = None


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
    destination_argument_paths: tuple[str, ...] = (),
    deferred_confirmation_eligible: bool = False,
    cleanest_result_tier: SourceTrustTier | None = None,
) -> LocalToolMetadata:
    """Create validated local tool metadata."""
    return LocalToolMetadata(
        tags=normalize_tool_tags(tags),
        summary=summary,
        destination_argument_paths=destination_argument_paths,
        deferred_confirmation_eligible=deferred_confirmation_eligible,
        cleanest_result_tier=cleanest_result_tier,
    )


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
    destination_argument_paths: tuple[str, ...] = (),
    deferred_confirmation_eligible: bool = False,
    cleanest_result_tier: SourceTrustTier | None = None,
) -> ToolDescriptor:
    """Build a tool descriptor from a definition and tag set."""
    return ToolDescriptor(
        name=get_tool_name(definition),
        definition=definition,
        tags=tags,
        origin=origin,
        mcp_server_id=mcp_server_id,
        summary=summary or extract_tool_summary(definition),
        destination_argument_paths=destination_argument_paths,
        deferred_confirmation_eligible=deferred_confirmation_eligible,
        cleanest_result_tier=cleanest_result_tier,
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


def join_tool_registrations(
    *groups: Sequence[ToolRegistration],
) -> list[ToolRegistration]:
    """Concatenate registration groups, refusing a name registered twice."""
    joined: list[ToolRegistration] = []
    seen_names: set[str] = set()
    for group in groups:
        for registration in group:
            if registration.name in seen_names:
                msg = f"Duplicate local tool registration for {registration.name!r}"
                raise ValueError(msg)
            seen_names.add(registration.name)
            joined.append(registration)
    return joined


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
            destination_argument_paths=(
                registration.metadata.destination_argument_paths
            ),
            deferred_confirmation_eligible=(
                registration.metadata.deferred_confirmation_eligible
            ),
            cleanest_result_tier=registration.metadata.cleanest_result_tier,
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
            destination_argument_paths=(
                metadata_by_name[get_tool_name(definition)].destination_argument_paths
            ),
            deferred_confirmation_eligible=(
                metadata_by_name[
                    get_tool_name(definition)
                ].deferred_confirmation_eligible
            ),
            cleanest_result_tier=(
                metadata_by_name[get_tool_name(definition)].cleanest_result_tier
            ),
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


def uncovered_configured_tools(
    tool_names: Iterable[str],
    configured_tool_metadata: dict[str, frozenset[ToolTag]] | None,
) -> tuple[str, ...]:
    """Return tool names an explicit ``tool_metadata`` map does not cover.

    An operator who writes an exact entry per tool has declared a security
    classification for that server. A tool the map omits -- and that no ``*``
    wildcard catches -- silently falls back to its protocol annotations, which
    is how a server that gains a tool (``booking``) or exposes one the operator
    forgot lands on an unintended sink class. Returns the uncovered names, sorted,
    so a caller can warn. An empty map or a wildcard covers everything.
    """
    if not configured_tool_metadata or "*" in configured_tool_metadata:
        return ()
    return tuple(
        sorted(name for name in tool_names if name not in configured_tool_metadata)
    )


def resolve_mcp_tool_tags(
    tool_name: str,
    configured_tool_metadata: dict[str, frozenset[ToolTag]] | None,
    annotation_tags: frozenset[ToolTag],
) -> frozenset[ToolTag]:
    """Resolve MCP tool tags from config, wildcard metadata, and annotations.

    Whichever source wins, a set without an output trust tag gains
    ``output_unspecified``, so policies matching it see every such tool.
    """
    resolved_from_config = None
    if configured_tool_metadata:
        if tool_name in configured_tool_metadata:
            resolved_from_config = configured_tool_metadata[tool_name]
        elif "*" in configured_tool_metadata:
            resolved_from_config = configured_tool_metadata["*"]

    resolved_tags = set(
        annotation_tags if resolved_from_config is None else resolved_from_config
    )
    if not resolved_tags & OUTPUT_TRUST_TAGS:
        resolved_tags.add(ToolTag.OUTPUT_UNSPECIFIED)
    return frozenset(resolved_tags)
