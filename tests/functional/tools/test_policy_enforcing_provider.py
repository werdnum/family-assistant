from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.metadata import ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition


def _names(definitions: list[ToolDefinition]) -> list[str]:
    return [definition["function"]["name"] for definition in definitions]


@pytest.mark.asyncio
async def test_real_local_catalog_is_filtered_by_policy_for_advertisement() -> None:
    local_provider = LocalToolsProvider(registrations=LOCAL_TOOL_REGISTRATIONS)
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=local_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                        decision=ToolPolicyDecision.ALLOW,
                        priority=10,
                    ),
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                        decision=ToolPolicyDecision.CONFIRM,
                        priority=20,
                    ),
                ],
            )
        ),
    )

    without_confirm = await policy_provider.get_tool_definitions(can_confirm=False)
    with_confirm = await policy_provider.get_tool_definitions(can_confirm=True)

    assert "get_note" in _names(without_confirm)
    assert "delete_note" not in _names(without_confirm)
    assert "delete_note" in _names(with_confirm)
