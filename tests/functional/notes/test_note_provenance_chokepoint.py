"""Every note write stamps provenance at one chokepoint.

Milestone 3 of docs/design/ambient-note-admission-at-write-time.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert, select

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.note_restamp import (
    RESTAMP_EVENT_TYPE,
    RestampExclusions,
    RestampRule,
    apply_note_restamp,
    plan_note_restamp,
)
from family_assistant.security.taint import SourceTrustTier
from family_assistant.storage.database import Database
from family_assistant.storage.notes import notes_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.tools.notes import add_or_update_note_tool
from tests.functional.notes.ambient_helpers import (
    notes_provider,
    state_at,
    stored_tier,
    tool_context,
    tracker_at,
    write_note,
)

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_a_clean_turn_tool_write_stamps_trusted_internal(
    db_engine: AsyncEngine,
) -> None:
    """Model output is never the human's own words."""
    db = Database(db_engine)

    await add_or_update_note_tool(
        tool_context(db, tracker_at(None)), title="Groceries", content="milk"
    )

    assert await stored_tier(db, "Groceries") is SourceTrustTier.TRUSTED_INTERNAL


@pytest.mark.asyncio
async def test_a_web_api_write_stamps_trusted_user(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Groceries",
        "copied from a web page",
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )

    response = await api_client.post(
        "/api/notes/",
        json={"title": "Groceries", "content": "milk", "include_in_prompt": True},
    )

    assert response.status_code in {200, 201}
    assert await stored_tier(db, "Groceries") is SourceTrustTier.TRUSTED_USER


@pytest.mark.asyncio
async def test_a_clean_turn_tool_write_never_lowers_a_stored_stamp(
    db_engine: AsyncEngine,
) -> None:
    """The title is always retained, so the stored tier is too."""
    db = Database(db_engine)
    await write_note(
        db,
        "Research",
        "copied from a web page",
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )

    await add_or_update_note_tool(
        tool_context(db, tracker_at(None)),
        title="Research",
        content="rewritten in a clean turn",
    )

    assert await stored_tier(db, "Research") is SourceTrustTier.UNKNOWN_EXTERNAL


# ---------------------------------------------------------------------------
# The rollout restamp
# ---------------------------------------------------------------------------


async def _insert_unstamped(db: Database, title: str, *, labels: str = "[]") -> int:
    result = await db.execute(
        insert(notes_table).values(
            title=title,
            content=f"body of {title}",
            include_in_prompt=True,
            attachment_ids="[]",
            visibility_labels=labels,
            is_skill=False,
            provenance_metadata_json=None,
        )
    )
    assert result.inserted_primary_key is not None
    return int(result.inserted_primary_key[0])


async def _restamp(db: Database, exclusions: RestampExclusions) -> None:
    await apply_note_restamp(
        db, await plan_note_restamp(db, exclusions), batch_id="batch-1"
    )


@pytest.mark.asyncio
async def test_restamp_stamps_an_unstamped_household_note_internal(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _insert_unstamped(db, "Household rules")

    await _restamp(db, RestampExclusions())

    assert await stored_tier(db, "Household rules") is SourceTrustTier.TRUSTED_INTERNAL
    assert "body of Household rules" in "\n".join(
        await notes_provider(db).get_context_fragments(acting_user_id=None)
    )


@pytest.mark.asyncio
async def test_restamp_stamps_call_transcripts_and_operator_exclusions_external(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _insert_unstamped(db, "Call Transcript: kitchen - 2026-09-01T10:00")
    await _insert_unstamped(db, "Labelled transcript", labels='["call_transcript"]')
    await _insert_unstamped(db, "Imported research")
    await _insert_unstamped(db, "import-recipes")

    await _restamp(
        db,
        RestampExclusions(
            titles=frozenset({"Imported research"}),
            title_patterns=("import-*",),
            transcript_labels=frozenset({"call_transcript"}),
        ),
    )

    for title in (
        "Call Transcript: kitchen - 2026-09-01T10:00",
        "Labelled transcript",
        "Imported research",
        "import-recipes",
    ):
        assert await stored_tier(db, title) is SourceTrustTier.UNKNOWN_EXTERNAL, title


@pytest.mark.asyncio
async def test_restamp_leaves_stamped_rows_untouched(db_engine: AsyncEngine) -> None:
    db = Database(db_engine)
    await write_note(
        db,
        "Web digest",
        "x",
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )

    decisions = await plan_note_restamp(db, RestampExclusions())
    await apply_note_restamp(db, decisions)

    assert decisions == []
    assert await stored_tier(db, "Web digest") is SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_restamp_records_the_batch_and_rule_in_the_audit(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    note_id = await _insert_unstamped(db, "Call Transcript: hall - 2026-09-02")

    await _restamp(db, RestampExclusions())

    events = await db.taint_audit_events.list_since(
        datetime(2000, 1, 1, tzinfo=UTC), limit=10
    )
    restamps = [e for e in events if e["event_type"] == RESTAMP_EVENT_TYPE]
    assert len(restamps) == 1
    assert restamps[0]["artifact_id"] == f"note:{note_id}"
    assert restamps[0]["conversation_id"] == "batch-1"
    assert restamps[0]["effective_outcome"] == RestampRule.CALL_TRANSCRIPT.value


@pytest.mark.asyncio
async def test_restamp_enqueues_indexing_for_every_restamped_row(
    db_engine: AsyncEngine,
) -> None:
    """The indexed copy snapshots provenance, so search must see the new tier."""
    db = Database(db_engine)
    first = await _insert_unstamped(db, "One")
    second = await _insert_unstamped(db, "Two")

    await _restamp(db, RestampExclusions())

    rows = await db.fetch_all(
        select(tasks_table.c.payload).where(tasks_table.c.task_type == "index_note")
    )
    indexed = {row["payload"]["note_id"] for row in rows}
    assert {first, second} <= indexed


@pytest.mark.asyncio
async def test_a_null_row_surviving_the_restamp_is_excluded_and_logged(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = Database(db_engine)
    await _restamp(db, RestampExclusions())
    await _insert_unstamped(db, "Written after the batch")

    with caplog.at_level(logging.ERROR):
        prompt = "\n".join(
            await notes_provider(db).get_context_fragments(acting_user_id=None)
        )

    assert "body of Written after the batch" not in prompt
    assert any(record.levelno == logging.ERROR for record in caplog.records)
