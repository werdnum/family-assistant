"""
Test the NotesContextProvider with prompt inclusion filtering.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import add_or_update_note


async def get_test_db_context(engine: AsyncEngine) -> DatabaseContext:
    """Helper to create DatabaseContext for testing."""
    return DatabaseContext(engine=engine)


@pytest.mark.asyncio
async def test_notes_context_provider_respects_include_in_prompt(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that NotesContextProvider only includes notes with include_in_prompt=True."""
    # Create test notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await add_or_update_note(
            db_context=db,
            title="Visible Note 1",
            content="This should appear in context",
            include_in_prompt=True,
        )
        await add_or_update_note(
            db_context=db,
            title="Hidden Note 1",
            content="This should NOT appear in context",
            include_in_prompt=False,
        )
        await add_or_update_note(
            db_context=db,
            title="Visible Note 2",
            content="This should also appear in context",
            include_in_prompt=True,
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Verify we got a fragment
    assert len(fragments) == 1
    context_text = fragments[0]

    # Verify included notes are present
    assert "Visible Note 1" in context_text
    assert "This should appear in context" in context_text
    assert "Visible Note 2" in context_text
    assert "This should also appear in context" in context_text

    # Verify excluded note is NOT present
    assert "Hidden Note 1" not in context_text
    assert "This should NOT appear in context" not in context_text


@pytest.mark.asyncio
async def test_notes_context_provider_empty_when_all_excluded(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test NotesContextProvider behavior when all notes are excluded from prompts."""
    # Create only excluded notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await add_or_update_note(
            db_context=db,
            title="Hidden Note A",
            content="Excluded content A",
            include_in_prompt=False,
        )
        await add_or_update_note(
            db_context=db,
            title="Hidden Note B",
            content="Excluded content B",
            include_in_prompt=False,
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "no_notes": "No notes configured.",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should get the "no notes" message
    assert len(fragments) == 1
    assert fragments[0] == "No notes configured."


@pytest.mark.asyncio
async def test_notes_context_provider_mixed_visibility(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test NotesContextProvider with a mix of visible and hidden notes."""
    # Create a mix of notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        test_notes = [
            ("API Keys", "Secret: abc123", False),  # Should be hidden
            ("Meeting Notes", "Tomorrow at 3pm", True),  # Should be visible
            ("Personal Info", "SSN: 123-45-6789", False),  # Should be hidden
            ("Shopping List", "Milk, Bread, Eggs", True),  # Should be visible
            ("Password", "mypassword123", False),  # Should be hidden
        ]

        for title, content, include in test_notes:
            await add_or_update_note(
                db_context=db,
                title=title,
                content=content,
                include_in_prompt=include,
            )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()
    assert len(fragments) == 1
    context_text = fragments[0]

    # Verify only visible notes appear
    assert "Meeting Notes" in context_text
    assert "Tomorrow at 3pm" in context_text
    assert "Shopping List" in context_text
    assert "Milk, Bread, Eggs" in context_text

    # Verify hidden notes do not appear
    assert "API Keys" not in context_text
    assert "Secret: abc123" not in context_text
    assert "Personal Info" not in context_text
    assert "SSN: 123-45-6789" not in context_text
    assert "Password" not in context_text
    assert "mypassword123" not in context_text
