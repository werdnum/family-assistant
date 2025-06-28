"""
Test the NotesContextProvider with prompt inclusion filtering.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table


async def get_test_db_context(engine: AsyncEngine) -> DatabaseContext:
    """Helper to create DatabaseContext for testing."""
    return DatabaseContext(engine=engine)


async def cleanup_notes(engine: AsyncEngine) -> None:
    """Clean up all notes from the database."""
    async with DatabaseContext(engine=engine) as db:
        stmt = delete(notes_table)
        await db.execute_with_retry(stmt)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_respects_include_in_prompt(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that NotesContextProvider only includes notes with include_in_prompt=True."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create test notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Visible Note 1",
            content="This should appear in context",
            include_in_prompt=True,
        )
        await db.notes.add_or_update(
            title="Hidden Note 1",
            content="This should NOT appear in context",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Visible Note 2",
            content="This should also appear in context",
            include_in_prompt=True,
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should get 2 fragments: included notes and excluded notes list
    assert len(fragments) == 2

    included_notes_fragment = fragments[0]
    excluded_notes_fragment = fragments[1]

    # Verify included notes are present
    assert "Visible Note 1" in included_notes_fragment
    assert "This should appear in context" in included_notes_fragment
    assert "Visible Note 2" in included_notes_fragment
    assert "This should also appear in context" in included_notes_fragment

    # Verify excluded note is NOT present in included notes
    assert "Hidden Note 1" not in included_notes_fragment
    assert "This should NOT appear in context" not in included_notes_fragment

    # Verify excluded note title appears in excluded list
    assert '"Hidden Note 1"' in excluded_notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_empty_when_all_excluded(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test NotesContextProvider behavior when all notes are excluded from prompts."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create only excluded notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Hidden Note A",
            content="Excluded content A",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Hidden Note B",
            content="Excluded content B",
            include_in_prompt=False,
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "no_notes": "No notes configured.",
        "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should get 2 fragments: "no notes" message and excluded notes list
    assert len(fragments) == 2
    assert fragments[0] == "No notes configured."

    # Should also show the excluded notes
    excluded_notes_fragment = fragments[1]
    assert '"Hidden Note A"' in excluded_notes_fragment
    assert '"Hidden Note B"' in excluded_notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_mixed_visibility(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test NotesContextProvider with a mix of visible and hidden notes."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

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
            await db.notes.add_or_update(
                title=title,
                content=content,
                include_in_prompt=include,
            )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should get 2 fragments: included notes and excluded notes list
    assert len(fragments) == 2

    included_notes_fragment = fragments[0]
    excluded_notes_fragment = fragments[1]

    # Verify only visible notes appear in included notes
    assert "Meeting Notes" in included_notes_fragment
    assert "Tomorrow at 3pm" in included_notes_fragment
    assert "Shopping List" in included_notes_fragment
    assert "Milk, Bread, Eggs" in included_notes_fragment

    # Verify hidden notes do not appear in included notes content
    assert "API Keys" not in included_notes_fragment
    assert "Secret: abc123" not in included_notes_fragment
    assert "Personal Info" not in included_notes_fragment
    assert "SSN: 123-45-6789" not in included_notes_fragment
    assert "Password" not in included_notes_fragment
    assert "mypassword123" not in included_notes_fragment

    # Verify hidden note titles appear in excluded list
    assert '"API Keys"' in excluded_notes_fragment
    assert '"Personal Info"' in excluded_notes_fragment
    assert '"Password"' in excluded_notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_shows_excluded_notes_list(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that NotesContextProvider shows a list of excluded note titles."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create test notes with mix of included and excluded
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Public Note 1",
            content="This is visible content",
            include_in_prompt=True,
        )
        await db.notes.add_or_update(
            title="Secret Note A",
            content="Hidden content A",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Private Data B",
            content="Hidden content B",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Public Note 2",
            content="Another visible note",
            include_in_prompt=True,
        )

    # Create context provider with excluded notes format
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should have 2 fragments: included notes and excluded notes list
    assert len(fragments) == 2

    included_notes_fragment = fragments[0]
    excluded_notes_fragment = fragments[1]

    # Verify included notes content
    assert "Public Note 1" in included_notes_fragment
    assert "This is visible content" in included_notes_fragment
    assert "Public Note 2" in included_notes_fragment
    assert "Another visible note" in included_notes_fragment

    # Verify excluded notes list format
    assert "Other available notes (not included above):" in excluded_notes_fragment
    assert '"Private Data B"' in excluded_notes_fragment
    assert '"Secret Note A"' in excluded_notes_fragment

    # Verify excluded note contents are NOT shown
    assert "Hidden content A" not in excluded_notes_fragment
    assert "Hidden content B" not in excluded_notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_no_excluded_list_when_all_included(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that no excluded notes list appears when all notes are included."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create only included notes
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Note 1",
            content="Content 1",
            include_in_prompt=True,
        )
        await db.notes.add_or_update(
            title="Note 2",
            content="Content 2",
            include_in_prompt=True,
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should only have 1 fragment (included notes, no excluded list)
    assert len(fragments) == 1
    assert "Note 1" in fragments[0]
    assert "Note 2" in fragments[0]
    assert "Other available notes" not in fragments[0]
