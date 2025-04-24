import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import asyncio
import random
from sqlalchemy.exc import DBAPIError
from dateutil import rrule  # Added for recurrence calculation
from dateutil.parser import isoparse  # Added for parsing dates in recurrence

# Import base components
from db_base import metadata, get_engine, engine # Import engine directly too

# Import specific storage modules
from notes_storage import notes_table, add_or_update_note, get_all_notes, get_note_by_title, delete_note
from email_storage import received_emails_table, store_incoming_email
from message_history_storage import message_history_table, add_message_to_history, get_recent_history, get_message_by_id, get_grouped_message_history
from task_storage import tasks_table, enqueue_task, dequeue_task, update_task_status, reschedule_task_for_retry, get_all_tasks

logger = logging.getLogger(__name__)

# --- Vector Storage Imports ---
try:
    from vector_storage import (
        Base as VectorBase,
        init_vector_db,
        add_document,
        get_document_by_source_id,
        add_embedding,
        delete_document,
        query_vectors,
    )  # Explicit imports

    VECTOR_STORAGE_ENABLED = True
    logger.info("Vector storage module imported successfully.")
except ImportError:
    # Define placeholder types if vector_storage is not available
    class Base: pass # type: ignore
    VectorBase = Base # type: ignore # Define here even if module fails to load
    logger.warning("vector_storage.py not found. Vector storage features disabled.")
    VECTOR_STORAGE_ENABLED = False
    # Define placeholders for the functions if the import failed
    def init_vector_db(): pass # type: ignore # noqa: E305
    vector_storage = None # Placeholder, though functions above handle the no-op
    # No need to re-log warning here, already logged in except block

# --- Global Init Function ---
logger = logging.getLogger(__name__)


# Add vector storage models to the same metadata object if enabled
if VECTOR_STORAGE_ENABLED:

__all__ = [
    "init_db",
    "get_all_notes",
    "get_engine",  # Export the engine creation function/getter
    "get_engine",  # Export the engine creation function/getter
    "add_message_to_history",
    "get_recent_history",
    "get_note_by_title",
    "get_message_by_id",
    "add_or_update_note",
    "delete_note",
    "enqueue_task",
    "dequeue_task",
    "update_task_status",
    "reschedule_task_for_retry", # Added task functions
    "get_all_tasks",  # Added
    "get_grouped_message_history",
    "notes_table",  # Also export tables if needed elsewhere (e.g., tests)
    "message_history",
    "tasks_table", # Export task table
    "received_emails_table", # Export new email table
    "store_incoming_email", # Export email storage function
    "engine",
    "metadata",
    # Vector Storage Exports (conditional)
]

if VECTOR_STORAGE_ENABLED and vector_storage:
if VECTOR_STORAGE_ENABLED and 'vector_storage' in locals() and vector_storage: # Check locals() and existence
    __all__.extend(
        [
            "add_document",
            "VectorBase", # Export the Base class for models
            "init_vector_db",
            "get_document_by_source_id",
            "add_embedding",
            "delete_document",
            "query_vectors",
        ]
    )
DATABASE_URL = os.getenv(
    "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
)  # Default to SQLite async

engine = create_async_engine(
    DATABASE_URL, echo=False
)  # Set echo=True for debugging SQL
metadata = MetaData()


def get_engine():
    """Returns the initialized SQLAlchemy async engine."""
    return engine


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
    Column(
        "tool_calls_info", JSON, nullable=True
    ),  # Added: To store details of tool calls
)

# Define the tasks table for the message queue
tasks_table = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),  # Internal primary key
    Column(
        "task_id", String, nullable=False, unique=True, index=True
    ),  # Caller-provided unique ID
    Column("task_type", String, nullable=False, index=True),  # For routing to handlers
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
    Column("error", Text, nullable=True),  # Store last error message on failure/retry
    Column(
        "retry_count", Integer, default=0, nullable=False
    ),  # Number of times this task has been retried
    Column(
        "max_retries", Integer, default=3, nullable=False
    ),  # Maximum number of retries allowed
    Column(
        "recurrence_rule", String, nullable=True
    ),  # Stores RRULE string, e.g., "FREQ=DAILY;INTERVAL=1"
    Column(
        "original_task_id", String, nullable=True, index=True
    ),  # Links recurring instances to the first one
)

# Add vector storage models to the same metadata object if enabled
if VECTOR_STORAGE_ENABLED:
    VectorBase.metadata = metadata  # Use the imported Base from vector_storage


async def init_db():
    """Initializes the database, with retries."""
    max_retries = 5  # Allow more retries for initial connection
    base_delay = 1.0  # seconds
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.begin() as conn:
                logger.info(f"Initializing database schema (attempt {attempt+1})...")
                await conn.run_sync(metadata.create_all)
                logger.info("Database schema initialized.")
                # Also initialize vector DB parts if enabled
                if VECTOR_STORAGE_ENABLED:
                    try:
                        await init_vector_db()  # Call the imported function directly
                    except Exception as vec_e:
                        logger.error(
                            f"Failed to initialize vector database: {vec_e}",
                            exc_info=True,
                        )
                        raise  # Propagate error if vector init fails
                return  # Success
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError during init_db (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for init_db. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay * 0.5)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in init_db: {e}", exc_info=True)
            raise  # Re-raise immediately

    logger.critical("Database initialization failed after all retries.")
    raise RuntimeError("Database initialization failed after multiple retries")

async def get_all_notes() -> List[Dict[str, str]]:
    """Retrieves all notes, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                stmt = select(notes_table.c.title, notes_table.c.content).order_by(
                    notes_table.c.title
                )
                result = await conn.execute(stmt)
                rows = result.fetchall()
                # Return as a list of dicts for easier iteration
                return [
                    {"title": row.title, "content": row.content} for row in rows
                ]  # Success
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_all_notes (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(f"Max retries exceeded for get_all_notes. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_all_notes: {e}", exc_info=True)
            raise  # Re-raise immediately

    logger.error("get_all_notes failed after all retries.")
    raise RuntimeError(
        "Database operation failed for get_all_notes after multiple retries"
    )


async def add_message_to_history(
    chat_id: int,
    message_id: int,
    timestamp: datetime,
    role: str,
    content: str,
    tool_calls_info: Optional[
        List[Dict[str, Any]]
    ] = None,  # Added tool_calls_info parameter
):
    """Adds a message to the history table, including optional tool call info, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = insert(message_history).values(
        chat_id=chat_id,
        message_id=message_id,
        timestamp=timestamp,
        role=role,
        content=content,
        tool_calls_info=tool_calls_info,  # Store the tool call info
    )
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                await conn.execute(stmt)
                await conn.commit()
                logger.debug(
                    f"Added message {message_id} from chat {chat_id} to history."
                )
                return  # Success
        except DBAPIError as e:
            # Catch potential unique constraint errors if retrying same message?
            # Check e.orig or specific driver error codes if needed.
            # For now, retry generic DBAPIError
            logger.warning(
                f"DBAPIError in add_message_to_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for add_message_to_history({chat_id}, {message_id}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            # Catch non-retryable errors (like constraint violations if not DBAPIError)
            logger.error(
                f"Non-retryable error in add_message_to_history({chat_id}, {message_id}): {e}",
                exc_info=True,
            )
            raise

    logger.error(
        f"add_message_to_history({chat_id}, {message_id}) failed after all retries."
    )
    raise RuntimeError(
        f"Database operation failed for add_message_to_history({chat_id}, {message_id}) after multiple retries"
    )


async def get_recent_history(
    chat_id: int, limit: int, max_age: timedelta
) -> List[Dict[str, Any]]:
    """
    Retrieves recent messages for a chat, including tool call info for assistant messages,
    ordered chronologically, with retries.
    """
    cutoff_time = datetime.now(timezone.utc) - max_age
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = (  # Define stmt outside loop
        select(
            message_history.c.role,
            message_history.c.content,
            message_history.c.tool_calls_info,  # Select the tool calls info
        )
        .where(message_history.c.chat_id == chat_id)
        .where(message_history.c.timestamp >= cutoff_time)
        .order_by(message_history.c.timestamp.desc())  # Get latest first
        .limit(limit)
    )
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                # Reverse to get chronological order for the LLM
                formatted_rows = []
                for row in reversed(rows):
                    msg = {"role": row.role, "content": row.content}
                    # Include tool_calls_info if it exists (it's stored as JSON)
                    if row.role == "assistant" and row.tool_calls_info:
                        # Directly attach the stored info. Assumes it's stored
                        # in a format compatible with LLM expectations or needs
                        # further transformation in main.py.
                        # LiteLLM expects a 'tool_calls' key with a list of tool call objects.
                        # We need to ensure the structure matches.
                        # The stored info might be simpler (e.g., just name, args, response).
                        # Let's pass it raw for now and adapt in main.py
                        msg["tool_calls_info_raw"] = row.tool_calls_info
                    formatted_rows.append(msg)
                return formatted_rows  # Success!
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_recent_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for get_recent_history({chat_id}). Raising error."
                )
                raise  # Re-raise the last exception if all retries fail
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            # Catch any other unexpected errors and don't retry
            logger.error(
                f"Non-retryable error in get_recent_history({chat_id}): {e}",
                exc_info=True,
            )
            raise  # Re-raise immediately

    logger.error(f"get_recent_history({chat_id}) failed after all retries.")
    raise RuntimeError(
        f"Database operation failed for get_recent_history({chat_id}) after multiple retries"
    )


async def get_note_by_title(title: str) -> Optional[Dict[str, Any]]:
    """Retrieves a specific note by its title, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = select(
        notes_table.c.title, notes_table.c.content
    ).where(  # Define stmt outside loop
        notes_table.c.title == title
    )
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                if row:
                    # Use ._mapping to access columns by name easily
                    return row._mapping
                return None  # Success (note not found or query succeeded)
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_note_by_title (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for get_note_by_title({title}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in get_note_by_title({title}): {e}", exc_info=True
            )
            raise  # Re-raise immediately

    logger.error(f"get_note_by_title({title}) failed after all retries.")
    raise RuntimeError(
        f"Database operation failed for get_note_by_title({title}) after multiple retries"
    )


async def get_message_by_id(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves a specific message by its chat and message ID, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = (  # Define stmt outside loop
        select(message_history.c.role, message_history.c.content)
        .where(message_history.c.chat_id == chat_id)
        .where(message_history.c.message_id == message_id)
    )
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                row = result.fetchone()
                if row:
                    return {"role": row.role, "content": row.content}
                return None  # Success (message not found or query succeeded)
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_message_by_id (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for get_message_by_id({chat_id}, {message_id}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in get_message_by_id({chat_id}, {message_id}): {e}",
                exc_info=True,
            )
            raise  # Re-raise immediately

    logger.error(
        f"get_message_by_id({chat_id}, {message_id}) failed after all retries."
    )
    raise RuntimeError(
        f"Database operation failed for get_message_by_id({chat_id}, {message_id}) after multiple retries"
    )


# Optional: Function to prune very old history if needed
# async def prune_history(max_age: timedelta): ...


async def add_or_update_note(title: str, content: str):
    """Adds/updates a note, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:  # Start of with block
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
                return "Success"
            # End of with block for conn
        except DBAPIError as e:
            # Note: Retrying might lead to race conditions if two updates happen concurrently.
            # The unique constraint on title helps, but consider implications.
            logger.warning(
                f"DBAPIError in add_or_update_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for add_or_update_note({title}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in add_or_update_note({title}): {e}",
                exc_info=True,
            )
            raise

    logger.error(f"add_or_update_note({title}) failed after all retries.")
    raise RuntimeError(
        f"Database operation failed for add_or_update_note({title}) after multiple retries"
    )


async def delete_note(title: str) -> bool:
    """Deletes a note by title, with retries."""
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = notes_table.delete().where(
        notes_table.c.title == title
    )  # Define stmt outside loop
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(f"Deleted note: {title}")
                    return True
                logger.warning(f"Note not found for deletion: {title}")
                return False  # Success (operation completed, note wasn't there)
            # End connection block
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in delete_note (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for delete_note({title}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in delete_note({title}): {e}", exc_info=True
            )
            raise

    logger.error(f"delete_note({title}) failed after all retries.")
    raise RuntimeError(
        f"Database operation failed for delete_note({title}) after multiple retries"
    )


async def get_grouped_message_history() -> Dict[int, List[Dict[str, Any]]]:
    """
    Retrieves all message history, grouped by chat_id and ordered by timestamp.
    """
    max_retries = 3
    base_delay = 0.5  # seconds

    stmt = (
        select(
            message_history.c.chat_id,
            message_history.c.message_id,
            message_history.c.timestamp,
            message_history.c.role,
            message_history.c.content,
            message_history.c.tool_calls_info,  # Added: Fetch tool_calls_info
        )
        # Order by chat_id first, then by timestamp DESC within each chat
        .order_by(message_history.c.chat_id, message_history.c.timestamp.desc())
    )
    engine = get_engine() # Get engine from db_base
    for attempt in range(max_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()

                # Group messages by chat_id
                grouped_history = {}
                for row in rows:
                    chat_id = row.chat_id
                    if chat_id not in grouped_history:
                        grouped_history[chat_id] = []
                    grouped_history[chat_id].append(
                        {
                            "chat_id": row.chat_id,
                            "message_id": row.message_id,
                            "timestamp": row.timestamp,
                            "role": row.role,
                            "content": row.content,
                            "tool_calls_info": row.tool_calls_info,  # Added: Include tool_calls_info in result
                        }
                    )
                return grouped_history  # Success
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_grouped_message_history (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for get_grouped_message_history. Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in get_grouped_message_history: {e}",
                exc_info=True,
            )
            raise

    logger.error("get_grouped_message_history failed after all retries.")
    raise RuntimeError(
        "Database operation failed for get_grouped_message_history after multiple retries"
    )
