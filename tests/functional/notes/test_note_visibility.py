import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table
from family_assistant.tools.notes import add_or_update_note_tool, delete_note_tool
from family_assistant.tools.types import ToolExecutionContext


async def cleanup_notes(engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=engine) as db:
        stmt = delete(notes_table)
        await db.execute_with_retry(stmt)


@pytest.mark.asyncio
async def test_create_note_with_visibility_labels(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Sensitive Info",
            content="Top secret data",
            visibility_labels=["sensitive", "private"],
        )

        note = await db.notes.get_by_title("Sensitive Info", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == ["sensitive", "private"]


@pytest.mark.asyncio
async def test_update_preserves_visibility_labels(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Labeled Note",
            content="Original content",
            visibility_labels=["sensitive"],
        )

        await db.notes.add_or_update(
            title="Labeled Note",
            content="Updated content",
        )

        note = await db.notes.get_by_title("Labeled Note", visibility_grants=None)
        assert note is not None
        assert note.content == "Updated content"
        assert note.visibility_labels == ["sensitive"]


@pytest.mark.asyncio
async def test_update_clears_visibility_labels(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Was Labeled",
            content="Some content",
            visibility_labels=["sensitive"],
        )

        await db.notes.add_or_update(
            title="Was Labeled",
            content="Some content",
            visibility_labels=[],
        )

        note = await db.notes.get_by_title("Was Labeled", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == []


@pytest.mark.asyncio
async def test_get_prompt_notes_with_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Public Note",
            content="Visible to all",
            visibility_labels=[],
        )
        await db.notes.add_or_update(
            title="Sensitive Note",
            content="Only for sensitive",
            visibility_labels=["sensitive"],
        )
        await db.notes.add_or_update(
            title="Private Note",
            content="Only for private",
            visibility_labels=["private"],
        )

        notes = await db.notes.get_prompt_notes(visibility_grants={"sensitive"})
        titles = [n.title for n in notes]
        assert "Public Note" in titles
        assert "Sensitive Note" in titles
        assert "Private Note" not in titles


@pytest.mark.asyncio
async def test_get_prompt_notes_empty_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Public Note",
            content="No labels",
            visibility_labels=[],
        )
        await db.notes.add_or_update(
            title="Labeled Note",
            content="Has a label",
            visibility_labels=["sensitive"],
        )

        notes = await db.notes.get_prompt_notes(visibility_grants=set())
        titles = [n.title for n in notes]
        assert "Public Note" in titles
        assert "Labeled Note" not in titles


@pytest.mark.asyncio
async def test_get_by_title_insufficient_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Secret Note",
            content="Very secret",
            visibility_labels=["top-secret"],
        )

        note = await db.notes.get_by_title("Secret Note", visibility_grants={"default"})
        assert note is None

        note = await db.notes.get_by_title(
            "Secret Note", visibility_grants={"top-secret"}
        )
        assert note is not None
        assert note.title == "Secret Note"


@pytest.mark.asyncio
async def test_get_excluded_notes_titles_respects_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Excluded Public",
            content="Excluded but public",
            include_in_prompt=False,
            visibility_labels=[],
        )
        await db.notes.add_or_update(
            title="Excluded Sensitive",
            content="Excluded and sensitive",
            include_in_prompt=False,
            visibility_labels=["sensitive"],
        )

        titles = await db.notes.get_excluded_notes_titles(visibility_grants={"default"})
        assert "Excluded Public" in titles
        assert "Excluded Sensitive" not in titles


@pytest.mark.asyncio
async def test_get_all_with_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Default Note",
            content="Visible with default grant",
            visibility_labels=["default"],
        )
        await db.notes.add_or_update(
            title="No Labels",
            content="Always visible",
            visibility_labels=[],
        )
        await db.notes.add_or_update(
            title="Admin Only",
            content="Admin content",
            visibility_labels=["admin"],
        )

        notes = await db.notes.get_all(visibility_grants={"default"})
        titles = [n.title for n in notes]
        assert "Default Note" in titles
        assert "No Labels" in titles
        assert "Admin Only" not in titles


@pytest.mark.asyncio
async def test_and_semantics(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Multi Label Note",
            content="Needs both grants",
            visibility_labels=["sensitive", "private"],
        )

        notes_one_grant = await db.notes.get_all(visibility_grants={"sensitive"})
        assert not any(n.title == "Multi Label Note" for n in notes_one_grant)

        notes_other_grant = await db.notes.get_all(visibility_grants={"private"})
        assert not any(n.title == "Multi Label Note" for n in notes_other_grant)

        notes_both_grants = await db.notes.get_all(
            visibility_grants={"sensitive", "private"}
        )
        assert any(n.title == "Multi Label Note" for n in notes_both_grants)


@pytest.mark.asyncio
async def test_no_grants_returns_all(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Public",
            content="No labels",
            visibility_labels=[],
        )
        await db.notes.add_or_update(
            title="Sensitive",
            content="Has label",
            visibility_labels=["sensitive"],
        )
        await db.notes.add_or_update(
            title="Multi",
            content="Multiple labels",
            visibility_labels=["a", "b"],
        )

        notes = await db.notes.get_all(visibility_grants=None)
        titles = [n.title for n in notes]
        assert "Public" in titles
        assert "Sensitive" in titles
        assert "Multi" in titles


@pytest.mark.asyncio
async def test_context_provider_with_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Sensitive Note",
            content="Sensitive content here",
            include_in_prompt=True,
            visibility_labels=["sensitive"],
        )
        await db.notes.add_or_update(
            title="Public Note",
            content="Public content here",
            include_in_prompt=True,
            visibility_labels=[],
        )

    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
        visibility_grants={"default"},
    )

    fragments = await provider.get_context_fragments()
    assert len(fragments) >= 1
    combined = "\n".join(fragments)
    assert "Public Note" in combined
    assert "Public content here" in combined
    assert "Sensitive Note" not in combined
    assert "Sensitive content here" not in combined


@pytest.mark.asyncio
async def test_context_provider_without_grants(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Sensitive Note",
            content="Sensitive content here",
            include_in_prompt=True,
            visibility_labels=["sensitive"],
        )
        await db.notes.add_or_update(
            title="Public Note",
            content="Public content here",
            include_in_prompt=True,
            visibility_labels=[],
        )

    test_prompts = {
        "note_item_format": "- {title}: {content}",
        "notes_context_header": "Relevant notes:\n{notes_list}",
    }

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=test_prompts,
        visibility_grants=None,
    )

    fragments = await provider.get_context_fragments()
    assert len(fragments) >= 1
    combined = "\n".join(fragments)
    assert "Public Note" in combined
    assert "Sensitive Note" in combined


def _make_tool_context(
    db_context: DatabaseContext,
    visibility_grants: set[str] | None = None,
    default_note_visibility_labels: list[str] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test",
        user_name="tester",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        visibility_grants=visibility_grants,
        default_note_visibility_labels=default_note_visibility_labels,
    )


@pytest.mark.asyncio
async def test_update_note_blocked_by_visibility(
    db_engine: AsyncEngine,
) -> None:
    """Cannot update a note that exists but is invisible to the user."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Secret Note",
            content="Original secret content",
            visibility_labels=["top-secret"],
        )

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants={"default"})
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Secret Note",
            content="Overwritten content",
        )
        assert "insufficient visibility permissions" in result.lower()

    # Verify original content is unchanged
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Secret Note", visibility_grants=None)
        assert note is not None
        assert note.content == "Original secret content"


@pytest.mark.asyncio
async def test_update_note_allowed_with_grants(
    db_engine: AsyncEngine,
) -> None:
    """Can update a note when visibility grants are sufficient."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Labeled Note",
            content="Original content",
            visibility_labels=["sensitive"],
        )

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants={"sensitive"})
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Labeled Note",
            content="Updated content",
        )
        assert "successfully" in result.lower()

    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Labeled Note", visibility_grants=None)
        assert note is not None
        assert note.content == "Updated content"


@pytest.mark.asyncio
async def test_create_new_note_with_grants(
    db_engine: AsyncEngine,
) -> None:
    """Creating a new note works when no conflicting title exists."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants={"default"})
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Brand New Note",
            content="Fresh content",
        )
        assert "successfully" in result.lower()

    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Brand New Note", visibility_grants=None)
        assert note is not None
        assert note.content == "Fresh content"


@pytest.mark.asyncio
async def test_delete_note_blocked_by_visibility(
    db_engine: AsyncEngine,
) -> None:
    """Cannot delete a note that is invisible to the user."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Protected Note",
            content="Protected content",
            visibility_labels=["admin"],
        )

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants={"default"})
        result = await delete_note_tool(title="Protected Note", exec_context=ctx)
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    # Verify note still exists
    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Protected Note", visibility_grants=None)
        assert note is not None


@pytest.mark.asyncio
async def test_delete_note_allowed_with_grants(
    db_engine: AsyncEngine,
) -> None:
    """Can delete a note when visibility grants are sufficient."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Deletable Note",
            content="Will be deleted",
            visibility_labels=["sensitive"],
        )

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants={"sensitive"})
        result = await delete_note_tool(title="Deletable Note", exec_context=ctx)
        assert result["success"] is True

    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Deletable Note", visibility_grants=None)
        assert note is None


@pytest.mark.asyncio
async def test_update_note_no_grants_allows_all(
    db_engine: AsyncEngine,
) -> None:
    """With visibility_grants=None (no filtering), all notes are accessible."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Any Note",
            content="Original",
            visibility_labels=["admin"],
        )

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(db, visibility_grants=None)
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Any Note",
            content="Updated by admin",
        )
        assert "successfully" in result.lower()


@pytest.mark.asyncio
async def test_new_note_gets_default_labels(
    db_engine: AsyncEngine,
) -> None:
    """New notes get default visibility labels from config."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        ctx = _make_tool_context(
            db,
            visibility_grants={"default"},
            default_note_visibility_labels=["default"],
        )
        result = await add_or_update_note_tool(
            exec_context=ctx,
            title="Auto Labeled",
            content="Content",
        )
        assert "successfully" in result.lower()

    async with DatabaseContext(engine=db_engine) as db:
        note = await db.notes.get_by_title("Auto Labeled", visibility_grants=None)
        assert note is not None
        assert note.visibility_labels == ["default"]
