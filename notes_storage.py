"""
Handles storage and retrieval of notes.
"""

import logging
import random
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table, Column, String, Integer, Text, DateTime, select, insert, update, delete
)
from sqlalchemy.exc import DBAPIError

from db_base import metadata, get_engine

logger = logging.getLogger(__name__)
engine = get_engine()

# Define the notes table
notes_table = Table(
    "notes",
    metadata,
    Column(
        "id", Integer, primary_key=True, autoincrement=True
    ),
    Column(
        "title", String, nullable=False, unique=True, index=True
    ),
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

async def get_all_notes() -> List[Dict[str, str]]:
    """Retrieves all notes, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                stmt = select(notes_table.c.title, notes_table.c.content).order_by(
                    notes_table.c.title
                )
                result = await conn.execute(stmt)
                rows = result.fetchall()
                return [
                    {"title": row.title, "content": row.content} for row in rows
                ]
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_all_notes (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for get_all_notes. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_all_notes: {e}", exc_info=True)
            raise
    raise RuntimeError("Database operation failed for get_all_notes after multiple retries")


async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = select(notes_table.c.title, notes_table.c.content).where(notes_table.c.title == title)

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                return row._mapping if row else None
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_note_by_title (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_note_by_title({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_note_by_title({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_note_by_title({title}) after multiple retries")


async def add_or_update_note(title: str, content: str):
    """Adds/updates a note, with retries."""
    max_retries = 3
    base_delay = 0.5

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                select_stmt = select(notes_table).where(notes_table.c.title == title)
                result = await conn.execute(select_stmt)
                existing_note = result.fetchone()
                now = datetime.now(timezone.utc)
                if existing_note:
                    stmt = update(notes_table).where(notes_table.c.title == title).values(content=content, updated_at=now)
                    logger.info(f"Updating note: {title}")
                else:
                    stmt = insert(notes_table).values(title=title, content=content, created_at=now, updated_at=now)
                    logger.info(f"Inserting new note: {title}")
                await conn.execute(stmt)
                await conn.commit()
                return "Success"
        except DBAPIError as e:
            logger.warning(f"DBAPIError in add_or_update_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_or_update_note({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in add_or_update_note({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for add_or_update_note({title}) after multiple retries")


async def delete_note(title: str) -> bool:
    """Deletes a note by title, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = delete(notes_table).where(notes_table.c.title == title)

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(f"Deleted note: {title}")
                    return True
                logger.warning(f"Note not found for deletion: {title}")
                return False
        except DBAPIError as e:
            logger.warning(f"DBAPIError in delete_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for delete_note({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in delete_note({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for delete_note({title}) after multiple retries")

"""
Handles storage and retrieval of notes.
"""

import logging
import random
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table, Column, String, Integer, Text, DateTime, select, insert, update, delete
)
from sqlalchemy.exc import DBAPIError

from db_base import metadata, get_engine

logger = logging.getLogger(__name__)
engine = get_engine()

# Define the notes table
notes_table = Table(
    "notes",
    metadata,
    Column(
        "id", Integer, primary_key=True, autoincrement=True
    ),
    Column(
        "title", String, nullable=False, unique=True, index=True
    ),
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

async def get_all_notes() -> List[Dict[str, str]]:
    """Retrieves all notes, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                stmt = select(notes_table.c.title, notes_table.c.content).order_by(
                    notes_table.c.title
                )
                result = await conn.execute(stmt)
                rows = result.fetchall()
                return [
                    {"title": row.title, "content": row.content} for row in rows
                ]
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_all_notes (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for get_all_notes. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_all_notes: {e}", exc_info=True)
            raise
    raise RuntimeError("Database operation failed for get_all_notes after multiple retries")


async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = select(notes_table.c.title, notes_table.c.content).where(notes_table.c.title == title)

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                return row._mapping if row else None
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_note_by_title (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_note_by_title({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_note_by_title({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for get_note_by_title({title}) after multiple retries")


async def add_or_update_note(title: str, content: str):
    """Adds/updates a note, with retries."""
    max_retries = 3
    base_delay = 0.5

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                select_stmt = select(notes_table).where(notes_table.c.title == title)
                result = await conn.execute(select_stmt)
                existing_note = result.fetchone()
                now = datetime.now(timezone.utc)
                if existing_note:
                    stmt = update(notes_table).where(notes_table.c.title == title).values(content=content, updated_at=now)
                    logger.info(f"Updating note: {title}")
                else:
                    stmt = insert(notes_table).values(title=title, content=content, created_at=now, updated_at=now)
                    logger.info(f"Inserting new note: {title}")
                await conn.execute(stmt)
                await conn.commit()
                return "Success"
        except DBAPIError as e:
            logger.warning(f"DBAPIError in add_or_update_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_or_update_note({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in add_or_update_note({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for add_or_update_note({title}) after multiple retries")


async def delete_note(title: str) -> bool:
    """Deletes a note by title, with retries."""
    max_retries = 3
    base_delay = 0.5
    stmt = delete(notes_table).where(notes_table.c.title == title)

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(f"Deleted note: {title}")
                    return True
                logger.warning(f"Note not found for deletion: {title}")
                return False
        except DBAPIError as e:
            logger.warning(f"DBAPIError in delete_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for delete_note({title}). Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in delete_note({title}): {e}", exc_info=True)
            raise
    raise RuntimeError(f"Database operation failed for delete_note({title}) after multiple retries")

