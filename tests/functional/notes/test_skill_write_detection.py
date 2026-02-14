"""Tests for write-time skill detection via is_skill column."""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.notes import notes_table

SKILL_CONTENT = (
    "---\n"
    "name: Email Drafting\n"
    "description: Draft professional emails.\n"
    "---\n"
    "# Email Drafting\n\n"
    "When drafting emails, consider the audience."
)


async def cleanup_notes(engine: AsyncEngine) -> None:
    async with DatabaseContext(engine=engine) as db:
        stmt = delete(notes_table)
        await db.execute_with_retry(stmt)


@pytest.mark.asyncio
async def test_skill_detected_on_create(db_engine: AsyncEngine) -> None:
    """Creating a note with skill frontmatter sets is_skill=True and metadata."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="My Skill",
            content=SKILL_CONTENT,
            include_in_prompt=True,
        )

        note = await db.notes.get_by_title("My Skill", visibility_grants=None)

    assert note is not None
    assert note.is_skill is True
    assert note.skill_name == "Email Drafting"
    assert note.skill_description == "Draft professional emails."


@pytest.mark.asyncio
async def test_regular_note_not_detected_as_skill(db_engine: AsyncEngine) -> None:
    """Creating a regular note (no frontmatter) sets is_skill=False."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Regular Note",
            content="Just regular content.",
            include_in_prompt=True,
        )

        note = await db.notes.get_by_title("Regular Note", visibility_grants=None)

    assert note is not None
    assert note.is_skill is False
    assert note.skill_name is None
    assert note.skill_description is None


@pytest.mark.asyncio
async def test_skill_detection_updated_on_content_change(
    db_engine: AsyncEngine,
) -> None:
    """Updating a note's content re-evaluates skill detection."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        # Create as skill
        await db.notes.add_or_update(
            title="Changeable",
            content=SKILL_CONTENT,
        )
        note = await db.notes.get_by_title("Changeable", visibility_grants=None)
        assert note is not None
        assert note.is_skill is True

        # Update to regular note
        await db.notes.add_or_update(
            title="Changeable",
            content="Now just regular content.",
        )
        note = await db.notes.get_by_title("Changeable", visibility_grants=None)
        assert note is not None
        assert note.is_skill is False
        assert note.skill_name is None


@pytest.mark.asyncio
async def test_get_skills_returns_only_skills(db_engine: AsyncEngine) -> None:
    """get_skills() returns only notes where is_skill=True."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Skill Note",
            content=SKILL_CONTENT,
        )
        await db.notes.add_or_update(
            title="Regular Note",
            content="Plain content.",
        )

        skills = await db.notes.get_skills(visibility_grants=None)

    assert len(skills) == 1
    assert skills[0].title == "Skill Note"
    assert skills[0].is_skill is True
    assert skills[0].skill_name == "Email Drafting"


@pytest.mark.asyncio
async def test_get_prompt_notes_excludes_skills(db_engine: AsyncEngine) -> None:
    """get_prompt_notes() excludes skill notes even if include_in_prompt=True."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Regular Prompt Note",
            content="Include me.",
            include_in_prompt=True,
        )
        await db.notes.add_or_update(
            title="Skill Note",
            content=SKILL_CONTENT,
            include_in_prompt=True,
        )

        prompt_notes = await db.notes.get_prompt_notes(visibility_grants=None)

    titles = [n.title for n in prompt_notes]
    assert "Regular Prompt Note" in titles
    assert "Skill Note" not in titles


@pytest.mark.asyncio
async def test_get_excluded_notes_titles_excludes_skills(
    db_engine: AsyncEngine,
) -> None:
    """get_excluded_notes_titles() excludes skill notes."""
    await cleanup_notes(db_engine)

    async with DatabaseContext(engine=db_engine) as db:
        await db.notes.add_or_update(
            title="Hidden Regular Note",
            content="Hidden content.",
            include_in_prompt=False,
        )
        await db.notes.add_or_update(
            title="Hidden Skill",
            content=SKILL_CONTENT,
            include_in_prompt=False,
        )

        excluded = await db.notes.get_excluded_notes_titles(visibility_grants=None)

    assert "Hidden Regular Note" in excluded
    assert "Hidden Skill" not in excluded
