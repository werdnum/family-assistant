"""Tests for get_note tool fallback to file-based skills via NoteRegistry."""

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.skills import NoteRegistry, ParsedSkill
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table
from family_assistant.tools.notes import get_note_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult


async def cleanup_notes(engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=engine) as db:
        stmt = delete(notes_table)
        await db.execute_with_retry(stmt)


def make_exec_context(
    db_context: DatabaseContext,
    note_registry: NoteRegistry | None = None,
    visibility_grants: set[str] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        conversation_id="test_conversation",
        interface_type="test",
        turn_id="turn_123",
        user_name="test_user",
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        note_registry=note_registry,
        visibility_grants=visibility_grants,
    )


# ast-grep-ignore: no-dict-any - ToolResult.data is a union type
def _data(result: ToolResult) -> dict[str, Any]:
    """Extract data dict from ToolResult for assertions."""
    assert isinstance(result.data, dict)
    return cast("dict[str, Any]", result.data)


@pytest.mark.asyncio
async def test_get_note_returns_db_note(db_engine: AsyncEngine) -> None:
    """get_note should return DB note when it exists."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="My Note", content="DB content.", include_in_prompt=True
        )

        exec_context = make_exec_context(db)
        result = await get_note_tool(title="My Note", exec_context=exec_context)

    data = _data(result)
    assert data["exists"] is True
    assert data["title"] == "My Note"
    assert data["content"] == "DB content."


@pytest.mark.asyncio
async def test_get_note_falls_back_to_file_skill(db_engine: AsyncEngine) -> None:
    """get_note should return file-based skill when DB note doesn't exist."""
    await cleanup_notes(db_engine)

    registry = NoteRegistry([
        ParsedSkill(
            name="Research Assistant",
            description="Help with research.",
            content="# Research\n\nSearch and synthesize.",
            source_path=Path("/fake/research.md"),
        ),
    ])

    async with DatabaseContext(engine=db_engine) as db:
        exec_context = make_exec_context(db, note_registry=registry)
        result = await get_note_tool(
            title="Research Assistant", exec_context=exec_context
        )

    data = _data(result)
    assert data["exists"] is True
    assert data["title"] == "Research Assistant"
    assert data["content"] == "# Research\n\nSearch and synthesize."
    assert data["source"] == "file"


@pytest.mark.asyncio
async def test_get_note_db_overrides_file_skill(db_engine: AsyncEngine) -> None:
    """DB note should take priority over file-based skill with same name."""
    await cleanup_notes(db_engine)

    registry = NoteRegistry([
        ParsedSkill(
            name="Research Assistant",
            description="File version.",
            content="File content.",
            source_path=Path("/fake/research.md"),
        ),
    ])

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Research Assistant",
            content="DB version of research assistant.",
            include_in_prompt=False,
        )

        exec_context = make_exec_context(db, note_registry=registry)
        result = await get_note_tool(
            title="Research Assistant", exec_context=exec_context
        )

    data = _data(result)
    assert data["exists"] is True
    assert data["content"] == "DB version of research assistant."
    assert "source" not in data  # DB notes don't have source field


@pytest.mark.asyncio
async def test_get_note_file_skill_respects_visibility(
    db_engine: AsyncEngine,
) -> None:
    """File-based skills with visibility labels should be filtered by grants."""
    await cleanup_notes(db_engine)

    registry = NoteRegistry([
        ParsedSkill(
            name="Internal Skill",
            description="Secret stuff.",
            content="Internal instructions.",
            source_path=Path("/fake/internal.md"),
            visibility_labels=frozenset({"internal"}),
        ),
    ])

    async with DatabaseContext(engine=db_engine) as db:
        # Without grants
        exec_context = make_exec_context(
            db, note_registry=registry, visibility_grants=set()
        )
        result = await get_note_tool(title="Internal Skill", exec_context=exec_context)
        data = _data(result)
        assert data["exists"] is False

        # With matching grants
        exec_context_with_grants = make_exec_context(
            db, note_registry=registry, visibility_grants={"internal"}
        )
        result = await get_note_tool(
            title="Internal Skill", exec_context=exec_context_with_grants
        )
        data = _data(result)
        assert data["exists"] is True
        assert data["content"] == "Internal instructions."


@pytest.mark.asyncio
async def test_get_note_not_found_anywhere(db_engine: AsyncEngine) -> None:
    """get_note should return exists=False when note not in DB or registry."""
    await cleanup_notes(db_engine)

    registry = NoteRegistry([
        ParsedSkill(
            name="Some Skill",
            description="Not what we're looking for.",
            content="Content.",
            source_path=Path("/fake/some.md"),
        ),
    ])

    async with DatabaseContext(engine=db_engine) as db:
        exec_context = make_exec_context(db, note_registry=registry)
        result = await get_note_tool(
            title="Nonexistent Note", exec_context=exec_context
        )

    data = _data(result)
    assert data["exists"] is False


@pytest.mark.asyncio
async def test_get_note_no_registry(db_engine: AsyncEngine) -> None:
    """get_note should work fine with no NoteRegistry (backwards compat)."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        exec_context = make_exec_context(db, note_registry=None)
        result = await get_note_tool(title="Missing", exec_context=exec_context)

    data = _data(result)
    assert data["exists"] is False
