"""Per-profile tool inventory introspection for diagnosing tool bloat.

A processing profile advertises a set of tool definitions to the LLM on every
turn. Some tools are *eager* (always advertised) while others are hidden behind
progressive disclosure (the ``activate_tools`` meta-tool) and only advertised
after the model activates them. The eager set is what costs prompt tokens on
every single turn, so it is the primary driver of "tool bloat".

This module computes a machine-readable breakdown of a profile's resolved tool
set, partitioned into eager vs on-demand, attributed to each source (local vs a
specific MCP server), with a serialized-size and heuristic token estimate per
tool. It is the shared core used by both the ``/api/debug/profiles/tools``
endpoint and the engineer-profile ``get_profile_tool_inventory`` tool.

The token figures are a heuristic (serialized JSON characters // 4); they are
meant for *relative* comparison between profiles and sources, not as an exact
provider token count.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.tools.infrastructure import PolicyEnforcingToolsProvider
    from family_assistant.tools.on_demand import OnDemandToolsView
    from family_assistant.tools.types import ToolDefinition

# Name of the synthetic meta-tool injected by ``OnDemandToolsView`` to let the
# model unlock on-demand tools. It is not a real registered tool, so it is
# attributed to the ``meta`` source rather than ``local``.
_ACTIVATE_TOOLS_NAME = "activate_tools"

# Shared disclaimer surfaced alongside every inventory payload so consumers know
# the token figures are a heuristic, not an exact provider count.
TOKEN_ESTIMATE_NOTE = (
    "estimated_tokens is a heuristic (serialized JSON characters / 4) "
    "for relative comparison, not an exact provider token count."
)

_META_SOURCE = "meta"
_LOCAL_SOURCE = "local"


def _estimate_tokens(serialized_chars: int) -> int:
    """Heuristic token estimate from serialized JSON length.

    Roughly four characters per token. This is deliberately provider-agnostic
    and offline; it is for relative comparison, not exact billing.
    """
    if serialized_chars <= 0:
        return 0
    return (serialized_chars + 3) // 4


def _definition_name(definition: ToolDefinition) -> str | None:
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) else None


@dataclass(frozen=True)
class ToolSizeEntry:
    """Size accounting for a single advertised tool definition."""

    name: str
    source: str  # "local" | "mcp:<server_id>" | "meta"
    serialized_chars: int
    estimated_tokens: int


@dataclass(frozen=True)
class ToolGroupSummary:
    """Aggregated size for a group of tools (eager or on-demand)."""

    count: int
    serialized_chars: int
    estimated_tokens: int
    tools: list[ToolSizeEntry] = field(default_factory=list)


@dataclass(frozen=True)
class SourceBreakdown:
    """Per-source contribution to the advertised tool surface."""

    source: str
    eager_count: int
    on_demand_count: int
    serialized_chars: int
    estimated_tokens: int


@dataclass(frozen=True)
class ToolInventory:
    """Resolved per-profile tool advertisement, partitioned for bloat analysis.

    ``eager`` is what the profile advertises to the LLM on every turn (the
    always-present tools plus the ``activate_tools`` meta-tool when present).
    ``on_demand`` is the set hidden behind progressive disclosure until the
    model activates it. ``advertised_per_turn_tokens`` equals
    ``eager.estimated_tokens`` and is the headline bloat number;
    ``all_if_activated_tokens`` is the worst case if every on-demand tool were
    activated in a single turn.
    """

    profile_id: str | None
    can_confirm: bool
    has_on_demand_view: bool
    eager: ToolGroupSummary
    on_demand: ToolGroupSummary
    activate_tools_present: bool
    by_source: list[SourceBreakdown]
    advertised_per_turn_tokens: int
    all_if_activated_tokens: int

    # ast-grep-ignore: no-dict-any - Serializes a typed dataclass to a JSON-ready mapping
    def to_dict(self, *, include_tools: bool = True) -> dict[str, Any]:
        """Return a plain JSON-serializable dict of this inventory.

        With ``include_tools=False`` the per-tool lists are dropped, leaving the
        summary-only counts and token estimates for a compact payload.
        """
        data = asdict(self)
        if not include_tools:
            data["eager"].pop("tools", None)
            data["on_demand"].pop("tools", None)
        return data


def _classify_source(name: str, mcp_server_id_by_name: dict[str, str | None]) -> str:
    if name == _ACTIVATE_TOOLS_NAME:
        return _META_SOURCE
    server_id = mcp_server_id_by_name.get(name)
    if server_id:
        return f"mcp:{server_id}"
    return _LOCAL_SOURCE


def _build_entries(
    definitions: list[ToolDefinition],
    mcp_server_id_by_name: dict[str, str | None],
) -> list[ToolSizeEntry]:
    entries: list[ToolSizeEntry] = []
    for definition in definitions:
        name = _definition_name(definition)
        if name is None:
            continue
        serialized_chars = len(json.dumps(definition, sort_keys=True, default=str))
        entries.append(
            ToolSizeEntry(
                name=name,
                source=_classify_source(name, mcp_server_id_by_name),
                serialized_chars=serialized_chars,
                estimated_tokens=_estimate_tokens(serialized_chars),
            )
        )
    entries.sort(key=lambda entry: entry.estimated_tokens, reverse=True)
    return entries


def _summarize(entries: list[ToolSizeEntry]) -> ToolGroupSummary:
    serialized_chars = sum(entry.serialized_chars for entry in entries)
    return ToolGroupSummary(
        count=len(entries),
        serialized_chars=serialized_chars,
        estimated_tokens=sum(entry.estimated_tokens for entry in entries),
        tools=entries,
    )


def _source_breakdowns(
    eager: list[ToolSizeEntry], on_demand: list[ToolSizeEntry]
) -> list[SourceBreakdown]:
    sources = sorted({entry.source for entry in (*eager, *on_demand)})
    breakdowns: list[SourceBreakdown] = []
    for source in sources:
        eager_for_source = [e for e in eager if e.source == source]
        on_demand_for_source = [e for e in on_demand if e.source == source]
        serialized_chars = sum(
            e.serialized_chars for e in (*eager_for_source, *on_demand_for_source)
        )
        estimated_tokens = sum(
            e.estimated_tokens for e in (*eager_for_source, *on_demand_for_source)
        )
        breakdowns.append(
            SourceBreakdown(
                source=source,
                eager_count=len(eager_for_source),
                on_demand_count=len(on_demand_for_source),
                serialized_chars=serialized_chars,
                estimated_tokens=estimated_tokens,
            )
        )
    breakdowns.sort(key=lambda b: b.estimated_tokens, reverse=True)
    return breakdowns


async def build_tool_inventory(
    *,
    tools_provider: PolicyEnforcingToolsProvider,
    on_demand_view: OnDemandToolsView | None,
    can_confirm: bool = True,
    profile_id: str | None = None,
) -> ToolInventory:
    """Compute the resolved tool inventory for a single profile.

    ``tools_provider`` is the profile's policy-enforcing provider (the full
    policy-allowed set, with on-demand tools NOT yet hidden). ``on_demand_view``
    is the profile's on-demand view, or ``None`` when the profile hides no tools
    (in which case every allowed tool is eager).

    ``can_confirm`` mirrors the per-turn capability: when ``False`` the policy
    engine may drop confirmation-gated tools, so the inventory reflects the
    interaction kind. The default ``True`` represents the common case.
    """
    # The full policy-allowed set (on-demand tools NOT hidden). The second call
    # below re-filters the same memoized provider output rather than re-fetching
    # (PolicyEnforcingToolsProvider caches per can_confirm), so the double call
    # is cheap.
    all_definitions = await tools_provider.get_tool_definitions(can_confirm=can_confirm)

    if on_demand_view is not None:
        # Ask the view for the authoritative eager set (what the LLM actually
        # sees at turn start), then derive on-demand as the policy-allowed
        # remainder. The view wraps this same provider and forwards the same
        # can_confirm, so eager_definitions is a subset of all_definitions and
        # the remainder is exactly the progressively-disclosed tools. Deriving
        # by subtraction (rather than the view's name-only catalog) keeps the
        # full definition for each hidden tool so it can be sized.
        eager_definitions = await on_demand_view.get_tool_definitions(
            can_confirm=can_confirm, activated=frozenset()
        )
    else:
        eager_definitions = all_definitions

    eager_names = {
        name
        for definition in eager_definitions
        if (name := _definition_name(definition)) is not None
    }
    activate_tools_present = _ACTIVATE_TOOLS_NAME in eager_names

    on_demand_definitions = [
        definition
        for definition in all_definitions
        if _definition_name(definition) not in eager_names
    ]

    mcp_server_id_by_name: dict[str, str | None] = {
        descriptor.name: descriptor.mcp_server_id
        for descriptor in await tools_provider.get_tool_descriptors()
    }

    eager_entries = _build_entries(eager_definitions, mcp_server_id_by_name)
    on_demand_entries = _build_entries(on_demand_definitions, mcp_server_id_by_name)

    eager_summary = _summarize(eager_entries)
    on_demand_summary = _summarize(on_demand_entries)

    return ToolInventory(
        profile_id=profile_id,
        can_confirm=can_confirm,
        has_on_demand_view=on_demand_view is not None,
        eager=eager_summary,
        on_demand=on_demand_summary,
        activate_tools_present=activate_tools_present,
        by_source=_source_breakdowns(eager_entries, on_demand_entries),
        advertised_per_turn_tokens=eager_summary.estimated_tokens,
        all_if_activated_tokens=(
            eager_summary.estimated_tokens + on_demand_summary.estimated_tokens
        ),
    )


async def inventory_dict_for_service(
    profile_id: str,
    service: object,
    *,
    can_confirm: bool = True,
    include_tools: bool = True,
    # ast-grep-ignore: no-dict-any - Returns a serialized ToolInventory or error marker
) -> dict[str, Any]:
    """Build one live profile's inventory dict, or an error dict.

    Reads ``tools_provider`` / ``on_demand_view`` off a live processing service
    (duck-typed, so delegation-only stubs are handled gracefully) and returns
    either the serialized :class:`ToolInventory` or, when no tools provider is
    wired up, an explicit ``{"profile_id", "error"}`` marker so the caller can
    report it rather than silently dropping the profile. Shared by the
    ``/api/debug/profiles/tools`` endpoint and the ``get_profile_tool_inventory``
    engineer tool so the service-introspection contract lives in one place.
    """
    tools_provider = getattr(service, "tools_provider", None)
    if tools_provider is None:
        return {
            "profile_id": profile_id,
            "error": "No tools provider wired into this profile's service.",
        }
    inventory = await build_tool_inventory(
        tools_provider=tools_provider,
        on_demand_view=getattr(service, "on_demand_view", None),
        can_confirm=can_confirm,
        profile_id=profile_id,
    )
    return inventory.to_dict(include_tools=include_tools)
