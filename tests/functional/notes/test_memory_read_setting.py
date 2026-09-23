"""What ``memory_read`` does to a profile's reads and writes.

Slice 4 of docs/design/conversation-memory.md, "Two settings, one convenience
default". Memory notes carry the ``memory`` visibility label and the shipped
profiles are granted ``default``, so without this setting no ordinary profile
could see the core note at all. ``memory_read`` is resolved in exactly one
place -- :meth:`NoteReadPolicy.for_profile` -- into the grant that makes memory
visible, and into the denial that takes it away again from a profile that is
turned off. A profile that cannot read memory may not write it either: it would
be duplicating entries it never saw and replacing text it never read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.context_providers import NotesContextProvider
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.limits import MemoryLimits
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools.memory import propose_memory_edits_tool
from family_assistant.tools.notes import (
    add_or_update_note_tool,
    delete_note_tool,
    get_note_tool,
    list_notes_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolResult

CORE_TITLE = MemoryLimits.DEFAULTS.core_note_title
TOPIC_TITLE = "Memory: Sam"
HOUSEHOLD_NOTE = "House Rules"

PROMPTS = {
    "note_item_format": "- {title}: {content}",
    "notes_context_header": "Relevant notes:\n{notes_list}",
    "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
}


async def _seed(engine: AsyncEngine) -> Database:
    db = Database(engine=engine)
    await db.notes.add_or_update(
        title=HOUSEHOLD_NOTE,
        content="Shoes off at the door.",
        include_in_prompt=True,
        visibility_labels=["default"],
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    await db.notes.add_or_update(
        title=TOPIC_TITLE,
        content="- Sam takes the tram to school (2026-09-01, Alice).",
        include_in_prompt=False,
        visibility_labels=[MEMORY_LABEL],
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    await db.notes.add_or_update(
        title=CORE_TITLE,
        content="- The household eats dinner at 6 (2026-09-01, Alice).",
        include_in_prompt=True,
        visibility_labels=[MEMORY_LABEL],
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    return db


def _policy(*, memory_read: bool, grants: list[str]) -> NoteReadPolicy:
    return NoteReadPolicy.for_profile(
        visibility_grants=grants, required_labels=None, memory_read=memory_read
    )


def _context(
    db: Database, *, memory_read: bool, grants: list[str] | None = None
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="memory-setting-conv",
        user_name="alice",
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=set(grants) if grants is not None else None,
        memory_read=memory_read,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


# ast-grep-ignore: no-dict-any - ToolResult.data is a union type
def _data(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result.data, dict)
    return cast("dict[str, Any]", result.data)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reading_profile_gets_the_core_note_in_its_prompt(
    db_engine: AsyncEngine,
) -> None:
    """The setting supplies the grant; the profile's own list never names it."""
    await _seed(db_engine)
    provider = NotesContextProvider(
        get_db_context_func=lambda: Database(engine=db_engine),
        prompts=PROMPTS,
        read_policy=_policy(memory_read=True, grants=["default"]),
        note_registry=None,
    )

    rendered = "\n".join(await provider.get_context_fragments(acting_user_id=None))

    assert "The household eats dinner at 6" in rendered
    assert "Shoes off at the door" in rendered


@pytest.mark.asyncio
async def test_a_non_reading_profile_sees_no_memory_despite_the_grant(
    db_engine: AsyncEngine,
) -> None:
    """The setting wins over the grant, so the two cannot disagree."""
    await _seed(db_engine)
    provider = NotesContextProvider(
        get_db_context_func=lambda: Database(engine=db_engine),
        prompts=PROMPTS,
        read_policy=_policy(memory_read=False, grants=["default", MEMORY_LABEL]),
        note_registry=None,
    )

    rendered = "\n".join(await provider.get_context_fragments(acting_user_id=None))

    assert "The household eats dinner at 6" not in rendered
    assert "Shoes off at the door" in rendered


@pytest.mark.asyncio
async def test_a_profile_with_no_grants_at_all_is_still_denied_memory(
    db_engine: AsyncEngine,
) -> None:
    """Grants cannot express this: an unconfigured reader sees every label set."""
    db = await _seed(db_engine)

    notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.for_profile(
            visibility_grants=None, required_labels=None, memory_read=False
        )
    )

    assert {note.title for note in notes} == {HOUSEHOLD_NOTE}


@pytest.mark.asyncio
async def test_a_non_reading_profile_cannot_open_a_memory_topic(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=TOPIC_TITLE,
        exec_context=_context(db, memory_read=False, grants=["default", MEMORY_LABEL]),
    )

    assert _data(result)["exists"] is False


@pytest.mark.asyncio
async def test_a_non_reading_profile_does_not_list_memory_notes(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    listed = await list_notes_tool(
        exec_context=_context(db, memory_read=False, grants=["default", MEMORY_LABEL])
    )

    assert {note["title"] for note in listed} == {HOUSEHOLD_NOTE}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_reading_profile_cannot_propose_memory_edits(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    result = await propose_memory_edits_tool(
        _context(db, memory_read=False),
        edits=[
            {
                "op": "add",
                "note_title": TOPIC_TITLE,
                "entry": "Sam prefers the ferry.",
                "message_ids": [1],
            }
        ],
    )

    assert "does not read the household's memory" in result.get_text()
    note = await db.notes.get_by_title(
        TOPIC_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert "ferry" not in note.content


@pytest.mark.asyncio
async def test_a_non_reading_profile_cannot_overwrite_a_memory_note(
    db_engine: AsyncEngine,
) -> None:
    """The whole-note path is refused at the repository, not only the tool."""
    db = await _seed(db_engine)

    result = await add_or_update_note_tool(
        exec_context=_context(db, memory_read=False, grants=["default", MEMORY_LABEL]),
        title=TOPIC_TITLE,
        content="- Sam prefers the ferry.",
    )

    assert result.startswith("Error:")
    note = await db.notes.get_by_title(
        TOPIC_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert "ferry" not in note.content


@pytest.mark.asyncio
async def test_a_non_reading_profile_cannot_label_a_new_note_as_memory(
    db_engine: AsyncEngine,
) -> None:
    await _seed(db_engine)
    db = Database(engine=db_engine)

    result = await add_or_update_note_tool(
        exec_context=_context(db, memory_read=False, grants=["default", MEMORY_LABEL]),
        title="Smuggled Memory",
        content="- Invented standing fact.",
        visibility_labels=[MEMORY_LABEL],
    )

    assert "withheld from the active profile" in result
    assert (
        await db.notes.get_by_title(
            "Smuggled Memory", read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_non_reading_profile_cannot_delete_a_memory_note(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    result = await delete_note_tool(
        title=TOPIC_TITLE,
        exec_context=_context(db, memory_read=False, grants=["default", MEMORY_LABEL]),
    )

    assert result["success"] is False
    assert (
        await db.notes.get_by_title(
            TOPIC_TITLE, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        is not None
    )


@pytest.mark.asyncio
async def test_a_reading_profile_still_writes_its_own_notes(
    db_engine: AsyncEngine,
) -> None:
    """The denial is about memory, not about notes."""
    db = await _seed(db_engine)

    result = await add_or_update_note_tool(
        exec_context=_context(db, memory_read=False, grants=["default"]),
        title="Shopping",
        content="Milk, bread.",
    )

    assert "withheld" not in result
    assert (
        await db.notes.get_by_title("Shopping", read_policy=NoteReadPolicy.UNRESTRICTED)
        is not None
    )
