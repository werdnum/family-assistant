import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import asyncio
import os # Added for path manipulation
import random
from sqlalchemy.exc import DBAPIError
from dateutil import rrule
from dateutil.parser import isoparse
from alembic.config import Config as AlembicConfig  # Renamed import to avoid conflict
from alembic import command as alembic_command

# Import base components using absolute package paths
from family_assistant.storage.base import metadata, get_engine, engine
from family_assistant.storage.context import DatabaseContext, get_db_context

# Import specific storage modules using absolute package paths
from family_assistant.storage.notes import (
    notes_table,
    add_or_update_note,
    get_all_notes,
    get_note_by_title,
    delete_note,
)
from family_assistant.storage.email import received_emails_table, store_incoming_email
from family_assistant.storage.message_history import (
    message_history_table,
    add_message_to_history,
    get_recent_history,
    get_grouped_message_history,
    # Renamed from get_message_by_id
    get_message_by_interface_id,
    get_messages_by_turn_id,  # Added
    get_messages_by_thread_id,  # Added
    update_message_interface_id,  # Added
)
from family_assistant.storage.tasks import (
    tasks_table,
    enqueue_task,
    dequeue_task,
    update_task_status,
    reschedule_task_for_retry,
    get_all_tasks,
)

logger = logging.getLogger(__name__)

# --- Vector Storage Imports ---
try:
    # Use absolute package path
    from family_assistant.storage.vector import (
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
    class Base:
        pass  # type: ignore

    VectorBase = Base  # type: ignore # Define here even if module fails to load
    logger.warning("storage/vector.py not found. Vector storage features disabled.")
    VECTOR_STORAGE_ENABLED = False

    # Define placeholders for the functions if the import failed
    def init_vector_db():
        pass  # type: ignore # noqa: E305

    vector_storage = None  # Placeholder, though functions above handle the no-op
    # Add vector storage models to the same metadata object if enabled
    if "VectorBase" in locals() and hasattr(VectorBase, "metadata"):
        VectorBase.metadata = metadata
    else:
        # This case should ideally not happen if VECTOR_STORAGE_ENABLED is true, but defensively log.
        logger.warning(
            "VECTOR_STORAGE_ENABLED is True, but VectorBase not found or has no metadata attribute. Vector models might not be created."
        )

# --- Global Init Function ---
# logger definition moved here to be after potential vector_storage import logs
logger = logging.getLogger(__name__)


async def init_db():
    """Initializes the database by creating all tables defined in the metadata."""
    max_retries = 5
    base_delay = 1.0
    engine = get_engine()  # Get engine from db_base
    for attempt in range(max_retries):
        try:
            logger.info(f"Checking database state (attempt {attempt+1})...")

            # --- Alembic Configuration ---
            # Assume alembic.ini is in the project root, 3 levels up from this file's directory
            alembic_ini_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "alembic.ini"))
            alembic_cfg = AlembicConfig(alembic_ini_path)
            # Set the sqlalchemy.url for Alembic using the engine's URL
            alembic_cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))

            # Try upgrading first using asyncio.to_thread.
            # Alembic manages its own transaction/connection via the config.
            try:
                logger.info("Attempting Alembic upgrade to 'head'...")
                # Use asyncio.to_thread instead of conn.run_sync
                await asyncio.to_thread(alembic_command.upgrade, alembic_cfg, "head")
                logger.info("Database is up-to-date or migrated via Alembic.")

            # Catch Exception for broader compatibility, specific DBAPI errors vary.
            # The most common failure on a new DB is the alembic_version table missing.
            except Exception as upgrade_err:
                # Log the specific error for debugging, but treat it as needing init
                logger.warning(f"Alembic upgrade failed (likely new DB, attempting initial setup): {upgrade_err}")

                # Use engine.begin() specifically for metadata.create_all
                logger.info("Creating tables from SQLAlchemy metadata...")
                async with engine.begin() as conn:
                    # Create all tables defined in SQLAlchemy metadata
                    await conn.run_sync(metadata.create_all)
                logger.info("Tables created.")

                # Stamp the database using asyncio.to_thread after table creation
                try:
                    logger.info("Stamping database with Alembic 'head'...")
                    # Use asyncio.to_thread instead of conn.run_sync
                    await asyncio.to_thread(alembic_command.stamp, alembic_cfg, "head")
                    logger.info("Database schema stamped.")
                except Exception as stamp_err:
                    logger.error(f"Failed to stamp database after creation: {stamp_err}", exc_info=True)
                    raise  # Re-raise stamping error, as it indicates a problem

                # Initialize vector DB parts if enabled (only on initial creation)
                if VECTOR_STORAGE_ENABLED:
                    logger.info("Initializing vector DB components...")
                    try:
                        # Use a separate context for vector init, as the main conn might be closed
                        async with DatabaseContext(engine=engine) as vector_init_context:
                            await init_vector_db(db_context=vector_init_context)
                        logger.info("Vector DB components initialized.")
                    except Exception as vec_e:
                        logger.error(f"Failed to initialize vector database components after initial creation: {vec_e}", exc_info=True)
                        # Decide if this failure should prevent startup or just be logged
                        # For now, let's re-raise to prevent startup if vector init fails
                        raise

            # If upgrade or creation/stamp/vector init was successful
            logger.info("Database initialization successful.")
            return  # Exit the function successfully

        # --- Retry Logic ---
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError during init_db (attempt {attempt + 1}/{max_retries}): {e}. Retrying..."
            )
            if attempt == max_retries - 1:
                logger.error("Max retries exceeded for init_db due to DBAPIError.")
                # Let it fall through to the final critical log and raise
            else:
                delay = base_delay * (2**attempt) + random.uniform(0, base_delay * 0.5)
                await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Unexpected non-retryable error during init_db attempt {attempt + 1}: {e}", exc_info=True)
            raise # Re-raise unexpected errors immediately

    # This part is reached only if the loop completes without returning (all retries failed)
    logger.critical("Database initialization failed after all retries.")
    raise RuntimeError("Database initialization failed after multiple retries")


# --- Exports ---
# Re-export functions and tables from specific modules to maintain the facade
# Define __all__ AFTER all functions/variables it references are defined.
__all__ = [
    "init_db",  # Now defined above
    "get_all_notes",
    "get_engine",
    "add_message_to_history",
    "get_recent_history",
    "get_grouped_message_history",
    "get_message_by_interface_id",  # Renamed
    "get_messages_by_turn_id",  # Added
    "get_messages_by_thread_id",  # Added
    "add_or_update_note",
    "delete_note",
    "enqueue_task",
    "dequeue_task",
    "update_task_status",
    "reschedule_task_for_retry",  # Removed duplicate
    "get_all_tasks",
    "update_message_interface_id",  # Added
    "notes_table",
    "message_history_table",
    "tasks_table",
    "received_emails_table",
    "store_incoming_email",
    "engine",
    "metadata",
    "DatabaseContext",  # Export the new context manager
    "get_db_context",  # Export convenience function
    # Vector Storage Exports are added conditionally below
]

# Extend __all__ conditionally for vector storage if it was enabled.
if VECTOR_STORAGE_ENABLED:
    # Check if vector_storage specific names were imported successfully
    # This assumes the placeholder functions/classes were NOT defined if import failed
    if "add_document" in locals():
        __all__.extend(
            [
                "add_document",
                "VectorBase",
                "init_vector_db",
                "get_document_by_source_id",
                "add_embedding",
                "delete_document",
                "query_vectors",
                "VectorDocumentProtocol",
            ]
        )
