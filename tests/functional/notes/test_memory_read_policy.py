"""The read policy that confines the memory curator.

Slice 3 of docs/design/conversation-memory.md. Visibility grants alone cannot
confine a reader: a note is visible when its labels are a *subset* of the
grants, so an unlabelled note -- and every label-less file skill -- is visible
to a reader granted only `memory`. The required-label floor is what closes
that, and these tests check it at every path that surfaces a note or a skill to
a profile: the context provider's prompt notes, the title list, the skill
catalogue, and `get_note` by title including its file-skill fallback.

The second half is about the ordinary reader, who keeps every note it had:
memory topic notes leave the always-loaded title list (their pointers live in
the core note's derived index) but stay reachable by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.context_providers import NotesContextProvider
from family_assistant.memory.invariants import MEMORY_LABEL
from family_assistant.memory.limits import MemoryLimits
from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.skills import NoteRegistry, ParsedSkill
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy
from family_assistant.tools.notes import get_note_tool, list_notes_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.tools.types import ToolResult

CORE_TITLE = MemoryLimits.DEFAULTS.core_note_title
TOPIC_TITLE = "Memory: Sam"
UNLABELLED_PROMPT_NOTE = "House Rules"
UNLABELLED_EXCLUDED_NOTE = "Groceries"
DEFAULT_LABELLED_NOTE = "Holiday Plans"
FILE_SKILL_NAME = "Email Drafting"

PROMPTS = {
    "note_item_format": "- {title}: {content}",
    "notes_context_header": "Relevant notes:\n{notes_list}",
    "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
}

CURATOR_POLICY = NoteReadPolicy.for_profile(
    visibility_grants=[MEMORY_LABEL],
    required_labels=[MEMORY_LABEL],
    memory_read=True,
)
"""What the shipped `memory_curator` profile's config resolves to."""

ORDINARY_POLICY = NoteReadPolicy.for_profile(
    visibility_grants=["default"], required_labels=None, memory_read=True
)
"""An ordinary profile that has been turned on for memory reading."""


def _registry() -> NoteRegistry:
    """A file-based skill, which like every file skill carries no labels."""
    return NoteRegistry([
        ParsedSkill(
            name=FILE_SKILL_NAME,
            description="Draft professional emails.",
            content="Consider the audience and purpose.",
            source_path=Path("/fake/email.md"),
            visibility_labels=frozenset(),
        )
    ])


async def _seed(engine: AsyncEngine) -> Database:
    """One store holding a note of every kind a reader could reach."""
    db = Database(engine=engine)
    await db.notes.add_or_update(
        title=UNLABELLED_PROMPT_NOTE,
        content="Shoes off at the door.",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    await db.notes.add_or_update(
        title=UNLABELLED_EXCLUDED_NOTE,
        content="Milk, bread.",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    await db.notes.add_or_update(
        title=DEFAULT_LABELLED_NOTE,
        content="Two weeks in June.",
        include_in_prompt=False,
        visibility_labels=["default"],
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    # Writing a topic note bootstraps the core note, so the store is never in
    # the shape "topics and no core".
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


def _provider(engine: AsyncEngine, read_policy: NoteReadPolicy) -> NotesContextProvider:
    return NotesContextProvider(
        get_db_context_func=lambda: Database(engine=engine),
        prompts=PROMPTS,
        read_policy=read_policy,
        note_registry=_registry(),
    )


def _exec_context(
    db: Database, read_policy: NoteReadPolicy, *, memory_read: bool = True
) -> ToolExecutionContext:
    return ToolExecutionContext(
        conversation_id="memory-read-conv",
        interface_type="internal",
        turn_id="turn-1",
        user_name="curator",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        note_registry=_registry(),
        visibility_grants=None
        if read_policy.grants is None
        else set(read_policy.grants),
        required_note_read_labels=sorted(read_policy.required_labels) or None,
        memory_read=memory_read,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


# ast-grep-ignore: no-dict-any - ToolResult.data is a union type
def _data(result: ToolResult) -> dict[str, Any]:
    assert isinstance(result.data, dict)
    return cast("dict[str, Any]", result.data)


@pytest.mark.asyncio
async def test_curator_prompt_notes_are_the_core_memory_note_alone(
    db_engine: AsyncEngine,
) -> None:
    """Grants would have admitted the unlabelled note; the floor does not."""
    db = await _seed(db_engine)

    prompt_notes = await db.notes.get_prompt_notes(read_policy=CURATOR_POLICY)

    assert [note.title for note in prompt_notes] == [CORE_TITLE]


@pytest.mark.asyncio
async def test_curator_sees_no_note_titles_at_all(db_engine: AsyncEngine) -> None:
    """Non-memory notes fail the floor; memory topics are excluded for everyone."""
    db = await _seed(db_engine)

    titles = await db.notes.get_excluded_notes_titles(read_policy=CURATOR_POLICY)

    assert titles == []


@pytest.mark.asyncio
async def test_curator_reaches_no_file_skill(db_engine: AsyncEngine) -> None:
    """A label-less skill passes any grant set, so only the floor stops it."""
    _ = await _seed(db_engine)
    registry = _registry()

    assert registry.get_skill_catalog(CURATOR_POLICY) == []
    assert registry.get_skill_by_name(FILE_SKILL_NAME, CURATOR_POLICY) is None


@pytest.mark.asyncio
async def test_curator_context_holds_the_core_note_and_nothing_else(
    db_engine: AsyncEngine,
) -> None:
    """The rendered prompt contribution, measured end to end."""
    await _seed(db_engine)

    fragments = await _provider(db_engine, CURATOR_POLICY).get_context_fragments(
        acting_user_id=None
    )
    rendered = "\n".join(fragments)

    assert "The household eats dinner at 6" in rendered
    assert UNLABELLED_PROMPT_NOTE not in rendered
    assert UNLABELLED_EXCLUDED_NOTE not in rendered
    assert DEFAULT_LABELLED_NOTE not in rendered
    assert FILE_SKILL_NAME not in rendered
    # The topic is named by the core note's derived index, which is the point
    # of the index; its contents are not in the prompt.
    assert "tram to school" not in rendered
    assert "Other available notes" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title", [UNLABELLED_PROMPT_NOTE, UNLABELLED_EXCLUDED_NOTE, DEFAULT_LABELLED_NOTE]
)
async def test_curator_get_note_does_not_resolve_a_household_note(
    db_engine: AsyncEngine, title: str
) -> None:
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=title, exec_context=_exec_context(db, CURATOR_POLICY)
    )

    assert _data(result)["exists"] is False


@pytest.mark.asyncio
async def test_curator_get_note_does_not_fall_back_to_a_file_skill(
    db_engine: AsyncEngine,
) -> None:
    """The fallback resolves through the same policy the DB lookup used."""
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=FILE_SKILL_NAME, exec_context=_exec_context(db, CURATOR_POLICY)
    )

    assert _data(result)["exists"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [CORE_TITLE, TOPIC_TITLE])
async def test_curator_reaches_the_memory_notes(
    db_engine: AsyncEngine, title: str
) -> None:
    """Confinement has to leave the curator able to do its job."""
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=title, exec_context=_exec_context(db, CURATOR_POLICY)
    )

    assert _data(result)["exists"] is True


@pytest.mark.asyncio
async def test_curator_listing_is_the_memory_notes_only(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    listed = await list_notes_tool(exec_context=_exec_context(db, CURATOR_POLICY))

    assert {note["title"] for note in listed} == {CORE_TITLE, TOPIC_TITLE}


@pytest.mark.asyncio
async def test_ordinary_reader_keeps_its_notes_and_the_core_memory_note(
    db_engine: AsyncEngine,
) -> None:
    db = await _seed(db_engine)

    prompt_notes = await db.notes.get_prompt_notes(read_policy=ORDINARY_POLICY)

    assert {note.title for note in prompt_notes} == {
        CORE_TITLE,
        UNLABELLED_PROMPT_NOTE,
    }


@pytest.mark.asyncio
async def test_memory_topics_leave_the_ordinary_title_list(
    db_engine: AsyncEngine,
) -> None:
    """Their pointers live in the core note's index; the list stays bounded."""
    db = await _seed(db_engine)

    titles = await db.notes.get_excluded_notes_titles(read_policy=ORDINARY_POLICY)

    assert set(titles) == {UNLABELLED_EXCLUDED_NOTE, DEFAULT_LABELLED_NOTE}


@pytest.mark.asyncio
async def test_ordinary_reader_still_opens_a_memory_topic_by_name(
    db_engine: AsyncEngine,
) -> None:
    """Leaving the title list is not the same as being unreachable."""
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=TOPIC_TITLE, exec_context=_exec_context(db, ORDINARY_POLICY)
    )

    data = _data(result)
    assert data["exists"] is True
    assert "tram to school" in data["content"]


@pytest.mark.asyncio
async def test_list_notes_still_shows_memory_topics(db_engine: AsyncEngine) -> None:
    """`list_notes` is an explicit call, not an always-loaded contribution."""
    db = await _seed(db_engine)

    listed = await list_notes_tool(exec_context=_exec_context(db, ORDINARY_POLICY))

    assert TOPIC_TITLE in {note["title"] for note in listed}


@pytest.mark.asyncio
async def test_ordinary_reader_still_reaches_file_skills(
    db_engine: AsyncEngine,
) -> None:
    """A reader with no floor is unaffected by the mechanism that confines the curator."""
    db = await _seed(db_engine)

    result = await get_note_tool(
        title=FILE_SKILL_NAME, exec_context=_exec_context(db, ORDINARY_POLICY)
    )

    assert _data(result)["exists"] is True
