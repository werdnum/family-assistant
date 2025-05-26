import asyncio
import logging
import os  # Added for path manipulation
import random
import traceback
from collections.abc import Callable  # Added Any and Callable
from typing import Any

from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    inspect,
    text,
)  # Import inspect, text and table creation components
from sqlalchemy.engine import Connection  # Added Connection
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
    SQLAlchemyError,
)  # Keep OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command as alembic_command

# Import base components using absolute package paths
from family_assistant.storage.base import engine, get_engine, metadata
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.email import received_emails_table, store_incoming_email
from family_assistant.storage.message_history import (
    add_message_to_history,
    get_grouped_message_history,
    # Renamed from get_message_by_id
    get_message_by_interface_id,
    get_messages_by_thread_id,  # Added
    get_messages_by_turn_id,  # Added
    get_recent_history,
    message_history_table,
    update_message_error_traceback,  # Added
    update_message_interface_id,  # Added
)

# Import specific storage modules using absolute package paths
from family_assistant.storage.notes import (
    add_or_update_note,
    delete_note,
    get_all_notes,
    get_note_by_title,
    notes_table,
)
from family_assistant.storage.tasks import (
    dequeue_task,
    enqueue_task,
    get_all_tasks,
    reschedule_task_for_retry,
    tasks_table,
    update_task_status,
)

logger = logging.getLogger(__name__)

# --- Vector Storage Imports ---
try:
    # Use absolute package path
    from family_assistant.storage.vector import (
        Base as VectorBase,  # For ORM
    )
    from family_assistant.storage.vector import (
        Document,  # Protocol for document structure
        add_document,
        add_embedding,
        delete_document,
        get_document_by_id,
        get_document_by_source_id,
        init_vector_db,
        query_vectors,
    )

    VECTOR_STORAGE_ENABLED = True
    logger.info("Vector storage module imported successfully.")
except ImportError:
    # Define placeholder types if vector_storage is not available
    class Base:  # type: ignore
        pass

    VectorBase = Base  # type: ignore
    logger.warning("storage/vector.py not found. Vector storage features disabled.")
    VECTOR_STORAGE_ENABLED = False

    # Define placeholders for the functions if the import failed
    async def init_vector_db(*args: Any, **kwargs: Any) -> None:  # type: ignore
        pass

    async def add_document(*args: Any, **kwargs: Any) -> int:  # type: ignore
        # Return a dummy int; actual usage would expect an ID.
        # Or raise NotImplementedError if called when disabled.
        logger.warning(
            "add_document called but vector storage is disabled. Returning -1."
        )
        return -1

    async def get_document_by_source_id(*args: Any, **kwargs: Any) -> Any:  # type: ignore # Actual: DocumentRecord | None
        logger.warning(
            "get_document_by_source_id called but vector storage is disabled. Returning None."
        )
        return None

    async def get_document_by_id(*args: Any, **kwargs: Any) -> Any:  # type: ignore # Actual: DocumentRecord | None
        logger.warning(
            "get_document_by_id called but vector storage is disabled. Returning None."
        )
        return None

    async def add_embedding(*args: Any, **kwargs: Any) -> None:  # type: ignore
        pass

    async def delete_document(*args: Any, **kwargs: Any) -> bool:  # type: ignore
        logger.warning(
            "delete_document called but vector storage is disabled. Returning False."
        )
        return False

    async def query_vectors(*args: Any, **kwargs: Any) -> list[Any]:  # type: ignore
        return []  # Return an empty list for queries

    Document = Any  # type: ignore # Placeholder for the Document protocol if import fails


# logger definition moved here to be after potential vector_storage import logs
logger = logging.getLogger(__name__)
# --- Helper Functions for Database Initialization (Refactored) ---


async def _is_alembic_managed(engine: AsyncEngine) -> bool:
    """Checks if the database schema is managed by Alembic."""
    logger.info("Inspecting database for alembic_version table...")
    async with engine.connect() as conn:

        def inspector_sync(sync_conn: Connection) -> bool:
            """Checks for alembic_version table."""
            inspector = inspect(sync_conn)
            # Ensure inspector is valid before calling methods
            if inspector is None:
                logger.error("SQLAlchemy inspector could not be created.")
                raise RuntimeError("Failed to create SQLAlchemy inspector.")
            found_tables = inspector.get_table_names()
            logger.debug(f"Tables found by inspector: {found_tables}")
            return "alembic_version" in found_tables

        is_managed = await conn.run_sync(inspector_sync)  # No tuple return needed
        logger.info(f"Alembic version table found: {is_managed}")
        return is_managed


async def _log_current_revision(engine: AsyncEngine) -> None:
    """Logs the current Alembic revision stored in the database."""
    logger.info("Querying current revision from alembic_version table...")
    async with engine.connect() as conn_check:

        def sync_check_revision(sync_conn: Connection) -> str | None:
            inspector = inspect(sync_conn)
            if inspector is None:
                logger.error(
                    "SQLAlchemy inspector could not be created for revision check."
                )
                raise RuntimeError(
                    "Failed to create SQLAlchemy inspector for revision check."
                )
            if "alembic_version" not in inspector.get_table_names():
                logger.warning(
                    "Alembic version table not found when trying to query revision."
                )
                return None
            try:
                result = sync_conn.execute(
                    text("SELECT version_num FROM alembic_version")
                )
                return result.scalar_one_or_none()  # Fetch one scalar value or None
            except Exception as query_err:
                logger.error(
                    f"Error querying alembic_version table: {query_err!r}",
                    exc_info=True,
                )
                return None

        current_revision = await conn_check.run_sync(sync_check_revision)
        logger.info(
            f"Current Alembic revision in DB: {current_revision or 'Could not determine / Not applicable'}"
        )


def _get_alembic_config(engine: AsyncEngine) -> AlembicConfig:
    """Loads the Alembic configuration."""
    alembic_ini_env_var = os.getenv("ALEMBIC_CONFIG")
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    default_alembic_ini_path = os.path.join(project_root, "alembic.ini")

    if alembic_ini_env_var:
        if os.path.exists(alembic_ini_env_var):
            alembic_ini_path = alembic_ini_env_var
            logger.info(
                f"Using Alembic config from environment variable: {alembic_ini_path}"
            )
        else:
            logger.error(
                f"ALEMBIC_CONFIG environment variable points to non-existent file: {alembic_ini_env_var}"
            )
            raise FileNotFoundError(
                f"Alembic config file specified in ALEMBIC_CONFIG not found: {alembic_ini_env_var}"
            )
    else:
        alembic_ini_path = default_alembic_ini_path
        logger.info(
            f"ALEMBIC_CONFIG env var not set. Using default path: {alembic_ini_path}"
        )
        if not os.path.exists(alembic_ini_path):
            logger.error(
                f"Default alembic config file not found at {alembic_ini_path}. Cannot proceed."
            )
            raise FileNotFoundError(
                f"Alembic config file not found: {alembic_ini_path}"
            )

    alembic_cfg = AlembicConfig(alembic_ini_path)
    alembic_cfg.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False)
    )
    logger.info(
        f"Alembic config loaded. Using script_location: {alembic_cfg.get_main_option('script_location')}"
    )
    return alembic_cfg


async def _run_alembic_command(
    engine: AsyncEngine, config: AlembicConfig, command_name: str, *args: Any
) -> None:
    """Executes an Alembic command asynchronously using the engine's connection."""
    command_func = getattr(alembic_command, command_name)
    command_args_str = ", ".join(map(repr, args))
    logger.info(
        f"Attempting to run Alembic command '{command_name}' with args ({command_args_str}) via run_sync..."
    )

    async with engine.connect() as conn:

        def sync_command_wrapper(
            sync_conn: Connection,
            cfg: AlembicConfig,
            cmd_func: Callable[..., None],
            cmd_args: tuple[Any, ...],
        ) -> None:
            """Wrapper to run alembic commands with existing connection."""
            # Make the connection available to env.py via config attributes
            cfg.attributes["connection"] = sync_conn
            logger.info(
                f"Executing alembic_command.{command_name}(...) using connection {sync_conn!r}"
            )
            try:
                cmd_func(cfg, *cmd_args)
                logger.info(
                    f"Alembic command '{command_name}' executed successfully with args ({command_args_str})."
                )
            except Exception as e:  # Catch any exception from the command itself
                logger.error(
                    f"Error executing Alembic command '{command_name}' inside run_sync: {e!r}",
                    exc_info=True,
                )
                raise  # Re-raise to be caught by the outer try block

        try:
            # Pass the necessary arguments to the sync wrapper
            await conn.run_sync(sync_command_wrapper, config, command_func, args)
            logger.info(
                f"Alembic command '{command_name}' completed successfully via run_sync."
            )
        except Exception as e:
            # This catches errors from run_sync itself or re-raised from sync_command_wrapper
            logger.error(
                f"Failed running Alembic command '{command_name}' via run_sync: {e!r}",
                exc_info=True,
            )
            raise


async def _create_initial_schema(engine: AsyncEngine) -> None:
    """Creates all tables defined in the SQLAlchemy metadata."""
    logger.info("Creating tables from SQLAlchemy metadata...")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    logger.info("Tables created.")


async def _initialize_vector_storage(engine: AsyncEngine) -> None:
    """Initializes vector database components if enabled."""
    if VECTOR_STORAGE_ENABLED:
        logger.info("Initializing vector DB components...")
        try:
            # Use DatabaseContext which handles its own retry logic for execution
            async with DatabaseContext(engine=engine) as vector_init_context:
                # Assuming init_vector_db performs necessary table checks/creations
                await init_vector_db(db_context=vector_init_context)
            logger.info("Vector DB components initialized successfully.")
        except Exception as vec_e:
            logger.error(
                f"Failed to initialize vector database components: {vec_e!r}",
                exc_info=True,
            )
            # Decide if this failure should prevent startup. For now, re-raise.
            raise
    else:
        logger.info("Vector storage is disabled, skipping initialization.")


# --- Main Initialization Function ---


async def init_db() -> None:
    """
    Initializes the database:
    - Checks if the database is managed by Alembic.
    - If managed, runs Alembic upgrade to head.
    - If not managed, creates the initial schema using SQLAlchemy metadata,
      stamps the database with the Alembic head revision, and initializes
      vector storage if enabled.
    - Includes retry logic for transient database errors.
    """
    max_retries = 5
    base_delay = 1.0
    engine = get_engine()  # Get engine from db_base
    last_exception: Exception | None = (
        None  # Variable to store the last exception, typed broadly
    )
    for attempt in range(max_retries):
        last_exception = None  # Reset last exception for this attempt
        try:
            logger.info(f"Checking database state (attempt {attempt + 1})...")

            # 1. Load Alembic configuration (raises FileNotFoundError if config missing)
            alembic_cfg = _get_alembic_config(engine)

            # 2. Check if the database is already managed by Alembic
            # This might raise connection errors, caught by the outer handler
            has_version_table = await _is_alembic_managed(engine)

            if has_version_table:
                # 3a. Log current revision and upgrade
                await _log_current_revision(engine)  # Log current state before upgrade
                await _run_alembic_command(engine, alembic_cfg, "upgrade", "head")
            else:
                # 3b. Database is new or not managed by Alembic.
                logger.info(
                    "Alembic version table not found. Performing initial schema creation and stamping..."
                )

                # 4. Create all tables defined in SQLAlchemy metadata
                await _create_initial_schema(engine)

                # 5. Ensure the alembic_version table exists before stamping
                #    (Technically stamp might create it, but ensure_version is safer & explicit)
                await _run_alembic_command(engine, alembic_cfg, "ensure_version")

                # 6. Stamp the database with the latest revision ('head')
                await _run_alembic_command(engine, alembic_cfg, "stamp", "head")
                logger.info("Database schema stamped with Alembic 'head' revision.")

                # 7. Initialize vector DB parts if enabled (only on initial creation)
                await _initialize_vector_storage(engine)

            # If upgrade or creation/stamp sequence was successful
            logger.info("Database initialization successful.")
            return  # Exit the function successfully

        # --- Exception Handling and Retry Logic ---
        except (DBAPIError, OperationalError) as e:
            # Handle common database connection/operation errors
            logger.warning(
                f"Database connection/operation error during init_db (attempt {attempt + 1}/{max_retries}): {e!r}. Retrying..."
            )
            last_exception = e
            if attempt == max_retries - 1:
                logger.error(
                    "Max retries exceeded for init_db due to connection/operation error.",
                    exc_info=True,
                )
                # No sleep needed on the last attempt, will raise below
        except SQLAlchemyError as e:
            # Catch other SQLAlchemy specific errors (might include issues during commands not caught by _run_alembic_command)
            logger.warning(
                f"SQLAlchemy error during init_db (attempt {attempt + 1}/{max_retries}): {e!r}",
                exc_info=True,
            )
            last_exception = e
            if attempt == max_retries - 1:
                logger.error(
                    "Max retries exceeded for init_db due to SQLAlchemy error.",
                    exc_info=True,
                )
        except FileNotFoundError as e:
            # Specific handling for missing alembic config file
            logger.error(
                f"Configuration error during init_db: {e}", exc_info=False
            )  # Don't need full traceback
            # This is not recoverable by retrying, so re-raise immediately
            raise
        except Exception as e:
            # Catch any other unexpected exception during init (e.g., logic errors)
            logger.warning(
                f"Unexpected error during database initialization (attempt {attempt + 1}/{max_retries}): {e!r}",
                exc_info=True,
            )
            last_exception = e  # Update last_exception for generic errors too
            if attempt == max_retries - 1:
                logger.error(
                    f"Max retries exceeded for init_db due to error: {e}", exc_info=True
                )

        # If it wasn't the last attempt and an exception occurred, wait and retry
        if attempt < max_retries - 1 and last_exception:
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay * 0.5)
            logger.info(f"Retrying init_db in {delay:.2f} seconds...")
            await asyncio.sleep(delay)
        elif attempt == max_retries - 1 and last_exception:
            # If it was the last attempt and there was an error, break the loop to raise below
            break

    # This part is reached only if the loop completes without returning (all retries failed)
    logger.critical(
        f"Database initialization failed after {max_retries} attempts. Last error: {last_exception!r}",
        exc_info=last_exception,
    )
    # Ensure a meaningful error is raised if all retries fail
    raise RuntimeError(
        f"Database initialization failed after multiple retries. Last error: {last_exception!r} with traceback "
        + "\n".join(traceback.format_exception(last_exception))
    )


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
    "get_db_context",
    "get_note_by_title",
    "update_message_error_traceback",
    # Vector Storage Exports are added conditionally below
    # The names themselves will be defined (real or placeholder)
    # __all__ controls `from .storage import *` and documents the public API
]

# Extend __all__ conditionally for vector storage if it was enabled.
# Check if vector_storage specific names are available and if the feature is enabled.
if (
    VECTOR_STORAGE_ENABLED and "init_vector_db" in locals()
):  # 'init_vector_db' is a proxy for successful import
    __all__.extend([
        "add_document",
        "VectorBase",
        "init_vector_db",
        "get_document_by_source_id",
        "get_document_by_id",  # Added for completeness
        "add_embedding",
        "delete_document",
        "query_vectors",
        "Document",  # Changed from VectorDocumentProtocol to match actual name
    ])
# --- Email Storage (Moved to storage/email.py, re-exported here for compatibility) ---
