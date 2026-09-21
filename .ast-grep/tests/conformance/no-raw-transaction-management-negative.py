"""Fixture: transactions opened through the storage API must not be flagged."""

from sqlalchemy import text

from family_assistant.storage.database import Database, DatabaseTransaction


async def read_through_the_handle(db: Database) -> None:
    await db.fetch_all(text("SELECT 1"))


async def read_in_a_block(db: Database) -> None:
    async with db.transaction() as txn:
        await txn.execute(text("SELECT 1"))


async def read_in_a_closure(db: Database) -> None:
    async def body(txn: DatabaseTransaction) -> None:
        await txn.execute(text("SELECT 1"))

    await db.atomic(body)
