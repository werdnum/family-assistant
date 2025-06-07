"""
Handles storage and retrieval of notes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import SQLAlchemyError  # Use broader exception

# Use absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext
from family_assistant.storage.vector import Document  # Import Document protocol

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


@dataclass(frozen=True)
class NoteDocument(Document):
    """
    Represents a note document conforming to the Document protocol
    for vector storage ingestion.
    """

    _id: int | None
    _title: str
    _content: str
    _created_at: datetime
    _updated_at: datetime

    @property
    def id(self) -> int | None:
        return self._id

    @property
    def source_type(self) -> str:
        return "note"

    @property
    def source_id(self) -> str:
        return self._title  # Use title as unique identifier

    @property
    def source_uri(self) -> str | None:
        return None  # Notes don't have external URIs

    @property
    def title(self) -> str | None:
        return self._title

    @property
    def created_at(self) -> datetime | None:
        return self._created_at

    @property
    def metadata(self) -> dict[str, Any] | None:
        return {
            "title": self._title,
            "created_at": self._created_at.isoformat(),
            "updated_at": self._updated_at.isoformat(),
        }


async def get_all_notes(db_context: DatabaseContext) -> list[dict[str, str]]:
    """Retrieves all notes."""
    try:
        stmt = select(notes_table.c.title, notes_table.c.content).order_by(
            notes_table.c.title
        )
        rows = await db_context.fetch_all(stmt)
        return [{"title": row["title"], "content": row["content"]} for row in rows]
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_all_notes: {e}", exc_info=True)
        raise  # Re-raise after logging


async def get_note_by_title(
    db_context: DatabaseContext, title: str
) -> dict[str, Any] | None:
    """Retrieves a specific note by its title."""
    try:
        stmt = select(notes_table.c.title, notes_table.c.content).where(
            notes_table.c.title == title
        )
        row = await db_context.fetch_one(stmt)
        return row if row else None
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in get_note_by_title({title}): {e}", exc_info=True
        )
        raise


async def get_note_by_id(
    db_context: DatabaseContext, note_id: int
) -> dict[str, Any] | None:
    """Retrieves a specific note by its ID."""
    try:
        stmt = select(
            notes_table.c.id,
            notes_table.c.title,
            notes_table.c.content,
            notes_table.c.created_at,
            notes_table.c.updated_at,
        ).where(notes_table.c.id == note_id)
        row = await db_context.fetch_one(stmt)
        return dict(row) if row else None
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_note_by_id({note_id}): {e}", exc_info=True)
        raise


async def add_or_update_note(
    db_context: DatabaseContext, title: str, content: str
) -> str:
    """Adds a new note or updates an existing note with the given title (upsert)."""
    now = datetime.now(timezone.utc)

    if db_context.engine.dialect.name == "postgresql":
        # Use PostgreSQL's ON CONFLICT DO UPDATE for atomic upsert
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(notes_table).values(
                title=title, content=content, created_at=now, updated_at=now
            )
            # Define columns to update on conflict
            update_dict = {
                "content": stmt.excluded.content,
                "updated_at": stmt.excluded.updated_at,
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["title"],  # The unique constraint column
                set_=update_dict,
            )
            # Use execute_with_retry as commit is handled by context manager
            await db_context.execute_with_retry(stmt)
            logger.info(f"Successfully added/updated note: {title} (using ON CONFLICT)")

            # Enqueue indexing task
            await _enqueue_note_indexing_task(db_context, title)
            return "Success"
        except SQLAlchemyError as e:
            logger.error(
                f"PostgreSQL error in add_or_update_note({title}): {e}", exc_info=True
            )
            raise

    else:
        # Fallback for SQLite and other dialects: Try INSERT, then UPDATE on IntegrityError.
        # The surrounding DatabaseContext handles the overall transaction commit/rollback.
        try:
            # Attempt INSERT first
            insert_stmt = insert(notes_table).values(
                title=title, content=content, created_at=now, updated_at=now
            )
            await db_context.execute_with_retry(insert_stmt)
            logger.info(f"Inserted new note: {title} (SQLite fallback)")

            # Enqueue indexing task
            await _enqueue_note_indexing_task(db_context, title)
            return "Success"
        except SQLAlchemyError as e:
            # Check specifically for unique constraint violation (IntegrityError in SQLAlchemy)
            from sqlalchemy.exc import IntegrityError

            if isinstance(e, IntegrityError):  # Check only for IntegrityError
                logger.info(
                    f"Note '{title}' already exists (SQLite fallback), attempting update."
                )
                # Perform UPDATE if INSERT failed due to unique constraint
                update_stmt = (
                    update(notes_table)
                    .where(notes_table.c.title == title)
                    .values(content=content, updated_at=now)
                )
                # Execute update within the same transaction context
                result = await db_context.execute_with_retry(update_stmt)
                if result.rowcount == 0:  # type: ignore[attr-defined]
                    # This could happen if the note was deleted between the failed INSERT and this UPDATE
                    logger.error(
                        f"Update failed for note '{title}' after insert conflict (SQLite fallback). Note might have been deleted concurrently."
                    )
                    # Re-raise the original error or a custom one
                    raise RuntimeError(
                        f"Failed to update note '{title}' after insert conflict."
                    ) from e
                logger.info(f"Updated note: {title} (SQLite fallback)")

                # Enqueue indexing task
                await _enqueue_note_indexing_task(db_context, title)
                return "Success"
            else:
                # Re-raise other SQLAlchemy errors
                logger.error(
                    f"Database error during INSERT in add_or_update_note({title}) (SQLite fallback): {e}",
                    exc_info=True,
                )
                raise e


async def delete_note(db_context: DatabaseContext, title: str) -> bool:
    """Deletes a note by title."""
    try:
        stmt = delete(notes_table).where(notes_table.c.title == title)
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]
        if deleted_count > 0:
            logger.info(f"Deleted note: {title}")
            return True
        else:
            logger.warning(f"Note not found for deletion: {title}")
            return False
    except SQLAlchemyError as e:
        logger.error(f"Database error in delete_note({title}): {e}", exc_info=True)
        raise


async def _enqueue_note_indexing_task(db_context: DatabaseContext, title: str) -> None:
    """
    Helper function to enqueue an indexing task for a note.

    Args:
        db_context: Database context with task enqueueing capability
        title: Title of the note to index
    """
    try:
        # Fetch the note to get its ID
        note_stmt = select(notes_table.c.id).where(notes_table.c.title == title)
        note_row = await db_context.fetch_one(note_stmt)
        if note_row:
            from family_assistant import storage as storage_module

            await storage_module.enqueue_task(
                db_context=db_context,
                task_id=f"index_note_{note_row['id']}",
                task_type="index_note",
                payload={"note_id": note_row["id"]},
                notify_event=None,
            )
            logger.info(
                f"Enqueued indexing task for note ID {note_row['id']} (title: {title})"
            )
    except Exception as e:
        logger.error(f"Failed to enqueue indexing task for note '{title}': {e}")
        # Don't fail the note operation if indexing task enqueueing fails
