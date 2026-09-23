"""Fixture: raw writes to the notes table outside the repository are flagged."""

import sqlalchemy as sa
from sqlalchemy import insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from family_assistant.storage.database import DatabaseTransaction
from family_assistant.storage.notes import notes_table


async def rewrite_content(txn: DatabaseTransaction, title: str, content: str) -> None:
    """New content lands under whatever provenance the row already had."""
    await txn.execute(
        update(notes_table).where(notes_table.c.title == title).values(content=content)
    )


async def insert_unstamped(txn: DatabaseTransaction, title: str) -> None:
    """A row with no provenance envelope at all."""
    await txn.execute(insert(notes_table).values(title=title, content=""))


async def insert_through_the_module(txn: DatabaseTransaction, title: str) -> None:
    """The module-qualified spelling is the same write."""
    await txn.execute(sa.insert(notes_table).values(title=title, content=""))


async def upsert_on_postgres(txn: DatabaseTransaction, title: str) -> None:
    """So is the dialect's upsert."""
    await txn.execute(pg_insert(notes_table).values(title=title, content=""))


async def method_spelling(txn: DatabaseTransaction, title: str) -> None:
    """And the table's own statement constructors."""
    await txn.execute(notes_table.update().where(notes_table.c.title == title))
