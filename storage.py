import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import (
    MetaData, Table, Column, String, select, insert, update, BigInteger,
    DateTime, Text, desc
)
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
    Column("value", String, nullable=False),
)

# Define the message history table
message_history = Table(
    "message_history",
    metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column("role", String, nullable=False), # 'user' or 'assistant'
    Column("content", Text, nullable=False),
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

async def add_message_to_history(
    chat_id: int, message_id: int, timestamp: datetime, role: str, content: str
):
    """Adds a message to the history table."""
    async with engine.connect() as conn:
        stmt = insert(message_history).values(
            chat_id=chat_id,
            message_id=message_id,
            timestamp=timestamp,
            role=role,
            content=content,
        )
        await conn.execute(stmt)
        await conn.commit()
        logger.debug(f"Added message {message_id} from chat {chat_id} to history.")

async def get_recent_history(
    chat_id: int, limit: int, max_age: timedelta
) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a chat, ordered chronologically."""
    cutoff_time = datetime.now(timezone.utc) - max_age
    async with engine.connect() as conn:
        stmt = (
            select(message_history.c.role, message_history.c.content)
            .where(message_history.c.chat_id == chat_id)
            .where(message_history.c.timestamp >= cutoff_time)
            .order_by(message_history.c.timestamp.desc()) # Get latest first
            .limit(limit)
        )
        result = await conn.execute(stmt)
        rows = result.fetchall()
        # Reverse to get chronological order for the LLM
        return [{"role": row.role, "content": row.content} for row in reversed(rows)]

async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID."""
    async with engine.connect() as conn:
        stmt = (
            select(message_history.c.role, message_history.c.content)
            .where(message_history.c.chat_id == chat_id)
            .where(message_history.c.message_id == message_id)
        )
        result = await conn.execute(stmt)
        row = result.fetchone()
        if row:
            return {"role": row.role, "content": row.content}
        return None

# Optional: Function to prune very old history if needed
# async def prune_history(max_age: timedelta): ...


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
