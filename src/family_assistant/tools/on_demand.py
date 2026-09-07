"""On-demand tool activation support.

Provides the ``OnDemandToolsView``: an LLM-loop-only view over a real
``ToolsProvider`` that:

* hides on-demand tools from ``get_tool_definitions`` until the LLM activates
  them via the synthetic ``activate_tools`` meta-tool, and
* renders an on-demand catalog into the system prompt.

The view is not itself a ``ToolsProvider``. On-demand gating is a concern of
the LLM loop (which has a context window to protect); other consumers — most
notably the script engine — talk to the underlying provider directly and see
all tools that pass policy.

Activation state is *not* stored on the view. The view is long-lived per
profile and shared across concurrent conversations, so mutable activation
state would race between turns. Callers (the LLM loop) keep a turn-local
``activated`` set and pass it in to every method that needs to know which
tools are currently unlocked.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from family_assistant.tools.infrastructure import (
    ToolDescriptorProvider,
    ToolsProvider,
    resolve_descriptors_version,
)
from family_assistant.tools.live_meta import LIVE_META_TOOL_NAMES
from family_assistant.tools.metadata import ToolDescriptor, extract_tool_summary

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.tools.types import ToolDefinition

logger = logging.getLogger(__name__)


_EMPTY_ACTIVATED: frozenset[str] = frozenset()

ACTIVATE_TOOLS_TOOL_NAME = "activate_tools"

# Names a real tool may not take, because a consumer intercepts them before
# dispatch reaches the wrapped provider.
RESERVED_META_TOOL_NAMES: frozenset[str] = LIVE_META_TOOL_NAMES | {
    ACTIVATE_TOOLS_TOOL_NAME
}


# ---------------------------------------------------------------------------
# Catalog data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnDemandCatalogEntry:
    """A single entry in the on-demand tool catalog."""

    name: str
    summary: str


@dataclass(frozen=True)
class OnDemandToolCatalog:
    """Catalog of on-demand tools for system prompt injection."""

    entries: list[OnDemandCatalogEntry]

    def render_for_system_prompt(
        self,
        *,
        heading: str = "## On-Demand Tools",
        instruction: str = (
            "The following tools are available but not yet active. "
            "Call `activate_tools` with their names to enable them:"
        ),
        include_summaries: bool = True,
    ) -> str:
        """Render catalog as a system prompt section.

        ``heading`` and ``instruction`` are overridable because the same
        catalog is presented differently depending on how the caller lets the
        model reach a hidden tool: the LLM loop activates tools, while a Live
        (voice) session — whose declaration list is frozen at session setup —
        calls them through a meta-tool instead.

        ``include_summaries`` drops to a bare name list, which costs about a
        fifth of the tokens. It suits a caller whose disclosure mechanism hands
        the model the description anyway when it asks for one, so a summary
        here would only be paid for twice; a caller whose mechanism activates a
        tool by name has no such second chance and keeps them.

        Returns empty string when there are no on-demand tools.
        """
        if not self.entries:
            return ""
        if not include_summaries:
            names = ", ".join(entry.name for entry in self.entries)
            return f"{heading}\n{instruction}\n{names}"
        lines = [heading, instruction]
        for entry in self.entries:
            lines.append(f"- **{entry.name}**: {entry.summary}")
        return "\n".join(lines)


@dataclass(frozen=True)
class VisibleDefinitions:
    """Definitions a caller may declare, plus the on-demand names withheld."""

    definitions: list[ToolDefinition]
    hidden_names: frozenset[str]


@dataclass(frozen=True)
class OnDemandActivationResult:
    """Result of activating on-demand tools for a single turn."""

    newly_activated: frozenset[str]
    definitions: list[ToolDefinition]


# ---------------------------------------------------------------------------
# activate_tools meta-tool definition
# ---------------------------------------------------------------------------

ACTIVATE_TOOLS_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": ACTIVATE_TOOLS_TOOL_NAME,
        "description": (
            "Activate on-demand tools so you can use them in this conversation. "
            "Call with specific tool names from the on-demand catalog, search "
            "by keyword, or activate every on-demand tool on a given MCP server."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of tools to activate from the on-demand catalog",
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Search keyword to find and activate matching tools. "
                        "Searches tool names and summaries."
                    ),
                },
                "mcp_server_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs of on-demand MCP servers to activate. Activates "
                        "every on-demand tool exposed by each listed server."
                    ),
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# OnDemandToolsView
# ---------------------------------------------------------------------------


class OnDemandToolsView:
    """LLM-loop view that hides on-demand tools from the LLM until activated.

    Not a ``ToolsProvider``. Holds a reference to a real provider — typically
    a ``PolicyEnforcingToolsProvider`` — and exposes only the LLM-loop-facing
    operations: filtered tool-definition listing, system-prompt catalog
    rendering, and ``activate_tools``. Tool execution and descriptor access
    go through the underlying provider directly; on-demand gating only
    controls what the LLM sees in its tool list.

    The view itself holds no activation state. The LLM loop keeps a
    turn-local ``activated`` set and threads it through every call; that way
    concurrent turns on a shared view do not race on a common mutable set.
    """

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        on_demand_tool_names: set[str],
        on_demand_mcp_server_ids: set[str] | None = None,
    ) -> None:
        if not isinstance(wrapped_provider, ToolDescriptorProvider):
            msg = (
                "OnDemandToolsView requires a wrapped provider that "
                "supports tool descriptors."
            )
            raise ValueError(msg)
        self._wrapped_provider = wrapped_provider
        self._descriptor_provider = wrapped_provider
        self._on_demand_tool_names = on_demand_tool_names
        self._on_demand_mcp_server_ids = on_demand_mcp_server_ids or set()
        self._all_descriptors: list[ToolDescriptor] | None = None
        self._descriptors_version: int | None = None
        # Cache whether the wrapped provider's get_tool_definitions accepts
        # ``can_confirm``. The wrapped provider does not change for the lifetime
        # of this object, so doing the inspect.signature reflection once in
        # __init__ avoids paying the cost on every fetch.
        self._wrapped_accepts_can_confirm = any(
            parameter.name == "can_confirm"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in inspect.signature(
                wrapped_provider.get_tool_definitions
            ).parameters.values()
        )

    @property
    def wrapped_provider(self) -> ToolsProvider:
        """Return the underlying provider this view filters."""
        return self._wrapped_provider

    @property
    def on_demand_tool_names(self) -> frozenset[str]:
        """Return the configured on-demand tool names.

        Exposed for diagnostics (engineer profile policy resolution).
        """
        return frozenset(self._on_demand_tool_names)

    @property
    def on_demand_mcp_server_ids(self) -> frozenset[str]:
        """Return the configured on-demand MCP server ids.

        Exposed for diagnostics (engineer profile policy resolution).
        """
        return frozenset(self._on_demand_mcp_server_ids)

    async def ensure_no_reserved_name_collisions(self) -> None:
        """Raise if the wrapped provider exposes a reserved meta-tool name.

        The check itself lives with the descriptor cache, which every listing
        path populates. A consumer that intercepts a meta-tool name before
        dispatch — and can therefore be reached without having asked for a
        listing first — calls this so the invariant holds on that path too.
        """
        await self._ensure_descriptors()

    def has_on_demand_tools(self) -> bool:
        """Return True if there are any on-demand tool names configured."""
        return bool(self._on_demand_tool_names) or bool(self._on_demand_mcp_server_ids)

    # --- Internal helpers ---

    async def _ensure_descriptors(self) -> list[ToolDescriptor]:
        """Populate the descriptor cache from the wrapped provider on first access.

        A meta-tool name is intercepted before dispatch reaches the wrapped
        provider — ``activate_tools`` by the LLM loop, ``search_tools`` and
        ``call_tool`` by the Live meta-tools provider. A real tool carrying one
        of those names would be silently shadowed, and would additionally be
        declared twice to a Live session, so refuse to wrap such a provider and
        fail loudly instead. All three are reserved here rather than per
        consumer, because one view serves both and any profile may be reached
        by voice.

        The cache is rebuilt when the wrapped descriptor set changes (an MCP
        server connecting, reconnecting, or disconnecting), so on-demand
        activation of a server that was down at startup works once the
        health-check loop reconnects it.
        """
        current_version = resolve_descriptors_version(self._descriptor_provider)
        if (
            self._all_descriptors is None
            or current_version != self._descriptors_version
        ):
            descriptors = await self._descriptor_provider.get_tool_descriptors()
            collisions = sorted(
                d.name for d in descriptors if d.name in RESERVED_META_TOOL_NAMES
            )
            if collisions:
                msg = (
                    "OnDemandToolsView cannot wrap a provider that already "
                    f"exposes {', '.join(repr(name) for name in collisions)}; "
                    "these names are reserved for the on-demand meta-tools."
                )
                raise ValueError(msg)
            self._all_descriptors = descriptors
            self._descriptors_version = current_version
        return self._all_descriptors

    async def _fetch_wrapped_definitions(
        self, *, can_confirm: bool
    ) -> list[ToolDefinition]:
        """Fetch definitions from the wrapped provider, forwarding ``can_confirm``.

        ``PolicyEnforcingToolsProvider`` accepts ``can_confirm``; other providers
        do not. The decision is cached in ``__init__`` so this hot path does not
        repeat the ``inspect.signature`` reflection on every call.
        """
        method = self._wrapped_provider.get_tool_definitions
        if self._wrapped_accepts_can_confirm:
            return await cast("Any", method)(can_confirm=can_confirm)
        return await method()

    def _is_on_demand(
        self, descriptor: ToolDescriptor, activated: frozenset[str]
    ) -> bool:
        """Check if a descriptor is on-demand and not yet activated for this turn."""
        if descriptor.name in activated:
            return False
        if descriptor.name in self._on_demand_tool_names:
            return True
        return bool(
            descriptor.mcp_server_id
            and descriptor.mcp_server_id in self._on_demand_mcp_server_ids
        )

    def _freeze_activated(self, activated: Iterable[str] | None) -> frozenset[str]:
        if activated is None:
            return _EMPTY_ACTIVATED
        if isinstance(activated, frozenset):
            return activated
        return frozenset(activated)

    # --- LLM-loop-facing API ---

    async def get_visible_definitions(
        self,
        *,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> VisibleDefinitions:
        """Return the definitions the model may call directly, and what is hidden.

        ``hidden_names`` is what the wrapped provider *would* advertise for this
        interaction but is being withheld as on-demand, which is what tells a
        caller whether a meta-tool for reaching those is worth declaring at all.
        A caller that adds its own meta-tool declaration builds on this; the
        activation presentation is ``get_tool_definitions`` below.
        """
        activated_frozen = self._freeze_activated(activated)
        descriptors = await self._ensure_descriptors()
        wrapped_defs = await self._fetch_wrapped_definitions(can_confirm=can_confirm)

        on_demand_hidden_names = {
            d.name for d in descriptors if self._is_on_demand(d, activated_frozen)
        }
        advertisable_names = {
            name
            for defn in wrapped_defs
            if (name := defn.get("function", {}).get("name")) is not None
        }
        return VisibleDefinitions(
            definitions=[
                defn
                for defn in wrapped_defs
                if defn.get("function", {}).get("name") not in on_demand_hidden_names
            ],
            hidden_names=frozenset(on_demand_hidden_names & advertisable_names),
        )

    async def get_tool_definitions(
        self,
        *,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> list[ToolDefinition]:
        """Return eager + activated definitions, plus ``activate_tools`` if needed.

        ``can_confirm`` is forwarded to the wrapped provider. ``activated``
        lists the on-demand tool names already unlocked for this turn. The
        synthetic ``activate_tools`` meta-tool is only included when there is
        at least one on-demand tool the model could still usefully activate in
        this interaction (after policy filtering).
        """
        visible = await self.get_visible_definitions(
            can_confirm=can_confirm, activated=activated
        )
        if visible.hidden_names:
            return [*visible.definitions, ACTIVATE_TOOLS_DEFINITION]
        return visible.definitions

    async def get_system_prompt_addition(
        self,
        *,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> str | None:
        """Return the on-demand catalog rendered for system prompt injection.

        ``activated`` is the caller's turn-local activation set. Tools the
        caller has already activated this turn are removed from the catalog so
        they do not appear as still-on-demand.
        """
        if not self.has_on_demand_tools():
            return None
        catalog = await self.get_on_demand_catalog(
            can_confirm=can_confirm, activated=activated
        )
        if not catalog.entries:
            return None
        return catalog.render_for_system_prompt()

    async def get_on_demand_catalog(
        self,
        *,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> OnDemandToolCatalog:
        """Return catalog of on-demand tools not yet activated for this turn.

        Only tools that the wrapped policy provider would actually advertise for
        the current ``can_confirm`` value are surfaced; otherwise the catalog
        could include tools the model cannot activate or use in this turn.
        """
        activated_frozen = self._freeze_activated(activated)
        descriptors = await self._ensure_descriptors()
        advertisable_names = await self._advertisable_names(can_confirm=can_confirm)
        entries = [
            OnDemandCatalogEntry(
                name=descriptor.name,
                summary=descriptor.summary
                or extract_tool_summary(descriptor.definition),
            )
            for descriptor in descriptors
            if self._is_on_demand(descriptor, activated_frozen)
            and descriptor.name in advertisable_names
        ]
        return OnDemandToolCatalog(entries=entries)

    async def _advertisable_names(self, *, can_confirm: bool) -> set[str]:
        """Names the wrapped provider would advertise for ``can_confirm``."""
        wrapped_defs = await self._fetch_wrapped_definitions(can_confirm=can_confirm)
        return {
            name
            for defn in wrapped_defs
            if (name := defn.get("function", {}).get("name")) is not None
        }

    async def activate_tools(
        self,
        *,
        names: list[str] | None = None,
        search: str | None = None,
        mcp_server_ids: list[str] | None = None,
        can_confirm: bool = True,
        activated: Iterable[str] | None = None,
    ) -> OnDemandActivationResult:
        """Activate on-demand tools by name, search keyword, or MCP server id.

        Returns the set of newly activated names and the corresponding tool
        definitions, without mutating view state. Only descriptors that
        are currently on-demand (relative to the caller's ``activated`` set)
        AND that the wrapped policy provider would actually advertise for
        ``can_confirm`` are eligible. This avoids marking a tool as activated
        when policy filtering would still hide it from the LLM in this turn.
        """
        activated_frozen = self._freeze_activated(activated)
        descriptors = await self._ensure_descriptors()
        candidates: set[str] = set()

        if names:
            for name in names:
                matching = [
                    d
                    for d in descriptors
                    if d.name == name and self._is_on_demand(d, activated_frozen)
                ]
                if matching:
                    candidates.add(name)
                else:
                    logger.warning(
                        "activate_tools: unknown or non-on-demand tool %r", name
                    )

        if search:
            search_lower = search.lower()
            for descriptor in descriptors:
                if not self._is_on_demand(descriptor, activated_frozen):
                    continue
                summary = descriptor.summary or ""
                if (
                    search_lower in descriptor.name.lower()
                    or search_lower in summary.lower()
                ):
                    candidates.add(descriptor.name)

        # Direct activation by MCP server id. Skills that wrap a whole server
        # use this so they don't have to enumerate the server's tool names
        # (which depend on the operator's installed MCP server).
        requested_servers = set(mcp_server_ids) if mcp_server_ids else set()
        if requested_servers:
            seen_servers: set[str] = set()
            for descriptor in descriptors:
                server_id = descriptor.mcp_server_id
                if (
                    server_id is not None
                    and server_id in requested_servers
                    and self._is_on_demand(descriptor, activated_frozen)
                ):
                    candidates.add(descriptor.name)
                    seen_servers.add(server_id)
            unknown_servers = requested_servers - seen_servers
            for server_id in unknown_servers:
                logger.warning(
                    "activate_tools: unknown or non-on-demand MCP server %r",
                    server_id,
                )

        # Expand to other on-demand tools on the same MCP server so the LLM
        # gets the whole server in one shot. Eager tools on the same server
        # stay where they are.
        mcp_servers_to_activate: set[str] = {
            d.mcp_server_id
            for d in descriptors
            if d.name in candidates and d.mcp_server_id
        }
        for descriptor in descriptors:
            if (
                descriptor.mcp_server_id in mcp_servers_to_activate
                and self._is_on_demand(descriptor, activated_frozen)
            ):
                candidates.add(descriptor.name)

        if not candidates:
            return OnDemandActivationResult(
                newly_activated=_EMPTY_ACTIVATED, definitions=[]
            )

        # Intersect with what the wrapped provider will actually advertise for
        # this interaction. Confirm-required tools in a non-confirmable context,
        # for example, must not be reported as active because they would never
        # appear in the advertised tool list.
        wrapped_defs = await self._fetch_wrapped_definitions(can_confirm=can_confirm)
        returned_by_name: dict[str, ToolDefinition] = {
            name: defn
            for defn in wrapped_defs
            if (name := defn.get("function", {}).get("name")) is not None
        }
        actually_activatable = candidates & returned_by_name.keys()
        if not actually_activatable:
            return OnDemandActivationResult(
                newly_activated=_EMPTY_ACTIVATED, definitions=[]
            )

        logger.info("Activated on-demand tools: %s", sorted(actually_activatable))
        return OnDemandActivationResult(
            newly_activated=frozenset(actually_activatable),
            definitions=[returned_by_name[name] for name in actually_activatable],
        )
