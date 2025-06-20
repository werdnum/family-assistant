"""Test the append functionality for notes."""

from typing import Any

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.notes import add_or_update_note_tool
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_add_or_update_note_append(test_db_engine: Any) -> None:
    """Test that the append functionality works correctly for notes."""
    async with DatabaseContext() as db:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db,
        )

        # Create initial note
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="Test Note",
            content="Initial content",
            include_in_prompt=True,
            append=False,
        )
        assert "created" in result or "updated" in result

        # Verify initial content
        note = await db.notes.get_by_title("Test Note")
        assert note is not None
        assert note["content"] == "Initial content"

        # Append to the note
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="Test Note",
            content="Appended content",
            include_in_prompt=True,
            append=True,
        )
        assert "updated" in result

        # Verify appended content
        note = await db.notes.get_by_title("Test Note")
        assert note is not None
        assert note["content"] == "Initial content\nAppended content"

        # Replace the note (not append)
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="Test Note",
            content="Replaced content",
            include_in_prompt=True,
            append=False,
        )
        assert "updated" in result

        # Verify replaced content
        note = await db.notes.get_by_title("Test Note")
        assert note is not None
        assert note["content"] == "Replaced content"

        # Append to non-existing note should create it
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="New Note",
            content="New content",
            include_in_prompt=True,
            append=True,
        )
        assert "created" in result or "updated" in result

        # Verify new note content
        note = await db.notes.get_by_title("New Note")
        assert note is not None
        assert note["content"] == "New content"


@pytest.mark.asyncio
async def test_append_multiple_times(test_db_engine: Any) -> None:
    """Test appending multiple times to the same note."""
    async with DatabaseContext() as db:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db,
        )

        # Create initial note
        await add_or_update_note_tool(
            exec_context=exec_context,
            title="Multi Append",
            content="Line 1",
            include_in_prompt=True,
            append=False,
        )

        # Append multiple times
        for i in range(2, 5):
            await add_or_update_note_tool(
                exec_context=exec_context,
                title="Multi Append",
                content=f"Line {i}",
                include_in_prompt=True,
                append=True,
            )

        # Verify final content
        note = await db.notes.get_by_title("Multi Append")
        assert note is not None
        expected_content = "Line 1\nLine 2\nLine 3\nLine 4"
        assert note["content"] == expected_content


@pytest.mark.asyncio
async def test_add_or_update_note_append_postgres(pg_vector_db_engine: Any) -> None:
    """Test that the append functionality works correctly for notes with PostgreSQL."""
    async with DatabaseContext() as db:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db,
        )

        # Create initial note
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="Test Note PG",
            content="Initial content",
            include_in_prompt=True,
            append=False,
        )
        assert "created" in result or "updated" in result

        # Verify initial content
        note = await db.notes.get_by_title("Test Note PG")
        assert note is not None
        assert note["content"] == "Initial content"

        # Append to the note
        result = await add_or_update_note_tool(
            exec_context=exec_context,
            title="Test Note PG",
            content="Appended content",
            include_in_prompt=True,
            append=True,
        )
        assert "updated" in result

        # Verify appended content
        note = await db.notes.get_by_title("Test Note PG")
        assert note is not None
        assert note["content"] == "Initial content\nAppended content"
