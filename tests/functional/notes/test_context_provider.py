"""
Test the NotesContextProvider with prompt inclusion filtering.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.services.attachment_registry import AttachmentRegistry
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


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_with_attachments(
    pg_vector_db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Test NotesContextProvider displays attachment references for notes."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=pg_vector_db_engine,
    )

    # Create test note with attachments
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Register two test attachments
        attachment_id_1 = "test-attachment-1"
        attachment_id_2 = "test-attachment-2"

        await attachment_registry.register_attachment(
            db_context=db,
            attachment_id=attachment_id_1,
            source_type="user",
            source_id="test_user",
            mime_type="application/pdf",
            description="schedule.pdf",
            size=1024,
        )

        await attachment_registry.register_attachment(
            db_context=db,
            attachment_id=attachment_id_2,
            source_type="user",
            source_id="test_user",
            mime_type="image/png",
            description="calendar.png",
            size=2048,
        )

        # Create note with attachments
        await db.notes.add_or_update(
            title="Family Schedule",
            content="Monday through Friday morning meetings",
            include_in_prompt=True,
            attachment_ids=[attachment_id_1, attachment_id_2],
        )

        # Create note without attachments for comparison
        await db.notes.add_or_update(
            title="Shopping List",
            content="Milk, Bread, Eggs",
            include_in_prompt=True,
        )

    # Create context provider with attachment registry
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "note_attachment_format": "  📎 [{id}] {filename} ({mime_type})",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
        attachment_registry=attachment_registry,
    )

    # Get context fragments
    fragments = await provider.get_context_fragments()

    # Should have 1 fragment with both notes
    assert len(fragments) == 1
    notes_fragment = fragments[0]

    # Verify note content is present
    assert "Family Schedule" in notes_fragment
    assert "Monday through Friday morning meetings" in notes_fragment
    assert "Shopping List" in notes_fragment
    assert "Milk, Bread, Eggs" in notes_fragment

    # Verify attachment references are present for the first note
    assert f"📎 [{attachment_id_1}]" in notes_fragment
    assert "schedule.pdf" in notes_fragment
    assert "application/pdf" in notes_fragment
    assert f"📎 [{attachment_id_2}]" in notes_fragment
    assert "calendar.png" in notes_fragment
    assert "image/png" in notes_fragment

    # Verify attachment references appear after the note content
    # This ensures proper formatting
    family_schedule_pos = notes_fragment.find("Family Schedule")
    first_attachment_pos = notes_fragment.find(f"📎 [{attachment_id_1}]")
    shopping_list_pos = notes_fragment.find("Shopping List")

    assert family_schedule_pos < first_attachment_pos < shopping_list_pos


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_context_provider_handles_missing_attachments(
    pg_vector_db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Test NotesContextProvider handles missing attachments gracefully."""
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=pg_vector_db_engine,
    )

    # Create note with reference to non-existent attachment
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Test Note",
            content="This note references a missing attachment",
            include_in_prompt=True,
            attachment_ids=["non-existent-attachment-id"],
        )

    # Create context provider
    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
        "note_attachment_format": "  📎 [{id}] {filename} ({mime_type})",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
        attachment_registry=attachment_registry,
    )

    # Get context fragments - should not raise an exception
    fragments = await provider.get_context_fragments()

    # Should have 1 fragment with the note content
    assert len(fragments) == 1
    notes_fragment = fragments[0]

    # Note content should be present
    assert "Test Note" in notes_fragment
    assert "This note references a missing attachment" in notes_fragment

    # Missing attachment should not be displayed (logged as warning instead)
    assert "non-existent-attachment-id" not in notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_clearing_attachments_with_empty_list(
    pg_vector_db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Test that passing an empty list clears attachments from a note.

    This tests the fix for a bug where empty list [] was treated as None,
    which preserved existing attachments instead of clearing them.
    """
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=pg_vector_db_engine,
    )

    # Create test note with attachments
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Register test attachment
        attachment_id = "test-attachment-clear"

        await attachment_registry.register_attachment(
            db_context=db,
            attachment_id=attachment_id,
            source_type="user",
            source_id="test_user",
            mime_type="application/pdf",
            description="document.pdf",
            size=1024,
        )

        # Create note with attachment
        await db.notes.add_or_update(
            title="Note With Attachments",
            content="This note has an attachment",
            include_in_prompt=True,
            attachment_ids=[attachment_id],
        )

        # Verify attachment was added
        note = await db.notes.get_by_title(
            "Note With Attachments", visibility_grants=None
        )
        assert note is not None
        assert len(note.attachment_ids) == 1

        # Now clear attachments by passing empty list
        await db.notes.add_or_update(
            title="Note With Attachments",
            content="This note has an attachment",
            include_in_prompt=True,
            attachment_ids=[],  # Empty list should clear attachments
        )

        # Verify attachments were cleared
        note_after = await db.notes.get_by_title(
            "Note With Attachments", visibility_grants=None
        )
        assert note_after is not None
        attachment_ids = note_after.attachment_ids
        # Handle case where attachment_ids is a JSON string
        if isinstance(attachment_ids, str):
            attachment_ids = json.loads(attachment_ids)
        assert len(attachment_ids) == 0, (
            f"Attachments should be cleared, but got: {attachment_ids}"
        )


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_notes_preserving_attachments_when_not_specified(
    pg_vector_db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Test that not passing attachment_ids preserves existing attachments.

    This ensures that updating a note without specifying attachment_ids
    does not accidentally clear existing attachments.
    """
    # Clean up any existing notes
    await cleanup_notes(pg_vector_db_engine)

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path / "attachments"),
        db_engine=pg_vector_db_engine,
    )

    # Create test note with attachments
    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        # Register test attachment
        attachment_id = "test-attachment-preserve"

        await attachment_registry.register_attachment(
            db_context=db,
            attachment_id=attachment_id,
            source_type="user",
            source_id="test_user",
            mime_type="image/png",
            description="image.png",
            size=2048,
        )

        # Create note with attachment
        await db.notes.add_or_update(
            title="Note To Preserve",
            content="Original content",
            include_in_prompt=True,
            attachment_ids=[attachment_id],
        )

        # Verify attachment was added
        note = await db.notes.get_by_title("Note To Preserve", visibility_grants=None)
        assert note is not None
        assert len(note.attachment_ids) == 1

        # Update note content without specifying attachment_ids
        await db.notes.add_or_update(
            title="Note To Preserve",
            content="Updated content",
            include_in_prompt=True,
            # attachment_ids not specified - should preserve existing
        )

        # Verify attachments were preserved
        note_after = await db.notes.get_by_title(
            "Note To Preserve", visibility_grants=None
        )
        assert note_after is not None
        attachment_ids = note_after.attachment_ids
        # Handle case where attachment_ids is a JSON string
        if isinstance(attachment_ids, str):
            attachment_ids = json.loads(attachment_ids)
        assert len(attachment_ids) == 1, (
            f"Attachments should be preserved, but got: {attachment_ids}"
        )
        assert attachment_id in attachment_ids
