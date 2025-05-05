import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import asyncio
import os # Added for path manipulation
import random
from sqlalchemy.exc import DBAPIError
from sqlalchemy import inspect, Table, Column, String, MetaData as SqlaMetaData, insert # Import inspect and table creation components
from dateutil import rrule
from dateutil.parser import isoparse
# from alembic.script import ScriptDirectory # No longer needed for manual stamping
# from alembic import command as alembic_command # Already imported
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
            # Get the config file path from env var, or default to project root relative path
            alembic_ini_env_var = os.getenv("ALEMBIC_CONFIG")
            if alembic_ini_env_var and os.path.exists(alembic_ini_env_var):
                alembic_ini_path = alembic_ini_env_var
                logger.info(f"Using Alembic config from environment variable: {alembic_ini_path}")
            else:
                # Fallback: Assume alembic.ini is in the project root (3 levels up)
                project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
                alembic_ini_path = os.path.join(project_root, "alembic.ini")
                logger.warning(f"ALEMBIC_CONFIG env var not set or invalid. Falling back to default: {alembic_ini_path}")

            alembic_cfg = AlembicConfig(alembic_ini_path)
            # Set the sqlalchemy.url for Alembic using the engine's URL
            alembic_cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))

            # Explicitly resolve and set the script_location based on the INI file's directory
            # This is necessary because Alembic might not correctly resolve the relative path
            # defined in the INI when the config is loaded programmatically.
            ini_directory = os.path.dirname(alembic_ini_path)
            # Get the relative script location from the config (defaults to 'alembic' if somehow missing)
            relative_script_location = alembic_cfg.get_main_option("script_location", "alembic")
            absolute_script_location = os.path.abspath(os.path.join(ini_directory, relative_script_location))
            logger.info(f"Setting absolute script_location for Alembic: {absolute_script_location}")
            alembic_cfg.set_main_option("script_location", absolute_script_location)

            # --- Check for alembic_version table using inspect ---
            # Use run_sync on the async engine's connection to perform the check
            logger.info("Inspecting database for alembic_version table...")
            async with engine.connect() as conn:
                def inspector_sync(sync_conn) -> tuple[bool, list[str]]:
                    """Checks for alembic_version table and returns found tables."""
                    inspector = inspect(sync_conn)
                    found_tables = inspector.get_table_names()
                    has_table = "alembic_version" in found_tables
                    return has_table, found_tables
                has_version_table, found_tables_list = await conn.run_sync(inspector_sync)

            logger.info(f"Alembic version table found: {has_version_table}")
            if has_version_table:
                # Database is already managed by Alembic, upgrade it.
                logger.info("Alembic version table found. Upgrading database to 'head'...")
                # Use run_sync for the upgrade command
                async with engine.connect() as conn:
                    def sync_upgrade_command(sync_conn, cfg, revision):
                        """Wrapper to run alembic upgrade with existing connection."""
                        # Make the connection available to env.py
                        cfg.attributes["connection"] = sync_conn
                        alembic_command.upgrade(cfg, revision)
                    await conn.run_sync(sync_upgrade_command, alembic_cfg, "head")
                    logger.info("Alembic upgrade command completed via run_sync.")

            else:
                # Database is new or not managed by Alembic, create tables and stamp.
                logger.info(f"Tables found by inspector: {found_tables_list}")
                logger.info("Alembic version table not found. Performing initial schema creation and stamping...")

                # Create all tables defined in SQLAlchemy metadata
                logger.info("Creating tables from SQLAlchemy metadata...")
                async with engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                logger.info("Tables created.")

                # Use run_sync for ensure_version and stamp commands
                try:
                    async with engine.connect() as conn:
                        # Explicitly ensure the alembic_version table exists
                        logger.info("Attempting to run ensure_version via run_sync...")
                        def sync_ensure_version_command(sync_conn, cfg):
                            """Wrapper to run alembic ensure_version with existing connection."""
                            # Make the connection available to env.py
                            cfg.attributes["connection"] = sync_conn
                            alembic_command.ensure_version(cfg)
                        await conn.run_sync(sync_ensure_version_command, alembic_cfg)
                        logger.info("ensure_version command completed.")

                        # Stamp the database with the latest revision
                        logger.info("Attempting to run stamp via run_sync...")
                        def sync_stamp_command(sync_conn, cfg, revision):
                            """Wrapper to run alembic stamp with existing connection."""
                            # Make the connection available to env.py
                            cfg.attributes["connection"] = sync_conn
                            alembic_command.stamp(cfg, revision)
                        await conn.run_sync(sync_stamp_command, alembic_cfg, "head")
                        logger.info("stamp command completed.")
                        logger.info("Database schema stamped.")
                except Exception as stamp_err:
                     # Covers errors from both ensure_version and stamp
                     logger.error(f"Failed during ensure_version or stamp: {stamp_err}", exc_info=True)
                     raise # Re-raise stamping error

                # Initialize vector DB parts if enabled (only on initial creation)
                if VECTOR_STORAGE_ENABLED:
                    logger.info("Initializing vector DB components...")
                    try:
                        # Use a separate context for vector init
                        async with DatabaseContext(engine=engine) as vector_init_context:
                            await init_vector_db(db_context=vector_init_context)
                        logger.info("Vector DB components initialized.")
                    except Exception as vec_e:
                        logger.error(f"Failed to initialize vector database components after initial creation: {vec_e}", exc_info=True)
                        raise # Re-raise vector init error

            # If upgrade or creation/stamp sequence was successful
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
