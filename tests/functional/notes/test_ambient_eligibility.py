"""Ambient eligibility derives from a note's stored tier.

Milestone 2 of docs/design/ambient-note-admission-at-write-time.md: a note
marked ``include_in_prompt``, or a database skill, reaches every prompt whole
only when its stored tier is admissible for reuse. Everything else stays
discoverable by title, and explicit reads are unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import insert

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import SourceTrustTier
from family_assistant.storage.database import Database
from family_assistant.storage.notes import notes_table
from family_assistant.tools.notes import get_note_tool, list_notes_tool
from tests.functional.notes.ambient_helpers import (
    SKILL_BODY,
    notes_provider,
    state_at,
    tool_context,
    tracker_at,
    write_note,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

EXTERNAL = NoteProvenanceStamp.machine(state_at(SourceTrustTier.UNKNOWN_EXTERNAL))
REVIEWED = NoteProvenanceStamp.admitted(title="reviewed", decided_by="test")


async def _prompt_text(db: Database) -> str:
    return "\n".join(
        await notes_provider(db).get_context_fragments(acting_user_id=None)
    )


@pytest.mark.asyncio
async def test_unreviewed_prompt_note_is_listed_by_title_not_included(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(db, "Web digest", "BODY FROM THE WEB", provenance=EXTERNAL)

    prompt = await _prompt_text(db)

    assert "BODY FROM THE WEB" not in prompt
    assert '"Web digest"' in prompt


@pytest.mark.asyncio
async def test_unreviewed_skill_is_absent_from_the_catalog(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db, "Packing", SKILL_BODY, provenance=EXTERNAL, include_in_prompt=False
    )

    prompt = await _prompt_text(db)

    assert "Pack for a trip" not in prompt
    assert '"Packing"' in prompt


@pytest.mark.asyncio
async def test_unreviewed_prompt_note_contributes_no_turn_taint(
    db_engine: AsyncEngine,
) -> None:
    """The conversation starts at trusted_user: the note is not in its prompt."""
    db = Database(db_engine)
    await write_note(db, "Web digest", "BODY FROM THE WEB", provenance=EXTERNAL)

    assert await notes_provider(db).get_context_taint_sources() == ()


@pytest.mark.asyncio
async def test_reviewed_note_is_included_and_taints_the_turn_machine_reviewed(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(db, "Procedure", "REVIEWED BODY", provenance=REVIEWED)

    prompt = await _prompt_text(db)
    sources = await notes_provider(db).get_context_taint_sources()

    assert "REVIEWED BODY" in prompt
    assert {source.tier for source in sources} == {SourceTrustTier.MACHINE_REVIEWED}


@pytest.mark.asyncio
async def test_reviewed_skill_alone_starts_the_turn_at_machine_reviewed(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(
        db, "Packing", SKILL_BODY, provenance=REVIEWED, include_in_prompt=False
    )

    prompt = await _prompt_text(db)
    sources = await notes_provider(db).get_context_taint_sources()

    assert "Pack for a trip" in prompt
    assert sources
    assert max(source.tier for source in sources) is SourceTrustTier.MACHINE_REVIEWED


@pytest.mark.asyncio
async def test_explicit_read_of_an_unreviewed_note_still_propagates_its_taint(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(db, "Web digest", "BODY FROM THE WEB", provenance=EXTERNAL)
    tracker = tracker_at(None)

    result = await get_note_tool("Web digest", tool_context(db, tracker))

    assert result.data is not None
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL


async def _insert_unstamped(db: Database, title: str) -> None:
    await db.execute(
        insert(notes_table).values(
            title=title,
            content="LEGACY BODY",
            include_in_prompt=True,
            attachment_ids="[]",
            visibility_labels="[]",
            is_skill=False,
            provenance_metadata_json=None,
        )
    )


@pytest.mark.asyncio
async def test_unstamped_row_is_excluded_from_the_prompt_and_logged(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = Database(db_engine)
    await _insert_unstamped(db, "Legacy")

    with caplog.at_level(logging.ERROR):
        prompt = await _prompt_text(db)

    assert "LEGACY BODY" not in prompt
    assert '"Legacy"' in prompt
    assert any(
        record.levelno == logging.ERROR and "no stored provenance" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_unstamped_row_read_explicitly_raises_the_turn_to_unknown_external(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _insert_unstamped(db, "Legacy")
    note_tracker = tracker_at(None)
    list_tracker = tracker_at(None)

    await get_note_tool("Legacy", tool_context(db, note_tracker))
    await list_notes_tool(tool_context(db, list_tracker))

    assert note_tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert list_tracker.snapshot().max_tier is SourceTrustTier.RECOGNIZED_MACHINE


@pytest.mark.asyncio
async def test_rollout_audit_counts_the_notes_the_rule_excludes(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await write_note(db, "Web digest", "x", provenance=EXTERNAL)
    await write_note(
        db, "Packing", SKILL_BODY, provenance=EXTERNAL, include_in_prompt=False
    )
    await write_note(db, "Procedure", "y", provenance=REVIEWED)
    await _insert_unstamped(db, "Legacy")

    counts = await db.notes.count_excluded_ambient_notes()

    assert counts == {"prompt_notes": 2, "skills": 1, "missing_provenance": 1}
