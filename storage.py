import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import (
    MetaData, Table, Column, String, select, insert, update, BigInteger, Integer, # Added Integer
    DateTime, Text, desc
)
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db") # Default to SQLite async

engine = create_async_engine(DATABASE_URL, echo=False) # Set echo=True for debugging SQL
metadata = MetaData()

# Define the notes table (replaces key_value_store)
notes_table = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), # Use Integer for SQLite autoincrement
    Column("title", String, nullable=False, unique=True, index=True), # Unique title (like the old key)
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)),
    Column("updated_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)),
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

async def get_all_notes() -> List[Dict[str, str]]:
    """Retrieves all notes (title and content) from the store."""
    async with engine.connect() as conn:
        stmt = select(notes_table.c.title, notes_table.c.content).order_by(notes_table.c.title)
        result = await conn.execute(stmt)
        rows = result.fetchall()
        # Return as a list of dicts for easier iteration
        return [{"title": row.title, "content": row.content} for row in rows]

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

async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title."""
    async with engine.connect() as conn:
        stmt = select(notes_table.c.title, notes_table.c.content).where(notes_table.c.title == title)
        result = await conn.execute(stmt)
        row = result.fetchone()
        if row:
            # Use ._mapping to access columns by name easily
            return row._mapping
        return None

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


async def add_or_update_note(title: str, content: str):
    """Adds a new note or updates the content if the title exists."""
    async with engine.connect() as conn:
        # Check if title exists
        select_stmt = select(notes_table).where(notes_table.c.title == title)
        result = await conn.execute(select_stmt)
        existing_note = result.fetchone()

        now = datetime.now(timezone.utc)
        if existing_note:
            # Update existing note
            stmt = (
                update(notes_table)
                .where(notes_table.c.title == title)
                .values(content=content, updated_at=now)
            )
            logger.info(f"Updating note: {title}")
        else:
            # Insert new note - omit 'id' to allow autoincrement
            stmt = insert(notes_table).values(
                title=title,
                content=content,
                created_at=now,
                updated_at=now
                # id is handled by autoincrement
            )
            logger.info(f"Inserting new note: {title}")

        await conn.execute(stmt)
        await conn.commit()

async def delete_note(title: str) -> bool:
    """Deletes a note by its title."""
    async with engine.connect() as conn:
        stmt = notes_table.delete().where(notes_table.c.title == title)
        result = await conn.execute(stmt)
        await conn.commit()
        if result.rowcount > 0:
            logger.info(f"Deleted note: {title}")
            return True
        logger.warning(f"Note not found for deletion: {title}")
        return False
