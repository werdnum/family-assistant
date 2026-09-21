from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError

from family_assistant.config_models import DefaultProfileSettings, ServiceProfile
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    DEFAULT_POLICY_PRIORITY_OFFSET,
    OPERATOR_POLICY_PRIORITY_OFFSET,
    PROFILE_POLICY_PRIORITY_OFFSET,
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)


def make_descriptor(
    *,
    name: str,
    tags: set[ToolTag],
    origin: Literal["local", "mcp"] = "local",
    mcp_server_id: str | None = None,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} description",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=frozenset(tags),
        origin=origin,
        mcp_server_id=mcp_server_id,
    )


def test_empty_matcher_matches_nothing() -> None:
    descriptor = make_descriptor(
        name="get_note",
        tags={ToolTag.READ_ONLY, ToolTag.NOTES, ToolTag.SENSITIVE_DATA},
    )

    assert ToolMatcher().matches(descriptor) is False


def test_matcher_supports_name_globs_and_tag_filters() -> None:
    descriptor = make_descriptor(
        name="delete_note",
        tags={
            ToolTag.NOTES,
            ToolTag.SENSITIVE_DATA,
            ToolTag.DESTRUCTIVE,
            ToolTag.STATE_CHANGING,
        },
    )

    matcher = ToolMatcher(
        names=["delete_*"],
        tags_all=[ToolTag.NOTES, ToolTag.DESTRUCTIVE],
        tags_any=[ToolTag.STATE_CHANGING, ToolTag.CALENDAR],
    )

    assert matcher.matches(descriptor) is True


def test_matcher_supports_mcp_server_ids_and_wildcard() -> None:
    mcp_descriptor = make_descriptor(
        name="search_web",
        tags={ToolTag.BROWSER, ToolTag.OUTPUT_UNTRUSTED},
        origin="mcp",
        mcp_server_id="brave",
    )
    local_descriptor = make_descriptor(
        name="search_documents",
        tags={ToolTag.DOCUMENTS, ToolTag.READ_ONLY, ToolTag.SENSITIVE_DATA},
    )

    assert ToolMatcher(mcp_server_ids=["brave"]).matches(mcp_descriptor) is True
    assert ToolMatcher(mcp_server_ids=["*"]).matches(mcp_descriptor) is True
    assert ToolMatcher(mcp_server_ids=["time"]).matches(mcp_descriptor) is False
    assert ToolMatcher(mcp_server_ids=["*"]).matches(local_descriptor) is False


def test_matcher_supports_argument_equals() -> None:
    descriptor = make_descriptor(
        name="delegate_to_service",
        tags={ToolTag.DELEGATION},
    )

    assert (
        ToolMatcher(
            names=["delegate_to_service"],
            argument_equals={"target_service_id": "engineer_profile"},
        ).matches(
            descriptor,
            {"target_service_id": "engineer_profile"},
        )
        is True
    )
    assert (
        ToolMatcher(
            names=["delegate_to_service"],
            argument_equals={"target_service_id": "engineer_profile"},
        ).matches(
            descriptor,
            {"target_service_id": "default_assistant"},
        )
        is False
    )
    assert (
        ToolMatcher(argument_equals={"target_service_id": "engineer_profile"}).matches(
            descriptor,
            None,
        )
        is False
    )


def test_profile_rules_override_defaults_via_priority_offset() -> None:
    descriptor = make_descriptor(
        name="delete_note",
        tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            default_decision=ToolPolicyDecision.DENY,
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.DENY,
                    priority=50,
                    description="default destructive deny",
                )
            ],
        ),
        profile=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["delete_note"]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=0,
                    description="profile exception",
                )
            ]
        ),
    )

    evaluation = engine.evaluate(descriptor)

    assert evaluation.decision is ToolPolicyDecision.ALLOW
    assert evaluation.reason == "profile exception"
    assert evaluation.matched_rule is not None
    assert evaluation.matched_rule.effective_priority == PROFILE_POLICY_PRIORITY_OFFSET


def test_operator_rules_override_profile_and_defaults() -> None:
    descriptor = make_descriptor(
        name="execute_script",
        tags={ToolTag.CODE_EXECUTION, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.CODE_EXECUTION]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=90,
                )
            ]
        ),
        profile=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["execute_script"]),
                    decision=ToolPolicyDecision.CONFIRM,
                    priority=0,
                )
            ]
        ),
        operator=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.CODE_EXECUTION]),
                    decision=ToolPolicyDecision.DENY,
                    priority=0,
                    description="operator hard block",
                )
            ]
        ),
    )

    evaluation = engine.evaluate(descriptor)

    assert evaluation.decision is ToolPolicyDecision.DENY
    assert evaluation.reason == "operator hard block"
    assert evaluation.matched_rule is not None
    assert evaluation.matched_rule.effective_priority == OPERATOR_POLICY_PRIORITY_OFFSET


def test_rule_order_breaks_ties_within_same_priority() -> None:
    descriptor = make_descriptor(
        name="modify_calendar_event",
        tags={ToolTag.CALENDAR, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["modify_calendar_event"]),
                    decision=ToolPolicyDecision.CONFIRM,
                    priority=20,
                    description="first rule wins",
                ),
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.CALENDAR]),
                    decision=ToolPolicyDecision.DENY,
                    priority=20,
                    description="second rule loses tie",
                ),
            ]
        )
    )

    evaluation = engine.evaluate(descriptor)

    assert evaluation.decision is ToolPolicyDecision.CONFIRM
    assert evaluation.reason == "first rule wins"


def test_default_decision_uses_profile_then_operator_then_defaults() -> None:
    descriptor = make_descriptor(
        name="query_recent_events",
        tags={ToolTag.DATA, ToolTag.READ_ONLY},
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(default_decision=ToolPolicyDecision.DENY),
        profile=ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW),
        operator=ToolPolicyConfig(default_decision=ToolPolicyDecision.CONFIRM),
    )

    evaluation = engine.evaluate(descriptor)

    assert evaluation.decision is ToolPolicyDecision.ALLOW
    assert evaluation.reason == "no matching rule (default)"


def test_confirmation_rules_are_hidden_without_confirmation_capability() -> None:
    descriptor = make_descriptor(
        name="delete_calendar_event",
        tags={ToolTag.CALENDAR, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            default_decision=ToolPolicyDecision.DENY,
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.CONFIRM,
                    description="destructive operations require approval",
                )
            ],
        )
    )

    advertised = engine.evaluate_for_advertisement(descriptor, can_confirm=False)
    executable = engine.evaluate_for_execution(descriptor, can_confirm=False)

    assert advertised.decision is ToolPolicyDecision.DENY
    assert (
        "confirmation required but unavailable for advertisement" in advertised.reason
    )
    assert executable.decision is ToolPolicyDecision.DENY
    assert "confirmation required but unavailable for execution" in executable.reason


def test_confirmation_rules_remain_confirm_when_confirmation_is_available() -> None:
    descriptor = make_descriptor(
        name="delete_calendar_event",
        tags={ToolTag.CALENDAR, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.CONFIRM,
                    description="destructive operations require approval",
                )
            ],
            default_decision=ToolPolicyDecision.DENY,
        )
    )

    advertised = engine.evaluate_for_advertisement(descriptor, can_confirm=True)
    executable = engine.evaluate_for_execution(descriptor, can_confirm=True)

    assert advertised.decision is ToolPolicyDecision.CONFIRM
    assert executable.decision is ToolPolicyDecision.CONFIRM


def test_review_rules_remain_review_without_confirmation_capability() -> None:
    descriptor = make_descriptor(
        name="delete_calendar_event",
        tags={ToolTag.CALENDAR, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.REVIEW,
                    description="destructive operations require review",
                )
            ],
            default_decision=ToolPolicyDecision.DENY,
        )
    )

    advertised = engine.evaluate_for_advertisement(descriptor, can_confirm=False)
    executable = engine.evaluate_for_execution(descriptor, can_confirm=False)

    assert advertised.decision is ToolPolicyDecision.REVIEW
    assert executable.decision is ToolPolicyDecision.REVIEW
    assert advertised.reason == "destructive operations require review"
    assert executable.reason == "destructive operations require review"


def test_review_default_decision_parses_and_survives_confirmation_filter() -> None:
    descriptor = make_descriptor(
        name="unmatched_tool",
        tags={ToolTag.STATE_CHANGING},
    )
    config = ToolPolicyConfig.model_validate({"default_decision": "review"})
    engine = PolicyEngine.from_policy_config(config)

    assert config.default_decision is ToolPolicyDecision.REVIEW
    assert (
        engine.evaluate_for_advertisement(descriptor, can_confirm=False).decision
        is ToolPolicyDecision.REVIEW
    )
    assert (
        engine.evaluate_for_execution(
            descriptor,
            can_confirm=False,
        ).decision
        is ToolPolicyDecision.REVIEW
    )


def test_review_decision_participates_in_layer_priority_resolution() -> None:
    descriptor = make_descriptor(
        name="delete_note",
        tags={ToolTag.NOTES, ToolTag.DESTRUCTIVE, ToolTag.STATE_CHANGING},
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.CONFIRM,
                    priority=99,
                )
            ]
        ),
        profile=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["delete_note"]),
                    decision=ToolPolicyDecision.REVIEW,
                    priority=0,
                    description="profile review rule",
                )
            ]
        ),
        operator=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["other_tool"]),
                    decision=ToolPolicyDecision.DENY,
                )
            ]
        ),
    )

    evaluation = engine.evaluate_for_execution(
        descriptor,
        can_confirm=False,
    )

    assert evaluation.decision is ToolPolicyDecision.REVIEW
    assert evaluation.reason == "profile review rule"
    assert evaluation.matched_rule is not None
    assert evaluation.matched_rule.layer == "profile"
    assert evaluation.matched_rule.effective_priority == PROFILE_POLICY_PRIORITY_OFFSET


def test_config_models_accept_tools_policy() -> None:
    profile = ServiceProfile.model_validate({
        "id": "browser_profile",
        "tools_policy": {
            "default_decision": "deny",
            "rules": [
                {
                    "match": {"tags_any": ["browser"]},
                    "decision": "allow",
                    "priority": 10,
                }
            ],
        },
    })
    defaults = DefaultProfileSettings.model_validate({
        "tools_policy": {"default_decision": "deny", "rules": []},
    })

    assert profile.tools_policy is not None
    assert profile.tools_policy.rules[0].match.tags_any == [ToolTag.BROWSER]
    assert defaults.tools_policy is not None
    assert defaults.tools_policy.default_decision is ToolPolicyDecision.DENY


def test_rules_are_sorted_by_effective_priority_then_layer_then_order() -> None:
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["default_rule"]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=1,
                )
            ]
        ),
        profile=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["profile_rule"]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=1,
                )
            ]
        ),
        operator=ToolPolicyConfig(
            rules=[
                PolicyRule(
                    match=ToolMatcher(names=["operator_rule"]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=1,
                )
            ]
        ),
    )

    assert engine.rules[0].layer == "operator"
    assert engine.rules[0].effective_priority == OPERATOR_POLICY_PRIORITY_OFFSET + 1
    assert engine.rules[1].layer == "profile"
    assert engine.rules[1].effective_priority == PROFILE_POLICY_PRIORITY_OFFSET + 1
    assert engine.rules[2].layer == "defaults"
    assert engine.rules[2].effective_priority == DEFAULT_POLICY_PRIORITY_OFFSET + 1


def test_policy_rule_priority_is_bounded_to_documented_range() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 99"):
        PolicyRule(
            match=ToolMatcher(names=["too_high"]),
            decision=ToolPolicyDecision.ALLOW,
            priority=100,
        )

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        PolicyRule(
            match=ToolMatcher(names=["negative"]),
            decision=ToolPolicyDecision.ALLOW,
            priority=-1,
        )


def test_execution_policy_can_filter_by_tool_arguments() -> None:
    descriptor = make_descriptor(
        name="delegate_to_service",
        tags={ToolTag.DELEGATION},
    )
    engine = PolicyEngine.from_policy_config(
        ToolPolicyConfig(
            default_decision=ToolPolicyDecision.ALLOW,
            rules=[
                PolicyRule(
                    match=ToolMatcher(
                        names=["delegate_to_service"],
                        argument_equals={"target_service_id": "engineer_profile"},
                    ),
                    decision=ToolPolicyDecision.DENY,
                    description="block delegating to engineer profile",
                )
            ],
        )
    )

    blocked = engine.evaluate_for_execution(
        descriptor,
        arguments={"target_service_id": "engineer_profile"},
        can_confirm=True,
    )
    allowed = engine.evaluate_for_execution(
        descriptor,
        arguments={"target_service_id": "default_assistant"},
        can_confirm=True,
    )
    advertised = engine.evaluate_for_advertisement(descriptor, can_confirm=True)

    assert blocked.decision is ToolPolicyDecision.DENY
    assert blocked.reason == "block delegating to engineer profile"
    assert allowed.decision is ToolPolicyDecision.ALLOW
    assert advertised.decision is ToolPolicyDecision.ALLOW


def test_argument_equals_missing_key_does_not_match() -> None:
    """A rule with argument_equals must not match when the key is absent from arguments."""
    descriptor = make_descriptor(name="delegate_to_service", tags={ToolTag.DELEGATION})
    matcher = ToolMatcher(
        names=["delegate_to_service"],
        argument_equals={"target_service_id": None},
    )
    # Key absent — should NOT match even though expected value is None
    assert not matcher.matches(descriptor, arguments={})
    # Key present with None — should match
    assert matcher.matches(descriptor, arguments={"target_service_id": None})
    # Key present with different value — should not match
    assert not matcher.matches(descriptor, arguments={"target_service_id": "foo"})


def test_argument_equals_type_strict_comparison() -> None:
    """argument_equals must be type-strict: True != 1 and False != 0."""
    descriptor = make_descriptor(name="my_tool", tags={ToolTag.DELEGATION})
    bool_matcher = ToolMatcher(
        names=["my_tool"],
        argument_equals={"flag": True},
    )
    int_matcher = ToolMatcher(
        names=["my_tool"],
        argument_equals={"flag": 1},
    )
    # True should not match 1
    assert bool_matcher.matches(descriptor, arguments={"flag": True})
    assert not bool_matcher.matches(descriptor, arguments={"flag": 1})
    # 1 should not match True
    assert int_matcher.matches(descriptor, arguments={"flag": 1})
    assert not int_matcher.matches(descriptor, arguments={"flag": True})
