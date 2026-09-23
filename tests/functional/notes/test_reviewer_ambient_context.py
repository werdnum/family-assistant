"""Eligible ambient material reaches the tool-call reviewer, and nothing else.

Milestone 5 of docs/design/ambient-note-admission-at-write-time.md: the
turn-context block the reviewer skips also carries unreviewed titles, so the
reviewed notes and skills reach it through their own bounded section, rendered
by the prompt's own renderer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import SourceTrustTier
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    _ambient_review_context,  # noqa: PLC2701 - the reviewer's ambient channel
)
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

    from family_assistant.processing import ProcessingService


async def _seed(db: Database) -> None:
    await write_note(
        db,
        "Packing procedure",
        "Roll clothes.",
        provenance=NoteProvenanceStamp.admitted(
            title="Packing procedure", decided_by="test"
        ),
    )
    await write_note(
        db,
        "Packing skill",
        SKILL_BODY,
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.admitted(
            title="Packing skill", decided_by="test"
        ),
    )
    await write_note(
        db,
        "Web digest",
        "UNREVIEWED BODY",
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )
    await write_note(
        db,
        "Unreviewed reference title",
        "x",
        include_in_prompt=False,
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
    )


async def _reviewer_section(db: Database) -> str:
    context = tool_context(db, tracker_at(None))
    context.processing_service = cast(
        "ProcessingService", SimpleNamespace(context_providers=[notes_provider(db)])
    )
    section = await _ambient_review_context(context)
    assert section is not None
    return section


@pytest.mark.asyncio
async def test_the_section_is_what_the_prompt_rendered_for_reviewed_material(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _seed(db)

    section = await _reviewer_section(db)
    prompt_fragments = await notes_provider(db).get_context_fragments(
        acting_user_id=None
    )

    assert "Roll clothes." in section
    assert "Pack for a trip" in section
    for fragment in section.split("\n\n"):
        assert fragment in prompt_fragments


@pytest.mark.asyncio
async def test_unreviewed_titles_never_reach_the_reviewer(
    db_engine: AsyncEngine,
) -> None:
    db = Database(db_engine)
    await _seed(db)

    section = await _reviewer_section(db)

    assert "Web digest" not in section
    assert "UNREVIEWED BODY" not in section
    assert "Unreviewed reference title" not in section
