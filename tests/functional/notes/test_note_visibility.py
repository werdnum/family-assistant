import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table


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

        note = await db.notes.get_by_title("Sensitive Info")
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

        note = await db.notes.get_by_title("Labeled Note")
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

        note = await db.notes.get_by_title("Was Labeled")
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
