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
import asyncio
import random # Added for jitter
from sqlalchemy.exc import DBAPIError # Added for retry logic
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)


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
    max_retries = 3
    base_delay = 0.5 # seconds

    # Ensure scheduled_at is timezone-aware if provided (outside the loop)
    if scheduled_at and scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn: # Start of with block
                # Code below needs to be indented inside the 'with' block
                stmt = insert(tasks_table).values(
                    task_id=task_id,
                    task_type=task_type,
            payload=payload,
            scheduled_at=scheduled_at,
            # created_at is set by default in the table definition now
                    status="pending",
                )
                # This inner try/except is specific to the insert/commit part
                # and should also be inside the 'with' block
                try:
                    await conn.execute(stmt)
                    await conn.commit()
                    logger.info(f"Enqueued task {task_id} of type {task_type}.")

                    # Notify worker if task is immediate and event is provided
                    # This block MUST be inside the try block, indented correctly.
                    is_immediate = scheduled_at is None or scheduled_at <= datetime.now(
                        timezone.utc
                    )
                    if is_immediate and notify_event:
                        notify_event.set()
                        logger.debug(f"Notified worker about immediate task {task_id}.")

                    return # Success
                except Exception as inner_e: # Catch errors during execute/commit
                    # Rollback might be needed if commit failed, but connection might be bad.
                    # Let the outer exception handler manage retries.
                    logger.error(f"Error during execute/commit in enqueue_task: {inner_e}", exc_info=True)
                    # Re-raise to be caught by the outer DBAPIError or generic Exception handler
                    raise inner_e
            # End of with block for conn

        except DBAPIError as e:
            logger.warning(f"DBAPIError in enqueue_task (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for enqueue_task({task_id}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except ValueError: # Catch specific non-retryable errors like timezone issue
             raise # Re-raise immediately
        except Exception as e:
            # Consider specific exception types (e.g., unique constraint violation)
            # These might indicate non-retryable issues.
            logger.error(f"Non-retryable error in enqueue_task {task_id}: {e}", exc_info=True)
            # Should we rollback here? The connection might be dead. Rollback happens on context exit error anyway.
            raise # Re-raise other exceptions immediately

    logger.error(f"enqueue_task({task_id}) failed after all retries.")
    # Throwing an exception is better than returning None/False implicitly
    raise RuntimeError(f"Database operation failed for enqueue_task({task_id}) after multiple retries")


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
    max_retries = 3
    base_delay = 0.5 # seconds

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                async with conn.begin(): # Start transaction
                    # Select the oldest, pending task of the specified types,
                    # whose scheduled time is in the past (or null).
                    # Use FOR UPDATE SKIP LOCKED to handle concurrency.
                    # Note: This locking clause is PostgreSQL-specific via SQLAlchemy.
                    # SQLite will likely lock the entire table during the transaction.
                    # This block needs to be inside the transaction
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

            # If not task_row (no suitable task found by the initial SELECT)
            return None
        # End of transaction block (async with conn.begin())
    # End of connection block (async with engine.connect())
except DBAPIError as e:
            logger.warning(f"DBAPIError in dequeue_task (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for dequeue_task. Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in dequeue_task: {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error("dequeue_task failed after all retries.")
    raise RuntimeError("Database operation failed for dequeue_task after multiple retries")


async def update_task_status(
    task_id: str, status: str, error: Optional[str] = None
) -> bool:
    """Updates the status of a task (e.g., 'done', 'failed'), with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    values_to_update = {"status": status}
    if status == "failed":
        values_to_update["error"] = error
    # Clear lock info when finishing or failing
    values_to_update["locked_by"] = None
    values_to_update["locked_at"] = None

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn: # Start of with block
                # Code below needs to be indented inside the 'with' block
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
            # End connection block
        except DBAPIError as e:
            logger.warning(f"DBAPIError in update_task_status (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for update_task_status({task_id}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in update_task_status({task_id}): {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error(f"update_task_status({task_id}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for update_task_status({task_id}) after multiple retries")


# --- End Task Queue Functions ---


__all__ = [
    "init_db",
    "get_all_notes",
    "add_message_to_history",
    "get_recent_history",
    "get_note_by_title",
    "get_message_by_id",
    "add_or_update_note",
    "delete_note",
    "enqueue_task", # Add task functions to __all__
    "dequeue_task",
    "update_task_status",
    "notes_table", # Also export tables if needed elsewhere (e.g., tests)
    "message_history",
    "tasks_table",
    "engine",
    "metadata",
]

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

# Define the tasks table for the message queue
tasks_table = Table(
    "tasks",
    metadata,
    Column(
        "id", Integer, primary_key=True, autoincrement=True
    ),  # Internal primary key
    Column(
        "task_id", String, nullable=False, unique=True, index=True
    ),  # Caller-provided unique ID
    Column(
        "task_type", String, nullable=False, index=True
    ),  # For routing to handlers
    Column("payload", JSON, nullable=True),  # Task-specific data
    Column(
        "scheduled_at", DateTime(timezone=True), nullable=True, index=True
    ),  # Optional future execution time
    Column(
        "created_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    ),
    Column(
        "status",
        String,
        default="pending",
        nullable=False,
        index=True,  # e.g., pending, processing, done, failed
    ),
    Column("locked_by", String, nullable=True),  # Worker ID holding the lock
    Column(
        "locked_at", DateTime(timezone=True), nullable=True
    ),  # Timestamp when the lock was acquired
    Column("error", Text, nullable=True),  # Store error message on failure
)


async def init_db():
    """Initializes the database, with retries."""
    max_retries = 5 # Allow more retries for initial connection
    base_delay = 1.0 # seconds

    for attempt in range(max_retries):
        try:
            async with engine.begin() as conn:
                logger.info(f"Initializing database schema (attempt {attempt+1})...")
                await conn.run_sync(metadata.create_all)
                logger.info("Database schema initialized.")
                return # Success
        except DBAPIError as e:
            logger.warning(f"DBAPIError during init_db (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for init_db. Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay * 0.5)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in init_db: {e}", exc_info=True)
            raise # Re-raise immediately

    logger.critical("Database initialization failed after all retries.")
    raise RuntimeError("Database initialization failed after multiple retries")


async def get_all_notes() -> List[Dict[str, str]]:
    """Retrieves all notes, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                stmt = select(notes_table.c.title, notes_table.c.content).order_by(
                    notes_table.c.title
                )
                result = await conn.execute(stmt)
                rows = result.fetchall()
                # Return as a list of dicts for easier iteration
                return [{"title": row.title, "content": row.content} for row in rows] # Success
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_all_notes (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_all_notes. Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_all_notes: {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error("get_all_notes failed after all retries.")
    raise RuntimeError("Database operation failed for get_all_notes after multiple retries")


async def add_message_to_history(
    chat_id: int, message_id: int, timestamp: datetime, role: str, content: str
):
    """Adds a message to the history table, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    stmt = insert(message_history).values( # Define stmt outside loop
        chat_id=chat_id,
        message_id=message_id,
        timestamp=timestamp,
        role=role,
        content=content,
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(stmt)
                await conn.commit()
                logger.debug(f"Added message {message_id} from chat {chat_id} to history.")
                return # Success
        except DBAPIError as e:
            # Catch potential unique constraint errors if retrying same message?
            # Check e.orig or specific driver error codes if needed.
            # For now, retry generic DBAPIError
            logger.warning(f"DBAPIError in add_message_to_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_message_to_history({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
             # Catch non-retryable errors (like constraint violations if not DBAPIError)
             logger.error(f"Non-retryable error in add_message_to_history({chat_id}, {message_id}): {e}", exc_info=True)
             raise

    logger.error(f"add_message_to_history({chat_id}, {message_id}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for add_message_to_history({chat_id}, {message_id}) after multiple retries")


async def get_recent_history(
    chat_id: int, limit: int, max_age: timedelta
) -> List[Dict[str, Any]]:
    """Retrieves recent messages for a chat, ordered chronologically, with retries."""
    cutoff_time = datetime.now(timezone.utc) - max_age
    max_retries = 3
    base_delay = 0.5 # seconds

    stmt = ( # Define stmt outside loop
        select(message_history.c.role, message_history.c.content)
        .where(message_history.c.chat_id == chat_id)
        .where(message_history.c.timestamp >= cutoff_time)
        .order_by(message_history.c.timestamp.desc())  # Get latest first
        .limit(limit)
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                # Reverse to get chronological order for the LLM
                return [{"role": row.role, "content": row.content} for row in reversed(rows)] # Success!
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_recent_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_recent_history({chat_id}). Raising error.")
                raise # Re-raise the last exception if all retries fail
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            # Catch any other unexpected errors and don't retry
            logger.error(f"Non-retryable error in get_recent_history({chat_id}): {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error(f"get_recent_history({chat_id}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for get_recent_history({chat_id}) after multiple retries")


async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    stmt = select(notes_table.c.title, notes_table.c.content).where( # Define stmt outside loop
        notes_table.c.title == title
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                if row:
                    # Use ._mapping to access columns by name easily
                    return row._mapping
                return None # Success (note not found or query succeeded)
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_note_by_title (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_note_by_title({title}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_note_by_title({title}): {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error(f"get_note_by_title({title}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for get_note_by_title({title}) after multiple retries")


async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    stmt = ( # Define stmt outside loop
        select(message_history.c.role, message_history.c.content)
        .where(message_history.c.chat_id == chat_id)
        .where(message_history.c.message_id == message_id)
    )

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                if row:
                    return {"role": row.role, "content": row.content}
                return None # Success (message not found or query succeeded)
        except DBAPIError as e:
            logger.warning(f"DBAPIError in get_message_by_id (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_message_by_id({chat_id}, {message_id}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_message_by_id({chat_id}, {message_id}): {e}", exc_info=True)
            raise # Re-raise immediately

    logger.error(f"get_message_by_id({chat_id}, {message_id}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for get_message_by_id({chat_id}, {message_id}) after multiple retries")


# Optional: Function to prune very old history if needed
# async def prune_history(max_age: timedelta): ...




async def add_or_update_note(title: str, content: str):
    """Adds/updates a note, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn: # Start of with block
                # Code below needs to be indented inside the 'with' block
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
                return # Success
            # End of with block for conn
        except DBAPIError as e:
            # Note: Retrying might lead to race conditions if two updates happen concurrently.
            # The unique constraint on title helps, but consider implications.
            logger.warning(f"DBAPIError in add_or_update_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for add_or_update_note({title}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
             logger.error(f"Non-retryable error in add_or_update_note({title}): {e}", exc_info=True)
             raise

    logger.error(f"add_or_update_note({title}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for add_or_update_note({title}) after multiple retries")


async def delete_note(title: str) -> bool:
    """Deletes a note by title, with retries."""
    max_retries = 3
    base_delay = 0.5 # seconds

    stmt = notes_table.delete().where(notes_table.c.title == title) # Define stmt outside loop

    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(f"Deleted note: {title}")
                    return True
                logger.warning(f"Note not found for deletion: {title}")
                return False # Success (operation completed, note wasn't there)
            # End connection block
        except DBAPIError as e:
            logger.warning(f"DBAPIError in delete_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for delete_note({title}). Raising error.")
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
             logger.error(f"Non-retryable error in delete_note({title}): {e}", exc_info=True)
             raise

    logger.error(f"delete_note({title}) failed after all retries.")
    raise RuntimeError(f"Database operation failed for delete_note({title}) after multiple retries")
