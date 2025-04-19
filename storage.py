import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    select,
    insert,
    update,
    delete,  # Add delete
    BigInteger,
    Integer,
    DateTime,
    Text,
    JSON,  # Added JSON for payload
    desc,
)
import asyncio  # Add asyncio import
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
)  # Default to SQLite async

engine = create_async_engine(
    DATABASE_URL, echo=False
)  # Set echo=True for debugging SQL
metadata = MetaData()

# Define the notes table (replaces key_value_store)
notes_table = Table(
    "notes",
    metadata,
    Column(
        "id", Integer, primary_key=True, autoincrement=True
    ),  # Use Integer for SQLite autoincrement
    Column(
        "title", String, nullable=False, unique=True, index=True
    ),  # Unique title (like the old key)
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

# Define the message history table
message_history = Table(
    "message_history",
    metadata,
    Column("chat_id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, primary_key=True),
    Column("timestamp", DateTime(timezone=True), nullable=False, index=True),
    Column("role", String, nullable=False),  # 'user' or 'assistant'
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
        stmt = select(notes_table.c.title, notes_table.c.content).order_by(
            notes_table.c.title
        )
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
            .order_by(message_history.c.timestamp.desc())  # Get latest first
            .limit(limit)
        )
        result = await conn.execute(stmt)
        rows = result.fetchall()
        # Reverse to get chronological order for the LLM
        return [{"role": row.role, "content": row.content} for row in reversed(rows)]


async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title."""
    async with engine.connect() as conn:
        stmt = select(notes_table.c.title, notes_table.c.content).where(
            notes_table.c.title == title
        )
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

# --- Task Queue Functions ---


async def enqueue_task(
    task_id: str,
    task_type: str,
    payload: Optional[Any] = None,
    scheduled_at: Optional[datetime] = None,
    notify_event: Optional[asyncio.Event] = None, # Add optional event for notification
):
    """
    Adds a new task to the queue.

    If notify_event is provided and the task is scheduled for immediate execution
    (scheduled_at is None or in the past), the event will be set.
    """
    async with engine.connect() as conn:
        # Ensure scheduled_at is timezone-aware if provided
        if scheduled_at and scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")

        stmt = insert(tasks_table).values(
            task_id=task_id,
            task_type=task_type,
            payload=payload,
            scheduled_at=scheduled_at,
            # created_at is set by default in the table definition now
            status="pending",
        )
        try:
            await conn.execute(stmt)
            await conn.commit()
            logger.info(f"Enqueued task {task_id} of type {task_type}.")

            # Notify worker if task is immediate and event is provided
            is_immediate = scheduled_at is None or scheduled_at <= datetime.now(
                timezone.utc
            )
            if is_immediate and notify_event:
                notify_event.set()
                logger.debug(f"Notified worker about immediate task {task_id}.")

        except Exception as e:
            await conn.rollback()
            # Consider specific exception types (e.g., unique constraint violation)
            logger.error(f"Failed to enqueue task {task_id}: {e}", exc_info=True)
            raise  # Re-raise the exception


async def dequeue_task(
    worker_id: str, task_types: List[str]
) -> Optional[Dict[str, Any]]:
    """
    Atomically dequeues the next available task matching the types and locks it.

    Uses SELECT FOR UPDATE SKIP LOCKED, best suited for PostgreSQL.
    May behave differently or less concurrently on SQLite.

    Args:
        worker_id: An identifier for the worker attempting to dequeue.
        task_types: A list of task types the worker can handle.

    Returns:
        A dictionary representing the task row, or None if no suitable task is found.
    """
    now = datetime.now(timezone.utc)
    async with engine.connect() as conn:
        async with conn.begin():  # Start transaction
            # Select the oldest, pending task of the specified types,
            # whose scheduled time is in the past (or null).
            # Use FOR UPDATE SKIP LOCKED to handle concurrency.
            # Note: This locking clause is PostgreSQL-specific via SQLAlchemy.
            # SQLite will likely lock the entire table during the transaction.
            stmt = (
                select(tasks_table)
                .where(tasks_table.c.status == "pending")
                .where(tasks_table.c.task_type.in_(task_types))
                .where(
                    (tasks_table.c.scheduled_at == None)  # noqa: E711
                    | (tasks_table.c.scheduled_at <= now)
                )
                .order_by(tasks_table.c.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )

            result = await conn.execute(stmt)
            task_row = result.fetchone()

            if task_row:
                # Lock acquired, update the status and lock info
                update_stmt = (
                    update(tasks_table)
                    .where(tasks_table.c.id == task_row.id)
                    .where(
                        tasks_table.c.status == "pending"
                    ) # Ensure it wasn't somehow processed between SELECT and UPDATE
                    .values(
                        status="processing",
                        locked_by=worker_id,
                        locked_at=now,
                    )
                )
                update_result = await conn.execute(update_stmt)

                if update_result.rowcount == 1:
                    logger.info(
                        f"Worker {worker_id} dequeued task {task_row.task_id}"
                    )
                    return task_row._mapping # Return as dict
                else:
                    # Extremely rare race condition or issue, row was modified
                    # between SELECT FOR UPDATE and UPDATE. Transaction rollback
                    # will handle cleanup.
                    logger.warning(
                        f"Worker {worker_id} failed to lock task {task_row.task_id} after selection."
                    )
                    return None # Transaction will rollback changes implicitly

            # No suitable task found
            return None


async def update_task_status(
    task_id: str, status: str, error: Optional[str] = None
) -> bool:
    """Updates the status of a task (e.g., 'done', 'failed')."""
    async with engine.connect() as conn:
        values_to_update = {"status": status}
        if status == "failed":
            values_to_update["error"] = error
        # Clear lock info when finishing or failing
        values_to_update["locked_by"] = None
        values_to_update["locked_at"] = None

        stmt = (
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            # Optionally ensure it was being processed before marking done/failed
            # .where(tasks_table.c.status == 'processing')
            .values(**values_to_update)
        )
        result = await conn.execute(stmt)
        await conn.commit()
        if result.rowcount > 0:
            logger.info(f"Updated task {task_id} status to {status}.")
            return True
        logger.warning(
            f"Task {task_id} not found or status unchanged when updating to {status}."
        )
        return False


# --- End Task Queue Functions ---


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
                updated_at=now,
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
