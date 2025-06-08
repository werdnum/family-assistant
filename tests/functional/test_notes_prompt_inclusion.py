"""
Test cases for notes prompt inclusion control feature.
Tests the ability to mark notes as excluded from system prompts while keeping them searchable.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import (
    add_or_update_note,
    get_all_notes,
    get_note_by_title,
    get_prompt_notes,
)


@pytest.mark.asyncio
async def test_add_note_with_include_in_prompt_true(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test creating a note with include_in_prompt=True (default behavior)."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create note with explicit include_in_prompt=True
        result = await add_or_update_note(
            db_context=db,
            title="Test Note Included",
            content="This note should appear in prompts",
            include_in_prompt=True,
        )
        assert result == "Success"

        # Verify note exists and has correct flag
        note = await get_note_by_title(db, "Test Note Included")
        assert note is not None
        assert note["title"] == "Test Note Included"
        assert note["content"] == "This note should appear in prompts"
        assert note["include_in_prompt"] is True

        # Verify note appears in prompt notes
        prompt_notes = await get_prompt_notes(db)
        assert any(n["title"] == "Test Note Included" for n in prompt_notes)

        # Verify note appears in all notes
        all_notes = await get_all_notes(db)
        assert any(n["title"] == "Test Note Included" for n in all_notes)


@pytest.mark.asyncio
async def test_add_note_with_include_in_prompt_false(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test creating a note with include_in_prompt=False."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create note with include_in_prompt=False
        result = await add_or_update_note(
            db_context=db,
            title="Test Note Excluded",
            content="This note should NOT appear in prompts",
            include_in_prompt=False,
        )
        assert result == "Success"

        # Verify note exists and has correct flag
        note = await get_note_by_title(db, "Test Note Excluded")
        assert note is not None
        assert note["title"] == "Test Note Excluded"
        assert note["content"] == "This note should NOT appear in prompts"
        assert note["include_in_prompt"] is False

        # Verify note does NOT appear in prompt notes
        prompt_notes = await get_prompt_notes(db)
        assert not any(n["title"] == "Test Note Excluded" for n in prompt_notes)

        # Verify note still appears in all notes
        all_notes = await get_all_notes(db)
        matching_notes = [n for n in all_notes if n["title"] == "Test Note Excluded"]
        assert len(matching_notes) == 1
        assert matching_notes[0]["include_in_prompt"] is False


@pytest.mark.asyncio
async def test_add_note_default_includes_in_prompt(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that notes are included in prompts by default when parameter is omitted."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create note without specifying include_in_prompt
        result = await add_or_update_note(
            db_context=db,
            title="Test Note Default",
            content="This note uses default behavior",
        )
        assert result == "Success"

        # Verify note is included by default
        note = await get_note_by_title(db, "Test Note Default")
        assert note is not None
        assert note["include_in_prompt"] is True

        # Verify note appears in prompt notes
        prompt_notes = await get_prompt_notes(db)
        assert any(n["title"] == "Test Note Default" for n in prompt_notes)


@pytest.mark.asyncio
async def test_update_note_include_in_prompt_flag(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test updating a note's include_in_prompt flag."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create note included in prompts
        await add_or_update_note(
            db_context=db,
            title="Test Note Toggle",
            content="Original content",
            include_in_prompt=True,
        )

        # Verify initial state
        prompt_notes = await get_prompt_notes(db)
        assert any(n["title"] == "Test Note Toggle" for n in prompt_notes)

        # Update to exclude from prompts
        await add_or_update_note(
            db_context=db,
            title="Test Note Toggle",
            content="Updated content",
            include_in_prompt=False,
        )

        # Verify updated state
        note = await get_note_by_title(db, "Test Note Toggle")
        assert note is not None
        assert note["content"] == "Updated content"
        assert note["include_in_prompt"] is False

        # Verify no longer in prompt notes
        prompt_notes = await get_prompt_notes(db)
        assert not any(n["title"] == "Test Note Toggle" for n in prompt_notes)

        # Update back to include in prompts
        await add_or_update_note(
            db_context=db,
            title="Test Note Toggle",
            content="Final content",
            include_in_prompt=True,
        )

        # Verify final state
        prompt_notes = await get_prompt_notes(db)
        assert any(n["title"] == "Test Note Toggle" for n in prompt_notes)


@pytest.mark.asyncio
async def test_get_prompt_notes_filters_correctly(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that get_prompt_notes returns only notes with include_in_prompt=True."""
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Create mix of included and excluded notes
        test_notes = [
            ("Included Note 1", "Content 1", True),
            ("Excluded Note 1", "Content 2", False),
            ("Included Note 2", "Content 3", True),
            ("Excluded Note 2", "Content 4", False),
            ("Included Note 3", "Content 5", True),
        ]

        for title, content, include in test_notes:
            await add_or_update_note(
                db_context=db,
                title=title,
                content=content,
                include_in_prompt=include,
            )

        # Get prompt notes
        prompt_notes = await get_prompt_notes(db)
        prompt_titles = {n["title"] for n in prompt_notes}

        # Verify only included notes are returned
        expected_titles = {"Included Note 1", "Included Note 2", "Included Note 3"}
        assert expected_titles.issubset(prompt_titles)
        assert "Excluded Note 1" not in prompt_titles
        assert "Excluded Note 2" not in prompt_titles

        # Get all notes
        all_notes = await get_all_notes(db)
        all_titles = {n["title"] for n in all_notes}

        # Verify all notes are returned
        for title, _, _ in test_notes:
            assert title in all_titles
