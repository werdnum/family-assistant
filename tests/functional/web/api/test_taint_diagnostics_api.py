"""Functional tests for runtime taint diagnostics."""

from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import (
    Database,
    set_engine_history_taint_epoch,
)
from family_assistant.storage.message_history import message_history_table


def _counts_by_key(items: list[dict[str, object]]) -> dict[str | None, int]:
    """Convert a diagnostics count list into a lookup for assertions."""
    return {
        cast("str | None", item["key"]): cast("int", item["count"]) for item in items
    }


@pytest.mark.asyncio
async def test_taint_diagnostics_counts_review_escalation_trip_status(
    api_client: httpx.AsyncClient,
    api_db_context: Database,
) -> None:
    await api_db_context.taint_audit_events.add(
        event_id="review-escalation-trip",
        event_type="tool_call_review_escalation",
        conversation_id="diagnostics-escalation",
        turn_id="escalation-turn",
        processing_profile_id="default_assistant",
        subconversation_id=None,
        tool_name="reviewed_tool",
        tool_call_id="denied-call",
        sink_class="arbitrary_external_message",
        max_tier="unknown_external",
        sources=[],
        requested_outcome="review_escalation",
        effective_outcome="deny",
        mode="enforce",
        reason="Denial threshold reserved; unattended turn terminated.",
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

    response = await api_client.get("/api/diagnostics/taint-audit?days=1")

    assert response.status_code == 200
    audit = response.json()["audit"]
    assert _counts_by_key(audit["by_event_type"])["tool_call_review_escalation"] == 1
    assert _counts_by_key(audit["by_review_status"])["escalation_turn_terminated"] == 1
    assert _counts_by_key(audit["by_review_verdict"])["deny"] == 1


@pytest.mark.asyncio
async def test_taint_diagnostics_reports_audits_and_distinct_history_rows(
    api_client: httpx.AsyncClient,
    api_db_context: Database,
) -> None:
    """The endpoint aggregates audits and inventories each history row once."""
    await api_db_context.taint_audit_events.add(
        event_id="audit-confirm",
        event_type="policy_evaluation",
        conversation_id="diagnostics-test",
        turn_id="turn-1",
        processing_profile_id="default_assistant",
        subconversation_id=None,
        tool_name="browser_open",
        tool_call_id="call-1",
        sink_class="attacker_addressable_egress",
        max_tier="unknown_external",
        sources=[
            {
                "source_type": "tool_output",
                "source_id": "source-secret",
                "tier": "unknown_external",
                "labels": ["web"],
                "reason": "External web content",
            }
        ],
        requested_outcome="confirm",
        effective_outcome="audit",
        mode="observe",
        reason="Observe mode decision",
        arguments_summary={"keys": ["url"], "value_types": {"url": "str"}},
        review_verdict="confirm",
        review_status="completed",
        review_latency_ms=18.5,
        review_context={
            "delegating_contexts": [
                "taint:unknown_external:attacker_addressable_egress"
            ],
            "allowed_verdicts": ["allow", "confirm", "deny"],
            "fallback_verdict": "confirm",
            "used_fallback": False,
            "destination_echo": False,
        },
    )
    await api_db_context.taint_audit_events.add(
        event_id="audit-result",
        event_type="result_taint",
        conversation_id="diagnostics-test",
        turn_id="turn-1",
        processing_profile_id="default_assistant",
        subconversation_id=None,
        tool_name="browser_open",
        tool_call_id="call-1",
        sink_class=None,
        max_tier="unknown_external",
        sources=[],
        requested_outcome=None,
        effective_outcome=None,
        mode="observe",
        reason="Result raised turn taint",
        arguments_summary=None,
    )

    now = datetime.now(UTC)
    rows = [
        {
            "interface_type": "web",
            "conversation_id": "history-test",
            "timestamp": now,
            "role": "user",
            "content": "trusted",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": {"max_tier": "trusted_user", "sources": []},
            "taint_metadata_version": "runtime_v2",
        },
        {
            "interface_type": "web",
            "conversation_id": "history-test",
            "timestamp": now,
            "role": "tool",
            "content": "external",
            "processing_profile_id": "browser_profile",
            "tool_name": "browser_open",
            "taint_metadata_json": {
                "max_tier": "unknown_external",
                "sources": [],
            },
            "taint_metadata_version": "legacy_inferred",
        },
        {
            "interface_type": "telegram",
            "conversation_id": "history-test",
            "timestamp": now,
            "role": "assistant",
            "content": "legacy missing",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": None,
            "taint_metadata_version": None,
        },
        {
            "interface_type": "telegram",
            "conversation_id": "history-test",
            "timestamp": now,
            "role": "assistant",
            "content": "malformed",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": {"sources": []},
            "taint_metadata_version": "runtime_v2",
        },
        {
            "interface_type": "web",
            "conversation_id": "history-test",
            "timestamp": now,
            "role": "system",
            "content": "system context",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": None,
            "taint_metadata_version": None,
        },
    ]
    await api_db_context.execute(insert(message_history_table).values(rows))

    response = await api_client.get("/api/diagnostics/taint-audit?days=1")

    assert response.status_code == 200
    data = response.json()
    assert data["audit"]["matched_event_count"] == 2
    assert data["audit"]["included_event_count"] == 2
    assert data["audit"]["truncated"] is False
    assert _counts_by_key(data["audit"]["by_event_type"]) == {
        "policy_evaluation": 1,
        "result_taint": 1,
    }
    assert _counts_by_key(data["audit"]["by_requested_outcome"])["confirm"] == 1
    assert _counts_by_key(data["audit"]["by_review_verdict"]) == {
        None: 1,
        "confirm": 1,
    }
    assert _counts_by_key(data["audit"]["by_review_status"]) == {
        None: 1,
        "completed": 1,
    }
    assert _counts_by_key(data["audit"]["source_label_occurrences"]) == {"web": 1}

    assert data["history_taint_epoch"] is None
    history = data["message_history"]
    assert history["pre_epoch_rows"] is None
    assert history["post_epoch_rows"] is None
    assert history["post_epoch_missing_required_metadata_rows"] is None
    assert history["total_rows"] == 5
    assert history["classified_rows"] == 2
    assert history["missing_required_metadata_rows"] == 1
    assert history["malformed_metadata_rows"] == 1
    assert history["not_applicable_rows"] == 1
    assert _counts_by_key(history["by_metadata_version"]) == {
        None: 2,
        "legacy_inferred": 1,
        "runtime_v2": 2,
    }
    assert _counts_by_key(history["by_tool_name"])["browser_open"] == 1
    serialized = response.text
    assert "source-secret" not in serialized
    assert "history-test" not in serialized
    assert "External web content" not in serialized


@pytest.mark.asyncio
async def test_taint_diagnostics_splits_history_rows_by_epoch(
    api_client: httpx.AsyncClient,
    api_db_context: Database,
    db_engine: AsyncEngine,
) -> None:
    """With an epoch configured, history stats split pre/post epoch."""
    epoch = datetime(2026, 7, 6, tzinfo=UTC)
    set_engine_history_taint_epoch(db_engine, epoch)
    pre_epoch = epoch - timedelta(days=3)
    post_epoch = epoch + timedelta(days=3)
    rows = [
        {
            "interface_type": "telegram",
            "conversation_id": "epoch-test",
            "timestamp": pre_epoch,
            "role": "assistant",
            "content": "pre-epoch missing metadata",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": None,
            "taint_metadata_version": None,
        },
        {
            "interface_type": "telegram",
            "conversation_id": "epoch-test",
            "timestamp": pre_epoch,
            "role": "user",
            "content": "pre-epoch classified",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": {"max_tier": "unknown_external", "sources": []},
            "taint_metadata_version": "runtime_v2",
        },
        {
            "interface_type": "telegram",
            "conversation_id": "epoch-test",
            "timestamp": post_epoch,
            "role": "assistant",
            "content": "post-epoch missing metadata",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": None,
            "taint_metadata_version": None,
        },
        {
            "interface_type": "telegram",
            "conversation_id": "epoch-test",
            "timestamp": post_epoch,
            "role": "user",
            "content": "post-epoch classified",
            "processing_profile_id": "default_assistant",
            "tool_name": None,
            "taint_metadata_json": {"max_tier": "trusted_user", "sources": []},
            "taint_metadata_version": "runtime_v2",
        },
    ]
    await api_db_context.execute(insert(message_history_table).values(rows))

    response = await api_client.get("/api/diagnostics/taint-audit?days=1")

    assert response.status_code == 200
    data = response.json()
    assert data["history_taint_epoch"] == epoch.isoformat()
    history = data["message_history"]
    assert history["total_rows"] == 4
    assert history["pre_epoch_rows"] == 2
    assert history["post_epoch_rows"] == 2
    assert history["post_epoch_missing_required_metadata_rows"] == 1
    missing_groups = [
        group for group in history["groups"] if group["metadata_version"] is None
    ]
    assert {group["pre_epoch"] for group in missing_groups} == {True, False}


@pytest.mark.asyncio
async def test_taint_diagnostics_reports_truncation(
    api_client: httpx.AsyncClient,
    api_db_context: Database,
) -> None:
    """Audit breakdowns state when the requested event cap truncates them."""
    for index in range(2):
        await api_db_context.taint_audit_events.add(
            event_id=f"audit-{index}",
            event_type="policy_evaluation",
            conversation_id="diagnostics-test",
            turn_id=f"turn-{index}",
            processing_profile_id="default_assistant",
            subconversation_id=None,
            tool_name="browser_open",
            tool_call_id=f"call-{index}",
            sink_class="attacker_addressable_egress",
            max_tier="unknown_external",
            sources=[],
            requested_outcome="confirm",
            effective_outcome="audit",
            mode="observe",
            reason="Observe mode decision",
            arguments_summary=None,
        )

    response = await api_client.get("/api/diagnostics/taint-audit?days=1&max_events=1")

    assert response.status_code == 200
    audit = response.json()["audit"]
    assert audit["matched_event_count"] == 2
    assert audit["included_event_count"] == 1
    assert audit["truncated"] is True
