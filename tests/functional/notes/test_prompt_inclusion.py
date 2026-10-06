"""
Test cases for notes prompt inclusion control feature.
Tests the ability to mark notes as excluded from system prompts while keeping them searchable.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy, NoteWritePolicy


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_add_note_default_includes_in_prompt(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that notes are included in prompts by default when parameter is omitted."""
    db = Database(engine=pg_vector_db_engine)
    # Create note without specifying include_in_prompt
    result = await db.notes.add_or_update(
        title="Test Note Default",
        content="This note uses default behavior",
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )
    assert result.tier_lowered is None

    # Verify note is included by default
    note = await db.notes.get_by_title(
        "Test Note Default", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert note.include_in_prompt is True

    # Verify note appears in prompt notes
    prompt_notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert any(n.title == "Test Note Default" for n in prompt_notes)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_update_note_include_in_prompt_flag(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test updating a note's include_in_prompt flag."""
    db = Database(engine=pg_vector_db_engine)
    # Create note included in prompts
    await db.notes.add_or_update(
        title="Test Note Toggle",
        content="Original content",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    # Verify initial state
    prompt_notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert any(n.title == "Test Note Toggle" for n in prompt_notes)

    # Update to exclude from prompts
    await db.notes.add_or_update(
        title="Test Note Toggle",
        content="Updated content",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    # Verify updated state
    note = await db.notes.get_by_title(
        "Test Note Toggle", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert note.content == "Updated content"
    assert note.include_in_prompt is False

    # Verify no longer in prompt notes
    prompt_notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert not any(n.title == "Test Note Toggle" for n in prompt_notes)

    # Update back to include in prompts
    await db.notes.add_or_update(
        title="Test Note Toggle",
        content="Final content",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    # Verify final state
    prompt_notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert any(n.title == "Test Note Toggle" for n in prompt_notes)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_get_prompt_notes_filters_correctly(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Notes excluded from prompts are stored with the flag and stay listable.

    get_prompt_notes returns only notes written with include_in_prompt=True,
    while get_all and get_by_title still return every note with its flag.
    """
    db = Database(engine=pg_vector_db_engine)
    test_notes = [
        ("Included Note 1", "Content 1", True),
        ("Excluded Note 1", "Content 2", False),
        ("Included Note 2", "Content 3", True),
        ("Excluded Note 2", "Content 4", False),
        ("Included Note 3", "Content 5", True),
    ]

    for title, content, include in test_notes:
        result = await db.notes.add_or_update(
            title=title,
            content=content,
            include_in_prompt=include,
            write_policy=NoteWritePolicy.UNCONSTRAINED,
            provenance=NoteProvenanceStamp.internal(),
        )
        assert result.tier_lowered is None

    for title, content, include in test_notes:
        note = await db.notes.get_by_title(
            title, read_policy=NoteReadPolicy.UNRESTRICTED
        )
        assert note is not None
        assert note.content == content
        assert note.include_in_prompt is include

    prompt_notes = await db.notes.get_prompt_notes(
        read_policy=NoteReadPolicy.UNRESTRICTED
    )
    prompt_titles = {n.title for n in prompt_notes}

    expected_titles = {"Included Note 1", "Included Note 2", "Included Note 3"}
    assert expected_titles.issubset(prompt_titles)
    assert "Excluded Note 1" not in prompt_titles
    assert "Excluded Note 2" not in prompt_titles

    all_notes = await db.notes.get_all(read_policy=NoteReadPolicy.UNRESTRICTED)
    flags_by_title = {n.title: n.include_in_prompt for n in all_notes}

    for title, _, include in test_notes:
        assert flags_by_title.get(title) is include
