"""Database fixtures preserve independent durable writes across tests."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteWritePolicy


@pytest.mark.parametrize("content", ["first database", "second database"])
async def test_database_fixture_starts_empty_and_preserves_commits(
    db_engine: AsyncEngine, content: str
) -> None:
    """Reusing schema must not reuse rows or require an open test transaction."""
    db = Database(db_engine)
    title = "fixture isolation sentinel"
    assert await db.notes.get_by_title(title, visibility_grants=None) is None

    await db.notes.add_or_update(
        title=title, content=content, write_policy=NoteWritePolicy.UNCONSTRAINED
    )
    # Reinitializing an existing database exercises the Alembic-managed path.
    await init_db(db_engine)
    await db_engine.dispose()
    note = await Database(db_engine).notes.get_by_title(title, visibility_grants=None)
    assert note is not None
    assert note.content == content
