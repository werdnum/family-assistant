"""Definition records on one-shot callbacks (M1).

A reminder or future callback has no durable definition table: its definition
is the enqueued payload, so its record rides the payload beside the definition
it describes.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.security.definition_records import (
    callback_definition_content,
    definition_record_from_row,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import LlmCallbackPayload
from family_assistant.tools.tasks import (
    modify_pending_callback_tool,
    schedule_future_callback_tool,
    schedule_reminder_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from family_assistant.security.definition_records import DefinitionRecord


def _exec_context(
    db_ctx: Database,
    *,
    tracker: InMemoryTurnTaintTracker | None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id="test_turn",
        db_context=db_ctx,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        taint_tracker=tracker,
    )


def _tainted_tracker() -> InMemoryTurnTaintTracker:
    return InMemoryTurnTaintTracker(
        TurnTaintState.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.EMAIL,
                source_id="msg-1",
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset(),
                reason="Summarize this email.",
            )
        )
    )


def _future_time() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


async def _only_callback_payload(db_engine: AsyncEngine) -> LlmCallbackPayload:
    db = Database(engine=db_engine)
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert len(rows) == 1, rows
    return cast("LlmCallbackPayload", dict(rows[0])["payload"])


def _record(payload: LlmCallbackPayload) -> "DefinitionRecord":
    record = definition_record_from_row(
        payload.get("tool_call_review_definition_record")
    )
    assert record is not None
    return record


@pytest.mark.asyncio
async def test_a_reminder_from_a_clean_turn_records_trusted_internal(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    result = await schedule_reminder_tool(
        exec_context=_exec_context(db, tracker=InMemoryTurnTaintTracker()),
        reminder_time=_future_time(),
        message="Take the bins out",
    )
    assert result is not None and result.startswith("OK."), result

    payload = await _only_callback_payload(db_engine)
    record = _record(payload)

    assert record.taint_metadata.get("max_tier") == "trusted_internal"
    assert record.matches(callback_definition_content("Take the bins out"))


@pytest.mark.asyncio
async def test_a_reminder_from_a_tainted_turn_records_the_turn_maximum(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)

    await schedule_reminder_tool(
        exec_context=_exec_context(db, tracker=_tainted_tracker()),
        reminder_time=_future_time(),
        message="Take the bins out",
    )

    record = _record(await _only_callback_payload(db_engine))

    assert record.taint_metadata.get("max_tier") == "unknown_external"


@pytest.mark.asyncio
async def test_a_future_callback_records_its_context(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)

    await schedule_future_callback_tool(
        exec_context=_exec_context(db, tracker=InMemoryTurnTaintTracker()),
        callback_time=_future_time(),
        context="Check whether the parcel arrived",
    )

    payload = await _only_callback_payload(db_engine)
    record = _record(payload)

    assert record.matches(
        callback_definition_content("Check whether the parcel arrived")
    )


@pytest.mark.asyncio
async def test_editing_a_pending_callback_replaces_record_and_definition_together(
    db_engine: AsyncEngine,
) -> None:
    """A stale record against new content would silently un-cure a legitimate edit."""
    db = Database(engine=db_engine)
    await schedule_future_callback_tool(
        exec_context=_exec_context(db, tracker=InMemoryTurnTaintTracker()),
        callback_time=_future_time(),
        context="Check whether the parcel arrived",
    )
    before = await _only_callback_payload(db_engine)
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    task_id = str(dict(rows[0])["task_id"])

    result = await modify_pending_callback_tool(
        exec_context=_exec_context(db, tracker=_tainted_tracker()),
        task_id=task_id,
        new_context="Check whether the *other* parcel arrived",
    )
    assert "modified successfully" in result, result

    after = await _only_callback_payload(db_engine)
    record = _record(after)

    assert after["callback_context"] == "Check whether the *other* parcel arrived"
    assert (
        after.get("tool_call_review_trigger_definition")
        == "Check whether the *other* parcel arrived"
    )
    assert record.matches(
        callback_definition_content("Check whether the *other* parcel arrived")
    )
    assert not record.matches(
        callback_definition_content("Check whether the parcel arrived")
    )
    # The editing turn's taint is what the new record carries.
    assert record.taint_metadata.get("max_tier") == "unknown_external"
    assert _record(before).taint_metadata.get("max_tier") == "trusted_internal"


@pytest.mark.asyncio
async def test_rescheduling_a_callback_leaves_its_record_alone(
    db_engine: AsyncEngine,
) -> None:
    """Moving a callback in time changes no content."""
    db = Database(engine=db_engine)
    await schedule_future_callback_tool(
        exec_context=_exec_context(db, tracker=InMemoryTurnTaintTracker()),
        callback_time=_future_time(),
        context="Check whether the parcel arrived",
    )
    before = _record(await _only_callback_payload(db_engine))
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    task_id = str(dict(rows[0])["task_id"])

    await modify_pending_callback_tool(
        exec_context=_exec_context(db, tracker=_tainted_tracker()),
        task_id=task_id,
        new_callback_time=(datetime.now(UTC) + timedelta(hours=3)).isoformat(),
    )

    assert _record(await _only_callback_payload(db_engine)) == before
