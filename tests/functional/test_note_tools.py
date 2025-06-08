"""Test new note tools functionality."""

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.notes import (
    delete_note_tool,
    get_note_tool,
    list_notes_tool,
)


@pytest.mark.asyncio
async def test_get_note_tool(test_db_engine: AsyncEngine) -> None:
    """Test the get_note tool."""
    from family_assistant import storage

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Add a test note
        await storage.add_or_update_note(
            db_context, "Test Note", "Test content", include_in_prompt=True
        )

        # Mock context object
        @dataclass
        class MockContext:
            db_context: DatabaseContext

        context = MockContext(db_context)

        # Test getting existing note
        result = await get_note_tool("Test Note", context)
        assert result["exists"] is True
        assert result["title"] == "Test Note"
        assert result["content"] == "Test content"
        assert result["include_in_prompt"] is True

        # Test getting non-existent note
        result = await get_note_tool("Non-existent Note", context)
        assert result["exists"] is False
        assert result["title"] == "Non-existent Note"
        assert result["content"] is None
        assert result["include_in_prompt"] is None


@pytest.mark.asyncio
async def test_list_notes_tool(test_db_engine: AsyncEngine) -> None:
    """Test the list_notes tool."""
    from family_assistant import storage

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Add test notes with different include_in_prompt values
        await storage.add_or_update_note(
            db_context, "Note 1", "Content 1", include_in_prompt=True
        )
        await storage.add_or_update_note(
            db_context, "Note 2", "Content 2", include_in_prompt=False
        )
        await storage.add_or_update_note(
            db_context,
            "Note 3",
            "A very long content that should be truncated in the preview " * 10,
            include_in_prompt=True,
        )

        # Mock context object
        @dataclass
        class MockContext:
            db_context: DatabaseContext

        context = MockContext(db_context)

        # Test listing all notes
        result = await list_notes_tool(None, context)
        assert len(result) == 3

        # Test filtering for included notes
        result = await list_notes_tool(True, context)
        assert len(result) == 2
        assert all(note["include_in_prompt"] is True for note in result)

        # Test filtering for excluded notes
        result = await list_notes_tool(False, context)
        assert len(result) == 1
        assert result[0]["title"] == "Note 2"
        assert result[0]["include_in_prompt"] is False

        # Test content preview truncation
        long_note = next(
            note
            for note in await list_notes_tool(None, context)
            if note["title"] == "Note 3"
        )
        assert len(long_note["content_preview"]) == 103  # 100 chars + "..."
        assert long_note["content_preview"].endswith("...")


@pytest.mark.asyncio
async def test_delete_note_tool(test_db_engine: AsyncEngine) -> None:
    """Test the delete_note tool."""
    from family_assistant import storage

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Add a test note
        await storage.add_or_update_note(
            db_context, "Note to Delete", "Content", include_in_prompt=True
        )

        # Mock context object
        @dataclass
        class MockContext:
            db_context: DatabaseContext

        context = MockContext(db_context)

        # Verify note exists
        note = await storage.get_note_by_title(db_context, "Note to Delete")
        assert note is not None

        # Delete the note
        result = await delete_note_tool("Note to Delete", context)
        assert result["success"] is True
        assert "deleted successfully" in result["message"]

        # Verify note is deleted
        note = await storage.get_note_by_title(db_context, "Note to Delete")
        assert note is None

        # Test deleting non-existent note
        result = await delete_note_tool("Non-existent Note", context)
        assert result["success"] is False
        assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_add_or_update_note_with_prompt_control(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that add_or_update_note properly handles include_in_prompt parameter."""
    from family_assistant import storage

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Add note with include_in_prompt=False
        await storage.add_or_update_note(
            db_context,
            "Excluded Note",
            "This should not be in prompt",
            include_in_prompt=False,
        )

        # Verify it's saved correctly
        note = await storage.get_note_by_title(db_context, "Excluded Note")
        assert note is not None
        assert note["include_in_prompt"] is False

        # Update the same note to include_in_prompt=True
        await storage.add_or_update_note(
            db_context,
            "Excluded Note",
            "This should now be in prompt",
            include_in_prompt=True,
        )

        # Verify it's updated
        note = await storage.get_note_by_title(db_context, "Excluded Note")
        assert note is not None
        assert note["include_in_prompt"] is True
        assert note["content"] == "This should now be in prompt"

        # Test that only prompt-included notes are returned by get_prompt_notes
        prompt_notes = await storage.get_prompt_notes(db_context)
        assert any(n["title"] == "Excluded Note" for n in prompt_notes)

        # Add another excluded note
        await storage.add_or_update_note(
            db_context, "Another Excluded", "Not in prompt", include_in_prompt=False
        )

        # Verify it's not in prompt notes
        prompt_notes = await storage.get_prompt_notes(db_context)
        assert not any(n["title"] == "Another Excluded" for n in prompt_notes)
