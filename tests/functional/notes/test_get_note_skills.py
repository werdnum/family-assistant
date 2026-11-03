"""Tests for get_note tool fallback to file-based skills via NoteRegistry."""

from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.security.note_provenance import NoteProvenanceStamp
from family_assistant.security.taint import SourceTrustTier
from family_assistant.skills import NoteRegistry, ParsedSkill
from family_assistant.storage.database import Database
from family_assistant.storage.notes import notes_table
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.tools.notes import get_note_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.functional.notes.ambient_helpers import state_at, write_note


async def cleanup_notes(engine: AsyncEngine) -> None:
    db = Database(engine=engine)
    stmt = delete(notes_table)
    await db.execute(stmt)


def make_exec_context(
    db_context: Database,
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
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        note_registry=note_registry,
        visibility_grants=visibility_grants,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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

    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="My Note",
        content="DB content.",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
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

    db = Database(engine=db_engine)
    exec_context = make_exec_context(db, note_registry=registry)
    result = await get_note_tool(title="Research Assistant", exec_context=exec_context)

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

    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="Research Assistant",
        content="DB version of research assistant.",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    exec_context = make_exec_context(db, note_registry=registry)
    result = await get_note_tool(title="Research Assistant", exec_context=exec_context)

    data = _data(result)
    assert data["exists"] is True
    assert data["content"] == "DB version of research assistant."
    assert "source" not in data  # DB notes don't have source field


SKILL_WITH_TOOLS = (
    "---\n"
    "name: Home\n"
    "description: Lights, vacuum and heating.\n"
    "activate_tools:\n"
    "  - call_home_assistant_action\n"
    "activate_mcp_servers:\n"
    "  - homeassistant\n"
    "---\n"
    "Find the entity, then call the action."
)


@pytest.mark.asyncio
async def test_database_skill_declares_its_tools(db_engine: AsyncEngine) -> None:
    await cleanup_notes(db_engine)
    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="Home",
        content=SKILL_WITH_TOOLS,
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    result = await get_note_tool(title="Home", exec_context=make_exec_context(db))

    data = _data(result)
    assert data["activate_tools"] == ["call_home_assistant_action"]
    assert data["activate_mcp_servers"] == ["homeassistant"]


@pytest.mark.asyncio
async def test_activation_keys_in_an_ordinary_note_do_nothing(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)
    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="Not a skill",
        content="---\nactivate_tools:\n  - call_home_assistant_action\n---\nBody.",
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    result = await get_note_tool(
        title="Not a skill", exec_context=make_exec_context(db)
    )

    assert "activate_tools" not in _data(result)


@pytest.mark.asyncio
async def test_skill_kept_out_of_the_catalog_declares_nothing(
    db_engine: AsyncEngine,
) -> None:
    await cleanup_notes(db_engine)
    db = Database(engine=db_engine)
    await write_note(
        db,
        "Home",
        SKILL_WITH_TOOLS,
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
        include_in_prompt=False,
    )

    result = await get_note_tool(title="Home", exec_context=make_exec_context(db))

    data = _data(result)
    assert data["exists"] is True
    assert "activate_tools" not in data


@pytest.mark.asyncio
async def test_unreviewed_note_cannot_answer_for_a_skill_name(
    db_engine: AsyncEngine,
) -> None:
    """The catalog lists the built-in, so get_note must not serve the note."""
    await cleanup_notes(db_engine)
    registry = NoteRegistry([
        ParsedSkill(
            name="Home",
            description="The built-in skill.",
            content="Built-in steps.",
            source_path=Path("/fake/home.md"),
        ),
    ])
    db = Database(engine=db_engine)
    await write_note(
        db,
        "Web digest",
        SKILL_WITH_TOOLS,
        provenance=NoteProvenanceStamp.machine(
            state_at(SourceTrustTier.UNKNOWN_EXTERNAL)
        ),
        include_in_prompt=False,
    )

    result = await get_note_tool(
        title="Home", exec_context=make_exec_context(db, note_registry=registry)
    )

    assert _data(result)["content"] == "Built-in steps."


@pytest.mark.asyncio
async def test_get_note_finds_a_database_skill_by_its_catalog_name(
    db_engine: AsyncEngine,
) -> None:
    """The catalog lists a skill's name, which need not be its note's title."""
    await cleanup_notes(db_engine)
    db = Database(engine=db_engine)
    await db.notes.add_or_update(
        title="Our house",
        content=SKILL_WITH_TOOLS,
        include_in_prompt=False,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
        provenance=NoteProvenanceStamp.internal(),
    )

    result = await get_note_tool(title="Home", exec_context=make_exec_context(db))

    assert _data(result)["title"] == "Our house"


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

    db = Database(engine=db_engine)
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

    db = Database(engine=db_engine)
    exec_context = make_exec_context(db, note_registry=registry)
    result = await get_note_tool(title="Nonexistent Note", exec_context=exec_context)

    data = _data(result)
    assert data["exists"] is False


@pytest.mark.asyncio
async def test_get_note_no_registry(db_engine: AsyncEngine) -> None:
    """get_note should work fine with no NoteRegistry (backwards compat)."""
    await cleanup_notes(db_engine)

    db = Database(engine=db_engine)
    exec_context = make_exec_context(db, note_registry=None)
    result = await get_note_tool(title="Missing", exec_context=exec_context)

    data = _data(result)
    assert data["exists"] is False
