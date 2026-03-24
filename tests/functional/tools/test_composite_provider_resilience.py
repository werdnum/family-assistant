"""Test that CompositeToolsProvider is resilient to individual provider failures.

When one provider in the composite (e.g., an MCP server like Home Assistant) crashes
during tool discovery, the remaining providers' tools should still be available.
This prevents a single failing connection from "blinding" the entire assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.infrastructure import ToolsProvider
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from family_assistant.tools.metadata import (
        ToolDescriptor,
    )
    from family_assistant.tools.types import (
        ToolDefinition,
        ToolExecutionContext,
        ToolResult,
    )


class ExplodingToolsProvider(ToolsProvider):
    """A provider that raises on every operation, simulating a crashed connection."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or ConnectionError("No address associated with hostname")

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        raise self._error

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        raise self._error

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        raise self._error

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        raise self._error

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_composite_provider_survives_crashing_provider_in_full_stack() -> None:
    """When one provider crashes, the full tool discovery stack still works.

    This reproduces the scenario where a Home Assistant MCP connection fails,
    which should NOT prevent local tools like get_note from being discovered.
    The test exercises the full chain: CompositeToolsProvider -> PolicyEnforcingToolsProvider.
    """
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    exploding_provider = ExplodingToolsProvider()

    composite = CompositeToolsProvider(providers=[local_provider, exploding_provider])

    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )

    definitions = await policy_provider.get_tool_definitions()
    tool_names = [d["function"]["name"] for d in definitions]

    assert "get_note" in tool_names, (
        "Local tools should be available even when another provider crashes"
    )
    assert "delegate_to_service" in tool_names, (
        "All local tools should survive a provider crash"
    )


@pytest.mark.asyncio
async def test_composite_provider_descriptors_survive_crashing_provider() -> None:
    """get_tool_descriptors should not crash when one provider fails."""
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    exploding_provider = ExplodingToolsProvider()

    composite = CompositeToolsProvider(providers=[local_provider, exploding_provider])

    descriptors = await composite.get_tool_descriptors()
    descriptor_names = {d.name for d in descriptors}

    assert "get_note" in descriptor_names
    assert "delegate_to_service" in descriptor_names


@pytest.mark.asyncio
async def test_composite_provider_single_descriptor_survives_crashing_provider() -> (
    None
):
    """get_tool_descriptor should not crash when one provider fails.

    The exploding provider must come FIRST so the composite actually hits the
    exception path before falling through to the local provider.
    """
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    exploding_provider = ExplodingToolsProvider()

    composite = CompositeToolsProvider(providers=[exploding_provider, local_provider])

    descriptor = await composite.get_tool_descriptor("get_note")
    assert descriptor is not None
    assert descriptor.name == "get_note"
