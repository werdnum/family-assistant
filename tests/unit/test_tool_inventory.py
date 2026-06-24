"""Tests for the per-profile tool inventory introspection helper.

Uses the real ``LocalToolsProvider`` / ``PolicyEnforcingToolsProvider`` /
``OnDemandToolsView`` stack so the eager-vs-on-demand partition is exercised
against the actual provider behaviour, plus a small fake for the MCP-source
attribution branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.tool_inventory import build_tool_inventory
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    OnDemandToolsView,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


def _notes_policy_provider() -> PolicyEnforcingToolsProvider:
    return PolicyEnforcingToolsProvider(
        wrapped_provider=LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS),
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                ],
            )
        ),
    )


@pytest.mark.anyio
async def test_inventory_without_on_demand_view_is_all_eager() -> None:
    provider = _notes_policy_provider()
    all_definitions = await provider.get_tool_definitions(can_confirm=True)

    inventory = await build_tool_inventory(
        tools_provider=provider,
        on_demand_view=None,
        profile_id="notes_profile",
    )

    assert inventory.profile_id == "notes_profile"
    assert inventory.has_on_demand_view is False
    assert inventory.activate_tools_present is False
    assert inventory.on_demand.count == 0
    assert inventory.eager.count == len(all_definitions)
    # The headline per-turn figure is the eager token estimate.
    assert inventory.advertised_per_turn_tokens == inventory.eager.estimated_tokens
    assert inventory.all_if_activated_tokens == inventory.eager.estimated_tokens
    # All advertised local tools are attributed to the "local" source.
    assert [b.source for b in inventory.by_source] == ["local"]


@pytest.mark.anyio
async def test_inventory_partitions_eager_and_on_demand() -> None:
    provider = _notes_policy_provider()
    all_names = [
        defn["function"]["name"]
        for defn in await provider.get_tool_definitions(can_confirm=True)
    ]
    assert len(all_names) >= 2, "need at least two notes tools to partition"
    hidden = all_names[0]

    on_demand_view = OnDemandToolsView(
        wrapped_provider=provider,
        on_demand_tool_names={hidden},
    )

    inventory = await build_tool_inventory(
        tools_provider=provider,
        on_demand_view=on_demand_view,
        profile_id="notes_profile",
    )

    eager_names = {entry.name for entry in inventory.eager.tools}
    on_demand_names = {entry.name for entry in inventory.on_demand.tools}

    assert on_demand_names == {hidden}
    assert hidden not in eager_names
    # Every other allowed tool stays eager; activate_tools is injected.
    assert eager_names == (set(all_names) - {hidden}) | {"activate_tools"}
    assert inventory.activate_tools_present is True
    assert inventory.has_on_demand_view is True

    # activate_tools is attributed to the synthetic "meta" source.
    meta_sources = {b.source for b in inventory.by_source}
    assert "meta" in meta_sources
    assert "local" in meta_sources

    # The on-demand catalog is rendered into the system prompt every turn, so the
    # per-turn total includes it on top of the eager tool definitions.
    assert inventory.on_demand_catalog_prompt.estimated_tokens > 0
    assert inventory.advertised_per_turn_tokens == (
        inventory.eager.estimated_tokens
        + inventory.on_demand_catalog_prompt.estimated_tokens
    )
    # Worst-case (all activated) accounts for every tool definition; the catalog
    # disappears as tools activate, so it is not added there.
    assert inventory.all_if_activated_tokens == (
        inventory.eager.estimated_tokens + inventory.on_demand.estimated_tokens
    )


class _FakeMcpProvider:
    """Minimal provider exposing one local and one MCP tool for source tests."""

    def __init__(self) -> None:
        self._definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {
                    "name": "local_tool",
                    "description": "x",
                    "parameters": {},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remote_tool",
                    "description": "y",
                    "parameters": {},
                },
            },
        ]

    async def get_tool_definitions(
        self, *, can_confirm: bool = True
    ) -> list[ToolDefinition]:
        _ = can_confirm
        return self._definitions

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="local_tool",
                definition=self._definitions[0],
                tags=frozenset(),
                origin="local",
            ),
            ToolDescriptor(
                name="remote_tool",
                definition=self._definitions[1],
                tags=frozenset(),
                origin="mcp",
                mcp_server_id="playwright",
            ),
        ]


@pytest.mark.anyio
async def test_inventory_attributes_mcp_source() -> None:
    inventory = await build_tool_inventory(
        tools_provider=_FakeMcpProvider(),
        on_demand_view=None,
        profile_id="mixed",
    )

    sources = {entry.name: entry.source for entry in inventory.eager.tools}
    assert sources["local_tool"] == "local"
    assert sources["remote_tool"] == "mcp:playwright"

    by_source = {b.source: b for b in inventory.by_source}
    assert by_source["mcp:playwright"].eager_count == 1
    assert by_source["local"].eager_count == 1
    assert inventory.source_name_collisions == []


class _CollidingProvider:
    """Provider where a local and an MCP tool share the name ``dup``."""

    def __init__(self) -> None:
        self._definitions: list[ToolDefinition] = [
            {
                "type": "function",
                "function": {"name": "dup", "description": "x", "parameters": {}},
            },
        ]

    async def get_tool_definitions(
        self, *, can_confirm: bool = True
    ) -> list[ToolDefinition]:
        _ = can_confirm
        return self._definitions

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="dup",
                definition=self._definitions[0],
                tags=frozenset(),
                origin="local",
            ),
            ToolDescriptor(
                name="dup",
                definition=self._definitions[0],
                tags=frozenset(),
                origin="mcp",
                mcp_server_id="playwright",
            ),
        ]


@pytest.mark.anyio
async def test_inventory_flags_source_name_collision() -> None:
    inventory = await build_tool_inventory(
        tools_provider=_CollidingProvider(),
        on_demand_view=None,
        profile_id="dup",
    )

    assert inventory.source_name_collisions == ["dup"]
    sources = {entry.name: entry.source for entry in inventory.eager.tools}
    assert sources["dup"] == "ambiguous"
