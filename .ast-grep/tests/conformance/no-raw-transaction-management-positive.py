"""Fixture: raw transaction scopes must be flagged."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def read_directly(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))


async def nest_directly(db_engine: AsyncEngine) -> None:
    async with db_engine.begin_nested():
        pass
