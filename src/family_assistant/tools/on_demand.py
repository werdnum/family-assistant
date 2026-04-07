"""On-demand tool activation support.

Provides the ``OnDemandAwareToolsProvider`` wrapper that splits tools into
eager (always-loaded) and on-demand (catalog-only until activated) sets, plus
the ``activate_tools`` meta-tool definition handled by the LLM loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from family_assistant.tools.infrastructure import (
    ToolDescriptorProvider,
    ToolsProvider,
)
from family_assistant.tools.metadata import ToolDescriptor, extract_tool_summary

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )

logger = logging.getLogger(__name__)


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

    def render_for_system_prompt(self) -> str:
        """Render catalog as a system prompt section.

        Returns empty string when there are no on-demand tools.
        """
        if not self.entries:
            return ""
        lines = [
            "## On-Demand Tools",
            "The following tools are available but not yet active. "
            "Call `activate_tools` with their names to enable them:",
        ]
        for entry in self.entries:
            lines.append(f"- **{entry.name}**: {entry.summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# activate_tools meta-tool definition
# ---------------------------------------------------------------------------

ACTIVATE_TOOLS_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "activate_tools",
        "description": (
            "Activate on-demand tools so you can use them in this conversation. "
            "Call with specific tool names from the on-demand catalog, or use "
            "the search parameter to find tools by keyword."
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
            },
        },
    },
}


# ---------------------------------------------------------------------------
# OnDemandAwareToolsProvider
# ---------------------------------------------------------------------------


class OnDemandAwareToolsProvider:
    """Wraps a provider to split tools into eager (always-loaded) and on-demand sets.

    On-demand tools are fully executable at all times — activation only controls
    whether their full JSON schema is included in the LLM tool list. This keeps
    context usage low while allowing the agent to activate tools when needed.

    This provider implements ``ToolDescriptorProvider`` so it can be wrapped by
    ``PolicyEnforcingToolsProvider``.
    """

    def __init__(
        self,
        wrapped_provider: ToolsProvider,
        on_demand_tool_names: set[str],
        on_demand_mcp_server_ids: set[str] | None = None,
    ) -> None:
        if not isinstance(wrapped_provider, ToolDescriptorProvider):
            msg = (
                "OnDemandAwareToolsProvider requires a wrapped provider that "
                "supports tool descriptors."
            )
            raise ValueError(msg)
        self._wrapped_provider = wrapped_provider
        self._descriptor_provider = wrapped_provider
        self._on_demand_tool_names = on_demand_tool_names
        self._on_demand_mcp_server_ids = on_demand_mcp_server_ids or set()
        self._activated_tool_names: set[str] = set()
        # Caches (populated on first access)
        self._all_descriptors: list[ToolDescriptor] | None = None
        self._all_definitions: list[ToolDefinition] | None = None
        self._catalog: OnDemandToolCatalog | None = None

    @property
    def wrapped_provider(self) -> ToolsProvider:
        """Return the wrapped provider."""
        return self._wrapped_provider

    def has_on_demand_tools(self) -> bool:
        """Return True if there are any on-demand tool names configured."""
        return bool(self._on_demand_tool_names) or bool(self._on_demand_mcp_server_ids)

    # --- Internal helpers ---

    async def _ensure_caches(self) -> None:
        """Populate caches from the wrapped provider on first access."""
        if self._all_descriptors is not None:
            return
        self._all_descriptors = await self._descriptor_provider.get_tool_descriptors()
        self._all_definitions = await self._wrapped_provider.get_tool_definitions()

    def _is_on_demand(self, descriptor: ToolDescriptor) -> bool:
        """Check if a descriptor is on-demand (and not yet activated)."""
        if descriptor.name in self._activated_tool_names:
            return False
        if descriptor.name in self._on_demand_tool_names:
            return True
        return bool(
            descriptor.mcp_server_id
            and descriptor.mcp_server_id in self._on_demand_mcp_server_ids
        )

    def _is_on_demand_by_name(self, name: str) -> bool:
        """Check if a tool name is on-demand (without requiring a descriptor)."""
        return (
            name in self._on_demand_tool_names
            and name not in self._activated_tool_names
        )

    # --- ToolsProvider interface ---

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return eager + activated definitions, plus ``activate_tools`` if needed."""
        await self._ensure_caches()
        assert self._all_definitions is not None
        assert self._all_descriptors is not None

        # Build set of on-demand (not yet activated) names
        on_demand_names = {
            d.name for d in self._all_descriptors if self._is_on_demand(d)
        }

        eager_and_activated = [
            defn
            for defn in self._all_definitions
            if defn.get("function", {}).get("name") not in on_demand_names
        ]
        if on_demand_names:
            return [*eager_and_activated, ACTIVATE_TOOLS_DEFINITION]
        return eager_and_activated

    async def get_system_prompt_addition(self) -> str | None:
        """Return the on-demand catalog rendered for system prompt injection."""
        if not self.has_on_demand_tools():
            return None
        catalog = await self.get_on_demand_catalog()
        if not catalog.entries:
            return None
        return catalog.render_for_system_prompt()

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        """Execute any tool (eager or on-demand). On-demand tools work without activation."""
        return await self._wrapped_provider.execute_tool(
            name, arguments, context, call_id
        )

    async def close(self) -> None:
        """Clean up resources."""
        await self._wrapped_provider.close()

    # --- ToolDescriptorProvider interface ---

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        """Return ALL tool descriptors (eager + on-demand). Policy layer uses these."""
        await self._ensure_caches()
        assert self._all_descriptors is not None
        return self._all_descriptors

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        """Return descriptor by name."""
        return await self._descriptor_provider.get_tool_descriptor(name)

    # --- On-demand specific ---

    async def get_on_demand_catalog(self) -> OnDemandToolCatalog:
        """Return catalog of on-demand tools (not yet activated) for system prompt."""
        await self._ensure_caches()
        assert self._all_descriptors is not None

        entries = [
            OnDemandCatalogEntry(
                name=descriptor.name,
                summary=descriptor.summary
                or extract_tool_summary(descriptor.definition),
            )
            for descriptor in self._all_descriptors
            if self._is_on_demand(descriptor)
        ]
        return OnDemandToolCatalog(entries=entries)

    async def activate_tools(
        self,
        *,
        names: list[str] | None = None,
        search: str | None = None,
    ) -> list[ToolDefinition]:
        """Activate on-demand tools by name or search keyword.

        Returns the full definitions of newly activated tools.
        """
        await self._ensure_caches()
        assert self._all_descriptors is not None
        assert self._all_definitions is not None

        to_activate: set[str] = set()

        if names:
            for name in names:
                # Check it exists in our on-demand set
                matching = [d for d in self._all_descriptors if d.name == name]
                if matching:
                    to_activate.add(name)
                else:
                    logger.warning("activate_tools: unknown tool %r", name)

        if search:
            search_lower = search.lower()
            for descriptor in self._all_descriptors:
                if (
                    not self._is_on_demand(descriptor)
                    and descriptor.name not in to_activate
                ):
                    continue
                summary = descriptor.summary or ""
                if (
                    search_lower in descriptor.name.lower()
                    or search_lower in summary.lower()
                ):
                    to_activate.add(descriptor.name)

        # Mark as activated
        newly_activated = to_activate - self._activated_tool_names
        self._activated_tool_names.update(newly_activated)

        if newly_activated:
            logger.info("Activated on-demand tools: %s", sorted(newly_activated))

        # Also activate all tools from the same MCP server
        mcp_servers_to_activate: set[str] = set()
        for descriptor in self._all_descriptors:
            if descriptor.name in newly_activated and descriptor.mcp_server_id:
                mcp_servers_to_activate.add(descriptor.mcp_server_id)
        if mcp_servers_to_activate:
            for descriptor in self._all_descriptors:
                if (
                    descriptor.mcp_server_id in mcp_servers_to_activate
                    and descriptor.name not in self._activated_tool_names
                ):
                    self._activated_tool_names.add(descriptor.name)
                    newly_activated.add(descriptor.name)

        # Return definitions for newly activated tools
        return [
            defn
            for defn in self._all_definitions
            if defn.get("function", {}).get("name") in newly_activated
        ]

    def reset_activations(self) -> None:
        """Reset all activations (for new conversation turns)."""
        self._activated_tool_names.clear()
        self._catalog = None
