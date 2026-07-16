"""Functional tests for runtime taint diagnostics."""

from datetime import UTC, datetime
from typing import cast

import httpx
import pytest
from sqlalchemy import insert

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.message_history import message_history_table


def _counts_by_key(items: list[dict[str, object]]) -> dict[str | None, int]:
    """Convert a diagnostics count list into a lookup for assertions."""
    return {
        cast("str | None", item["key"]): cast("int", item["count"]) for item in items
    }


async def _commit_seed_data(db_context: DatabaseContext) -> None:
    """Commit API fixture seed data so the request connection can observe it."""
    if db_context.conn is None:
        raise RuntimeError("Database test context is not active")
    await db_context.conn.commit()


@pytest.mark.asyncio
async def test_taint_diagnostics_reports_audits_and_distinct_history_rows(
    api_client: httpx.AsyncClient,
    api_db_context: DatabaseContext,
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
            "taint_metadata_version": "runtime_v1",
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
            "taint_metadata_version": "runtime_v1",
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
    await api_db_context.execute_with_retry(insert(message_history_table).values(rows))
    await _commit_seed_data(api_db_context)

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
    assert _counts_by_key(data["audit"]["source_label_occurrences"]) == {"web": 1}

    history = data["message_history"]
    assert history["total_rows"] == 5
    assert history["classified_rows"] == 2
    assert history["missing_required_metadata_rows"] == 1
    assert history["malformed_metadata_rows"] == 1
    assert history["not_applicable_rows"] == 1
    assert _counts_by_key(history["by_metadata_version"]) == {
        None: 2,
        "legacy_inferred": 1,
        "runtime_v1": 2,
    }
    assert _counts_by_key(history["by_tool_name"])["browser_open"] == 1
    serialized = response.text
    assert "source-secret" not in serialized
    assert "history-test" not in serialized
    assert "External web content" not in serialized


@pytest.mark.asyncio
async def test_taint_diagnostics_reports_truncation(
    api_client: httpx.AsyncClient,
    api_db_context: DatabaseContext,
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
    await _commit_seed_data(api_db_context)

    response = await api_client.get("/api/diagnostics/taint-audit?days=1&max_events=1")

    assert response.status_code == 200
    audit = response.json()["audit"]
    assert audit["matched_event_count"] == 2
    assert audit["included_event_count"] == 1
    assert audit["truncated"] is True
