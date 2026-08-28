from __future__ import annotations

from family_assistant.tools import LOCAL_TOOL_DESCRIPTORS
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)


def make_mcp_descriptor(
    name: str, *, server_id: str, tags: set[ToolTag]
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
        origin="mcp",
        mcp_server_id=server_id,
    )


def test_real_local_catalog_respects_tag_and_name_based_policy() -> None:
    descriptor_map = {
        descriptor.name: descriptor for descriptor in LOCAL_TOOL_DESCRIPTORS
    }
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
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
    )

    get_note_result = engine.evaluate_for_advertisement(
        descriptor_map["get_note"],
        can_confirm=False,
    )
    delete_note_result = engine.evaluate_for_advertisement(
        descriptor_map["delete_note"],
        can_confirm=False,
    )
    add_note_result = engine.evaluate_for_execution(
        descriptor_map["add_or_update_note"],
        can_confirm=True,
    )

    assert get_note_result.decision is ToolPolicyDecision.ALLOW
    assert delete_note_result.decision is ToolPolicyDecision.DENY
    assert add_note_result.decision is ToolPolicyDecision.ALLOW


def test_mixed_local_and_mcp_descriptors_use_same_policy_engine() -> None:
    local_descriptor = next(
        descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if descriptor.name == "get_note"
    )
    mcp_descriptor = make_mcp_descriptor(
        "search_web",
        server_id="brave",
        tags={ToolTag.BROWSER, ToolTag.OUTPUT_UNTRUSTED},
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            default_decision=ToolPolicyDecision.DENY,
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.NOTES]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=10,
                ),
                PolicyRule(
                    match=ToolMatcher(mcp_server_ids=["brave"]),
                    decision=ToolPolicyDecision.ALLOW,
                    priority=10,
                ),
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.OUTPUT_UNTRUSTED]),
                    decision=ToolPolicyDecision.CONFIRM,
                    priority=15,
                ),
            ],
        )
    )

    local_result = engine.evaluate_for_advertisement(
        local_descriptor, can_confirm=False
    )
    mcp_result_with_confirm = engine.evaluate_for_advertisement(
        mcp_descriptor,
        can_confirm=True,
    )
    mcp_result_without_confirm = engine.evaluate_for_advertisement(
        mcp_descriptor,
        can_confirm=False,
    )

    assert local_result.decision is ToolPolicyDecision.ALLOW
    assert mcp_result_with_confirm.decision is ToolPolicyDecision.CONFIRM
    assert mcp_result_without_confirm.decision is ToolPolicyDecision.DENY


def test_review_tools_stay_advertised_without_confirmation_capability() -> None:
    descriptor = next(
        descriptor
        for descriptor in LOCAL_TOOL_DESCRIPTORS
        if descriptor.name == "delete_note"
    )
    engine = PolicyEngine.from_layers(
        defaults=ToolPolicyConfig(
            default_decision=ToolPolicyDecision.DENY,
            rules=[
                PolicyRule(
                    match=ToolMatcher(tags_any=[ToolTag.DESTRUCTIVE]),
                    decision=ToolPolicyDecision.REVIEW,
                    priority=20,
                )
            ],
        )
    )

    advertised = engine.evaluate_for_advertisement(
        descriptor,
        can_confirm=False,
    )
    executable = engine.evaluate_for_execution(
        descriptor,
        can_confirm=False,
    )

    assert advertised.decision is ToolPolicyDecision.REVIEW
    assert executable.decision is ToolPolicyDecision.REVIEW
