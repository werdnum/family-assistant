"""Unit tests for global_tools_policy injection into every profile's engine.

``global_tools_policy`` makes a tool available in all contexts by injecting its
rules into each profile's policy engine, regardless of the profile's own
``tools_policy`` (which otherwise replaces the shipped defaults wholesale).
Operator policy still overrides global rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.assistant import (
    _build_profile_policy_engine,  # noqa: PLC2701 - intentionally testing this private helper; smallest unit covering global_tools_policy injection without a full Assistant
)
from family_assistant.tools.metadata import ToolTag, build_tool_descriptor
from family_assistant.tools.policy import (
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition

_TOOL_NAME = "report_technical_problem"

_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": "Report a technical problem.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_DESCRIPTOR = build_tool_descriptor(
    _DEFINITION,
    frozenset({ToolTag.STATE_CHANGING}),
    origin="local",
)

_GLOBAL_POLICY = ToolPolicyConfig(
    rules=[
        PolicyRule(
            match=ToolMatcher(names=[_TOOL_NAME]),
            decision=ToolPolicyDecision.ALLOW,
            priority=50,
        )
    ]
)

# A deny-by-default profile that does not mention the tool at all -- models a
# restrictive profile like email_intake or browser_profile.
_RESTRICTIVE_PROFILE_POLICY = ToolPolicyConfig(
    default_decision=ToolPolicyDecision.DENY,
    rules=[
        PolicyRule(
            match=ToolMatcher(names=["some_other_tool"]),
            decision=ToolPolicyDecision.ALLOW,
            priority=10,
        )
    ],
)


def test_global_policy_allows_tool_in_restrictive_profile() -> None:
    """The global allow rule applies even when the profile denies by default."""
    engine = _build_profile_policy_engine(
        "restrictive",
        _RESTRICTIVE_PROFILE_POLICY,
        None,
        _GLOBAL_POLICY,
    )
    assert engine.evaluate(_DESCRIPTOR).decision == ToolPolicyDecision.ALLOW


def test_without_global_policy_restrictive_profile_denies_tool() -> None:
    """Sanity check: without the global policy the profile denies the tool."""
    engine = _build_profile_policy_engine(
        "restrictive",
        _RESTRICTIVE_PROFILE_POLICY,
        None,
        None,
    )
    assert engine.evaluate(_DESCRIPTOR).decision == ToolPolicyDecision.DENY


def test_operator_policy_overrides_global_allow() -> None:
    """An operator-layer deny still wins over a global allow rule."""
    operator_policy = ToolPolicyConfig(
        rules=[
            PolicyRule(
                match=ToolMatcher(names=[_TOOL_NAME]),
                decision=ToolPolicyDecision.DENY,
                priority=10,
            )
        ]
    )
    engine = _build_profile_policy_engine(
        "restrictive",
        _RESTRICTIVE_PROFILE_POLICY,
        operator_policy,
        _GLOBAL_POLICY,
    )
    assert engine.evaluate(_DESCRIPTOR).decision == ToolPolicyDecision.DENY
