import asyncio
import logging
import os
import random
import traceback
from typing import Any

from alembic.config import Config as AlembicConfig
from sqlalchemy import (
    inspect,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    DBAPIError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command as alembic_command

# Import base components using absolute package paths
from family_assistant.paths import PROJECT_ROOT
from family_assistant.storage.base import (
    create_engine_with_sqlite_optimizations,
    metadata,
)
from family_assistant.storage.context import DatabaseContext, get_db_context

# Import table definitions for direct use
from family_assistant.storage.email import received_emails_table
from family_assistant.storage.error_logs import error_logs_table
from family_assistant.storage.events import (
    EventActionType,
    EventSourceType,
    InterfaceType,
    event_listeners_table,
    recent_events_table,
)
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.notes import notes_table
from family_assistant.storage.push_subscription import push_subscriptions_table
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import tasks_table

logger = logging.getLogger(__name__)

# --- Vector Storage Imports ---
try:
    # Use absolute package path
    from family_assistant.storage.vector import (  # noqa: PLC0415
        Base as VectorBase,  # For ORM
    )
    from family_assistant.storage.vector import (  # noqa: PLC0415
        Document,  # Protocol for document structure
    )

    VECTOR_STORAGE_ENABLED = True
except ImportError:
    # Define placeholder types if vector_storage is not available
    class Base:  # type: ignore
        pass

    VectorBase = Base  # type: ignore
    Document = Any  # type: ignore # Placeholder for the Document protocol if import fails
    logger.warning("storage/vector.py not found. Vector storage features disabled.")
    VECTOR_STORAGE_ENABLED = False


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
    project_root = str(PROJECT_ROOT)
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
    # ConfigParser uses % for interpolation, so we need to escape them as %%
    # This is especially important when URLs contain encoded characters like %2A
    db_url = engine.url.render_as_string(hide_password=False)
    escaped_url = db_url.replace("%", "%%")
    alembic_cfg.set_main_option("sqlalchemy.url", escaped_url)
    logger.info(
        f"Alembic config loaded. Using script_location: {alembic_cfg.get_main_option('script_location')}"
    )
    return alembic_cfg


async def _run_alembic_command(
    engine: AsyncEngine, config: AlembicConfig, command_name: str, *args: str
) -> None:
    """Executes an Alembic command asynchronously with detailed logging."""
    command_func = getattr(alembic_command, command_name)
    args_repr = ", ".join(map(repr, args))
    logger.info(f"Preparing to run Alembic command: {command_name}({args_repr})")

    async with engine.connect() as conn:

        def sync_command_wrapper(sync_conn: Connection) -> None:
            """Wrapper to run alembic commands with an existing connection."""
            logger.info(f"[sync] Setting connection for Alembic: {sync_conn!r}")
            config.attributes["connection"] = sync_conn
            logger.info(f"[sync] Executing: {command_name}({args_repr})")
            try:
                command_func(config, *args)
                logger.info(f"[sync] Successfully executed: {command_name}")
            except Exception as e:
                logger.error(
                    f"[sync] Error during {command_name}: {e!r}", exc_info=True
                )
                raise

        try:
            await conn.run_sync(sync_command_wrapper)
            logger.info(f"Alembic command {command_name} completed.")
        except Exception as e:
            logger.error(
                f"Failed to run Alembic command {command_name}: {e!r}", exc_info=True
            )
            raise


async def _create_initial_schema(engine: AsyncEngine) -> None:
    """Creates all tables defined in the SQLAlchemy metadata."""
    logger.info("Creating tables from SQLAlchemy metadata...")
    async with engine.begin() as conn:
        # For PostgreSQL, create pgvector extension before creating tables
        if engine.dialect.name == "postgresql":
            logger.info("Creating pgvector extension for PostgreSQL...")
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            logger.info("pgvector extension created or already exists.")

        await conn.run_sync(metadata.create_all)
    logger.info("Tables created.")


async def _initialize_vector_storage(engine: AsyncEngine) -> None:
    """Initializes vector database components if enabled."""
    if VECTOR_STORAGE_ENABLED:
        logger.info("Initializing vector DB components...")
        try:
            # Use DatabaseContext which handles its own retry logic for execution
            async with DatabaseContext(engine=engine) as vector_init_context:
                await vector_init_context.vector.init_db()
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


async def init_db(engine: AsyncEngine) -> None:
    """
    Initializes the database with robust retry logic.

    - Checks for Alembic management and runs upgrades if needed.
    - Otherwise, creates schema, stamps with Alembic head, and initializes vector storage.
    """
    max_retries = 10
    base_delay = 2.0  # Increased base delay
    last_exception: Exception | None = None

    for attempt in range(max_retries):
        try:
            logger.info(
                f"Checking database state (attempt {attempt + 1}/{max_retries})..."
            )
            alembic_cfg = _get_alembic_config(engine)
            has_version_table = await _is_alembic_managed(engine)

            if has_version_table:
                await _log_current_revision(engine)
                await _run_alembic_command(engine, alembic_cfg, "upgrade", "head")
            else:
                logger.info("New DB: creating schema, stamping with Alembic head...")
                await _create_initial_schema(engine)
                await _run_alembic_command(engine, alembic_cfg, "ensure_version")
                await _run_alembic_command(engine, alembic_cfg, "stamp", "head")
                await _initialize_vector_storage(engine)

            logger.info("Database initialization successful.")
            return

        except (DBAPIError, OperationalError) as e:
            last_exception = e
            logger.warning(
                f"DB connection error on attempt {attempt + 1}: {e!r}. Retrying..."
            )
        except SQLAlchemyError as e:
            last_exception = e
            logger.warning(
                f"SQLAlchemy error on attempt {attempt + 1}: {e!r}. Retrying..."
            )
        except FileNotFoundError as e:
            logger.error(f"Configuration error: {e}")
            raise
        except Exception as e:
            last_exception = e
            logger.error(
                f"Unexpected error on attempt {attempt + 1}: {e!r}", exc_info=True
            )

        if attempt < max_retries - 1:
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay * 0.5)
            logger.info(f"Retrying init_db in {delay:.2f} seconds...")
            await asyncio.sleep(delay)

    logger.critical(
        f"Database initialization failed after {max_retries} attempts. Last error: {last_exception!r}",
        exc_info=last_exception,
    )
    raise RuntimeError(
        f"Database initialization failed after multiple retries. Last error: {last_exception!r} with traceback "
        + "\n".join(traceback.format_exception(last_exception))
    )


# --- Exports ---
# Re-export functions and tables from specific modules to maintain the facade
# Define __all__ AFTER all functions/variables it references are defined.
__all__ = [
    "init_db",  # Now defined above
    "create_engine_with_sqlite_optimizations",
    # Tables - still exported for direct use
    "notes_table",
    "message_history_table",
    "tasks_table",
    "received_emails_table",
    "error_logs_table",
    "event_listeners_table",
    "recent_events_table",
    "schedule_automations_table",
    "push_subscriptions_table",
    "metadata",
    "DatabaseContext",  # Export the new context manager
    "get_db_context",
    # Enums
    "EventActionType",
    "EventSourceType",
    "InterfaceType",
    # Vector Storage Exports are added conditionally below
    # The names themselves will be defined (real or placeholder)
    # __all__ controls `from .storage import *` and documents the public API
]

# Extend __all__ conditionally for vector storage if it was enabled.
# Check if vector_storage specific names are available and if the feature is enabled.
if VECTOR_STORAGE_ENABLED:
    __all__.extend([
        "VectorBase",
        "Document",  # Protocol for document structure
    ])
# --- Email Storage (Moved to storage/email.py, re-exported here for compatibility) ---
