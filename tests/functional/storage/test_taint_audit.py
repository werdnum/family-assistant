"""Functional tests for durable runtime taint audit events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.storage.database import Database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_tool_call_review_fields_round_trip(
    db_engine: AsyncEngine,
) -> None:
    writer = Database(engine=db_engine)
    await writer.taint_audit_events.add(
        event_id="review-audit-round-trip",
        event_type="tool_call_review",
        conversation_id="review-conversation",
        turn_id="review-turn",
        processing_profile_id="default_assistant",
        subconversation_id=None,
        tool_name="delete_calendar_event",
        tool_call_id="review-call",
        sink_class="destructive_artifact_write",
        max_tier="trusted_user",
        sources=[],
        requested_outcome="review",
        effective_outcome="allow",
        mode="enforce",
        reason="The requested deletion matches the user's instruction.",
        arguments_summary={"keys": ["uid"], "value_types": {"uid": "str"}},
        review_verdict="allow",
        review_status="completed",
        review_latency_ms=37.25,
        review_context={
            "delegating_contexts": ["static:profile:destructive-tools"],
            "allowed_verdicts": ["allow", "confirm", "deny"],
            "fallback_verdict": "confirm",
            "used_fallback": False,
            "destination_echo": None,
        },
    )

    reader = Database(engine=db_engine)
    events = await reader.taint_audit_events.list_for_turn("review-turn")

    assert len(events) == 1
    event = events[0]
    assert event["review_verdict"] == "allow"
    assert event["review_status"] == "completed"
    assert event["review_latency_ms"] == pytest.approx(37.25)
    assert event["review_context_json"] == {
        "delegating_contexts": ["static:profile:destructive-tools"],
        "allowed_verdicts": ["allow", "confirm", "deny"],
        "fallback_verdict": "confirm",
        "used_fallback": False,
        "destination_echo": None,
    }


@pytest.mark.asyncio
async def test_tool_call_review_escalation_event_round_trip(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await db.taint_audit_events.add(
        event_id="review-escalation-round-trip",
        event_type="tool_call_review_escalation",
        conversation_id="review-conversation",
        turn_id="review-escalation-turn",
        processing_profile_id="default_assistant",
        subconversation_id=None,
        tool_name="send_message",
        tool_call_id="denied-call",
        sink_class="arbitrary_external_message",
        max_tier="unknown_external",
        sources=[],
        requested_outcome="review_escalation",
        effective_outcome="deny",
        mode="enforce",
        reason="Denial threshold reserved; turn terminated.",
        arguments_summary={"keys": [], "value_types": {}},
        review_verdict="deny",
        review_status="escalation_turn_terminated",
        review_context={
            "delegating_contexts": ["denial_threshold"],
            "allowed_verdicts": ["allow", "confirm", "deny"],
            "fallback_verdict": "confirm",
            "used_fallback": False,
            "destination_echo": None,
        },
    )

    events = await db.taint_audit_events.list_for_turn("review-escalation-turn")

    assert len(events) == 1
    assert events[0]["event_type"] == "tool_call_review_escalation"
    assert events[0]["review_status"] == "escalation_turn_terminated"
    assert events[0]["requested_outcome"] == "review_escalation"
