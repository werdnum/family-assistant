"""
Handles storage and retrieval of notes.
"""

import logging
import random
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table,
    Column,
    String,
    Integer,
    Text,
    DateTime,
    select,
    insert,
    update,
    delete,
)
from sqlalchemy.exc import SQLAlchemyError # Use broader exception

# Use absolute package path
from family_assistant.storage.base import metadata # Keep metadata
# Remove get_engine import
from family_assistant.storage.context import DatabaseContext # Import DatabaseContext

logger = logging.getLogger(__name__)

# Define the notes table
notes_table = Table(
    "notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("title", String, nullable=False, unique=True, index=True),
    Column("content", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    ),
)


async def get_all_notes(db_context: DatabaseContext) -> List[Dict[str, str]]:
    """Retrieves all notes."""
    try:
        stmt = select(notes_table.c.title, notes_table.c.content).order_by(
            notes_table.c.title
        )
        rows = await db_context.fetch_all(stmt)
        return [{"title": row["title"], "content": row["content"]} for row in rows]
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_all_notes: {e}", exc_info=True)
        raise # Re-raise after logging


async def get_note_by_title(db_context: DatabaseContext, title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title."""
    try:
        stmt = select(notes_table.c.title, notes_table.c.content).where(
            notes_table.c.title == title
        )
        row = await db_context.fetch_one(stmt)
        return row if row else None
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_note_by_title({title}): {e}", exc_info=True)
        raise


async def add_or_update_note(db_context: DatabaseContext, title: str, content: str) -> str:
    """Adds a new note or updates an existing note with the given title."""
    try:
        # Check if note exists first (within the same transaction context if possible)
        # Note: fetch_one doesn't participate in the outer transaction automatically
        # A better approach might be to use INSERT ... ON CONFLICT DO UPDATE if using PostgreSQL
        # For cross-DB compatibility, check then insert/update.
        # We'll use execute_and_commit which handles the transaction.

        select_stmt = select(notes_table.c.id).where(notes_table.c.title == title)
        existing_note = await db_context.fetch_one(select_stmt) # Check outside transaction for simplicity here

        now = datetime.now(timezone.utc)
        if existing_note:
            stmt = (
                update(notes_table)
                .where(notes_table.c.title == title)
                .values(content=content, updated_at=now)
            )
            logger.info(f"Updating note: {title}")
        else:
            stmt = insert(notes_table).values(
                title=title, content=content, created_at=now, updated_at=now
            )
            logger.info(f"Inserting new note: {title}")

        await db_context.execute_and_commit(stmt)
        return "Success"
    except SQLAlchemyError as e:
        logger.error(f"Database error in add_or_update_note({title}): {e}", exc_info=True)
        raise


async def delete_note(db_context: DatabaseContext, title: str) -> bool:
    """Deletes a note by title."""
    try:
        stmt = delete(notes_table).where(notes_table.c.title == title)
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
        deleted_count = result.rowcount
        if deleted_count > 0:
            logger.info(f"Deleted note: {title}")
            return True
        else:
            logger.warning(f"Note not found for deletion: {title}")
            return False
    except SQLAlchemyError as e:
        logger.error(f"Database error in delete_note({title}): {e}", exc_info=True)
        raise
