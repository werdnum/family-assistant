import logging
import os
from sqlalchemy import MetaData, Table, Column, String, select, insert, update
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db") # Default to SQLite async

engine = create_async_engine(DATABASE_URL, echo=False) # Set echo=True for debugging SQL
metadata = MetaData()

# Define the key-value table
key_value_store = Table(
    "key_value_store",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String),
)

async def init_db():
    """Initializes the database and creates tables if they don't exist."""
    async with engine.begin() as conn:
        logger.info("Initializing database schema...")
        await conn.run_sync(metadata.create_all)
        logger.info("Database schema initialized.")

async def get_all_key_values() -> dict[str, str]:
    """Retrieves all key-value pairs from the store."""
    async with engine.connect() as conn:
        result = await conn.execute(select(key_value_store))
        rows = result.fetchall()
        return {row.key: row.value for row in rows}

async def add_or_update_key_value(key: str, value: str):
    """Adds a new key-value pair or updates the value if the key exists."""
    async with engine.connect() as conn:
        # Check if key exists
        select_stmt = select(key_value_store).where(key_value_store.c.key == key)
        result = await conn.execute(select_stmt)
        exists = result.fetchone()

        if exists:
            # Update existing key
            stmt = (
                update(key_value_store)
                .where(key_value_store.c.key == key)
                .values(value=value)
            )
            logger.info(f"Updating key: {key}")
        else:
            # Insert new key-value pair
            stmt = insert(key_value_store).values(key=key, value=value)
            logger.info(f"Inserting new key: {key}")

        await conn.execute(stmt)
        await conn.commit()

async def delete_key_value(key: str):
    """Deletes a key-value pair."""
    async with engine.connect() as conn:
        stmt = key_value_store.delete().where(key_value_store.c.key == key)
        result = await conn.execute(stmt)
        await conn.commit()
        if result.rowcount > 0:
            logger.info(f"Deleted key: {key}")
            return True
        logger.warning(f"Key not found for deletion: {key}")
        return False
