"""Tests for skill catalog in NotesContextProvider and get_note tool."""

from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.context_providers import NotesContextProvider
from family_assistant.skills import NoteRegistry, ParsedSkill
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table

TEST_PROMPTS = {
    "note_item_format": "- {title}: {content}",
    "notes_context_header": "Relevant notes:\n{notes_list}",
    "excluded_notes_format": "Other available notes (not included above): {excluded_titles}",
}

SKILL_FRONTMATTER_CONTENT = (
    "---\n"
    "name: Email Drafting\n"
    "description: Draft professional emails with appropriate tone and structure.\n"
    "---\n"
    "# Email Drafting\n\n"
    "When drafting emails, consider the audience and purpose."
)


async def cleanup_notes(engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=engine) as db:
        stmt = delete(notes_table)
        await db.execute_with_retry(stmt)


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_db_skill_appears_in_catalog_not_notes(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """DB notes with skill frontmatter should appear in catalog, not regular notes."""
    await cleanup_notes(pg_vector_db_engine)

    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Regular Note",
            content="Just a normal note.",
            include_in_prompt=True,
        )
        await db.notes.add_or_update(
            title="Email Skill",
            content=SKILL_FRONTMATTER_CONTENT,
            include_in_prompt=True,
        )

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
    )

    fragments = await provider.get_context_fragments()

    # Find the fragments
    notes_fragment = next((f for f in fragments if "Regular Note" in f), None)
    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)

    # Regular note should appear in notes section
    assert notes_fragment is not None
    assert "Just a normal note." in notes_fragment

    # Skill should appear in catalog, not in notes
    assert catalog_fragment is not None
    assert "**Email Drafting**" in catalog_fragment
    assert "Draft professional emails" in catalog_fragment

    # Skill should NOT appear in regular notes
    assert "Email Skill" not in notes_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_db_skill_excluded_from_other_notes_list(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """DB skills should not appear in 'Other available notes' even if include_in_prompt=False."""
    await cleanup_notes(pg_vector_db_engine)

    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Hidden Regular Note",
            content="Regular hidden content.",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Hidden Skill",
            content=SKILL_FRONTMATTER_CONTENT,
            include_in_prompt=False,
        )

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
    )

    fragments = await provider.get_context_fragments()

    # Hidden regular note should appear in excluded list
    excluded_fragment = next(
        (f for f in fragments if "Other available notes" in f), None
    )
    assert excluded_fragment is not None
    assert '"Hidden Regular Note"' in excluded_fragment

    # Skill should be in catalog, not excluded list
    assert '"Hidden Skill"' not in excluded_fragment
    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)
    assert catalog_fragment is not None
    assert "**Email Drafting**" in catalog_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_file_skills_appear_in_catalog(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """File-based skills from NoteRegistry should appear in the skill catalog."""
    await cleanup_notes(pg_vector_db_engine)

    file_skills = [
        ParsedSkill(
            name="Research Assistant",
            description="Help with research tasks.",
            content="# Research\n\nSearch and synthesize.",
            source_path=Path("/fake/research.md"),
        ),
    ]
    registry = NoteRegistry(file_skills)

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
        note_registry=registry,
    )

    fragments = await provider.get_context_fragments()

    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)
    assert catalog_fragment is not None
    assert "**Research Assistant**" in catalog_fragment
    assert "Help with research tasks." in catalog_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mixed_db_and_file_skills_in_catalog(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Both DB skills and file skills should appear in the same catalog section."""
    await cleanup_notes(pg_vector_db_engine)

    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="DB Skill Note",
            content=SKILL_FRONTMATTER_CONTENT,
            include_in_prompt=True,
        )

    file_skills = [
        ParsedSkill(
            name="Meeting Notes",
            description="Format meeting notes.",
            content="# Meeting Notes\n\nUse this structure.",
            source_path=Path("/fake/meeting.md"),
        ),
    ]
    registry = NoteRegistry(file_skills)

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
        note_registry=registry,
    )

    fragments = await provider.get_context_fragments()

    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)
    assert catalog_fragment is not None
    assert "**Email Drafting**" in catalog_fragment
    assert "**Meeting Notes**" in catalog_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_file_skill_visibility_filtering(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """File-based skills with visibility labels should be filtered by grants."""
    await cleanup_notes(pg_vector_db_engine)

    file_skills = [
        ParsedSkill(
            name="Public Skill",
            description="Visible to all.",
            content="Public content.",
            source_path=Path("/fake/public.md"),
        ),
        ParsedSkill(
            name="Internal Skill",
            description="Only for internals.",
            content="Internal content.",
            source_path=Path("/fake/internal.md"),
            visibility_labels=frozenset({"skill_internal"}),
        ),
    ]
    registry = NoteRegistry(file_skills)

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    # Without grants, only public skill visible
    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
        note_registry=registry,
        visibility_grants=set(),
    )

    fragments = await provider.get_context_fragments()
    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)
    assert catalog_fragment is not None
    assert "**Public Skill**" in catalog_fragment
    assert "**Internal Skill**" not in catalog_fragment

    # With grants, both visible
    provider_with_grants = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
        note_registry=registry,
        visibility_grants={"skill_internal"},
    )

    fragments_with_grants = await provider_with_grants.get_context_fragments()
    catalog_fragment = next(
        (f for f in fragments_with_grants if "Available Skills" in f), None
    )
    assert catalog_fragment is not None
    assert "**Public Skill**" in catalog_fragment
    assert "**Internal Skill**" in catalog_fragment


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_no_catalog_when_no_skills(
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """No catalog section when there are no skills (DB or file-based)."""
    await cleanup_notes(pg_vector_db_engine)

    async with DatabaseContext(engine=pg_vector_db_engine) as db:
        await db.notes.add_or_update(
            title="Regular Note",
            content="Just content, no frontmatter.",
            include_in_prompt=True,
        )

    async def get_db_context_func() -> DatabaseContext:
        return DatabaseContext(engine=pg_vector_db_engine)

    provider = NotesContextProvider(
        get_db_context_func=get_db_context_func,
        prompts=TEST_PROMPTS,
    )

    fragments = await provider.get_context_fragments()
    catalog_fragment = next((f for f in fragments if "Available Skills" in f), None)
    assert catalog_fragment is None
