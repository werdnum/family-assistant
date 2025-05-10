import asyncio
import logging
import os
from typing import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

import docker  # Import the docker library to catch its exceptions
import pytest
import pytest_asyncio  # Import the correct decorator
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

# Import the metadata and the original engine object from your storage base
from family_assistant.storage import init_db  # Import init_db
from family_assistant.storage.context import DatabaseContext

# Explicitly import the module defining the tasks table to ensure metadata registration
# Import vector storage init and context
from family_assistant.storage.vector import init_vector_db  # Corrected import path

# Import for task_worker_manager fixture
from family_assistant.task_worker import TaskWorker

# Configure logging for tests (optional, but can be helpful)
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture(scope="function", autouse=True)  # Use pytest_asyncio.fixture
async def test_db_engine(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncEngine, None]:  # Add request fixture
    """
    Pytest fixture to set up an in-memory SQLite database for testing.

    This fixture runs automatically for each test function (`autouse=True`).
    It creates an in-memory SQLite engine, initializes the schema,
    and monkeypatches the global `storage.base.engine` to use this
    test engine during the test. It ensures cleanup afterwards.
    """
    # Create an in-memory SQLite engine for testing
    # Using file DB can sometimes be easier for inspection, but memory is faster
    # test_engine = create_async_engine("sqlite+aiosqlite:///./test_family_assistant.db")
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    logger.info(f"\n--- Test DB Setup ({request.node.name}) ---")
    logger.info(f"Created test engine: {test_engine.url}")

    # Patch the global engine used by storage modules
    # The patch needs to target where the 'engine' object is *looked up*
    # by the storage functions. Since they import from .base, we patch it there.
    patcher = patch("family_assistant.storage.base.engine", test_engine)
    patcher.start()  # Start the patch manually
    logger.info("Patched storage.base.engine with test engine.")

    try:
        # Initialize the database schema using the test engine via the patched global
        await init_db()
        logger.info("Database schema initialized in memory.")

        # Yield control to the test function
        yield test_engine  # Test function can optionally use this engine directly

    finally:
        # Cleanup: Stop the patch and dispose the engine
        patcher.stop()  # Stop the patch
        logger.info(f"--- Test DB Teardown ({request.node.name}) ---")
        await test_engine.dispose()
        logger.info("Test engine disposed.")
        logger.info("Restored original storage.base.engine.")


# --- PostgreSQL Test Fixtures (using testcontainers) ---


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """
    Starts and manages a PostgreSQL container for the test session.
    Respects DOCKER_HOST environment variable.
    """
    # Use an image that includes postgresql-contrib for extensions like pgvector
    # Note: pgvector might need explicit installation depending on the base image.
    # If using a standard postgres image, you might need to execute
    # `CREATE EXTENSION IF NOT EXISTS vector;` after connection.
    # Using a dedicated pgvector image simplifies this.
    # image = "postgres:16-alpine" # Standard image, might need manual extension creation
    image = "pgvector/pgvector:0.8.0-pg17"  # Image with pgvector pre-installed
    logger.info(f"Attempting to start PostgreSQL container with image: {image}")
    logger.info(
        f"Using Docker configuration from environment (DOCKER_HOST={os.getenv('DOCKER_HOST', 'Not Set')})"
    )
    try:
        # Specify the asyncpg driver directly
        with PostgresContainer(image=image, driver="asyncpg") as container:
            # Attempt to connect to check readiness early
            container.get_container_host_ip()
            logger.info(
                "PostgreSQL container started successfully with asyncpg driver configuration."
            )
            yield container
        logger.info("PostgreSQL container stopped.")
    except docker.errors.DockerException as e:
        # Catch errors during container startup (e.g., connection refused, image not found)
        pytest.fail(
            f"Failed to start PostgreSQL container. Docker error: {e}. "
            f"Check Docker daemon status and DOCKER_HOST ({os.getenv('DOCKER_HOST', 'default')}).",
            pytrace=False,
        )
    except Exception as e:
        # Catch other potential errors during setup
        pytest.fail(
            f"An unexpected error occurred during PostgreSQL container setup: {e}",
            pytrace=False,
        )


@pytest_asyncio.fixture(
    scope="function"
)  # Use function scope for engine to ensure isolation
async def pg_vector_db_engine(postgres_container: PostgresContainer) -> AsyncEngine:
    """
    Creates an AsyncEngine connected to the test PostgreSQL container,
    initializes the schema (including vector components), and yields the engine.
    """
    # Get the connection URL directly from the container (should include +asyncpg)
    async_url = postgres_container.get_connection_url()
    logger.info(
        f"Creating async engine for test PostgreSQL using URL from container: {async_url.split('@')[-1]}"
    )
    engine = create_async_engine(
        async_url, echo=False
    )  # Set echo=True for debugging SQL
    patcher = None
    try:
        # Patch the global engine used by storage modules to use the PG engine
        # The patch needs to target where the 'engine' object is *looked up*
        # by the storage functions (i.e., in storage.base).
        patcher = patch("family_assistant.storage.base.engine", engine)
        patcher.start()
        logger.info("Patched storage.base.engine with PostgreSQL test engine.")

        # --- Ensure Vector Extension Exists FIRST ---
        # Initialize vector-specific components (extension, indexes) BEFORE creating tables
        logger.info("Initializing vector database components (extension, indexes)...")
        # Use DatabaseContext to manage transaction for init_vector_db
        # We use the test engine directly here for this initial setup step.
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("Vector database components initialized.")

        # --- Initialize the main database schema (all tables) ---
        # Now that the vector extension exists, create all tables using the patched engine
        logger.info("Initializing main database schema on PostgreSQL...")
        await init_db()  # Call without engine argument, relies on patch
        logger.info("Main database schema initialized on PostgreSQL.")

        # Note: Removed manual creation of HNSW/FTS indexes here.
        # init_db() is now responsible for bringing the schema to 'head' via Alembic,
        # which should include the creation of necessary indexes defined in migrations.

        yield engine  # Provide the initialized engine to tests

    finally:
        # Cleanup: Stop the patch and dispose the engine
        if patcher:
            patcher.stop()
            logger.info("Restored original storage.base.engine after PG test.")
        logger.info("Disposing PostgreSQL test engine...")
        await engine.dispose()
        logger.info("PostgreSQL test engine disposed.")


# Note: We don't provide a DatabaseContext fixture directly.
# Tests should create their own context using the pg_vector_db_engine fixture:
#
# async def test_something(pg_vector_db_engine):
#     async with DatabaseContext(engine=pg_vector_db_engine) as db:
#         # Use db.fetch_all, db.execute_with_retry, etc.
#         ...


@pytest_asyncio.fixture(scope="function")
async def task_worker_manager() -> (
    AsyncGenerator[tuple[TaskWorker, asyncio.Event, asyncio.Event], None]
):
    """
    Manages the lifecycle of a TaskWorker instance.

    Yields a tuple: (TaskWorker instance, new_task_event, shutdown_event).
    The TaskWorker is initialized with default parameters and has no handlers
    registered by default. Tests using this fixture are responsible for
    registering necessary handlers on the yielded worker instance.
    """
    mock_application = MagicMock()  # Generic mock, tests can replace if needed
    worker = TaskWorker(
        processing_service=None,  # Default, can be customized by tests
        application=mock_application,
        calendar_config={},  # Default
        timezone_str="UTC",  # Default
    )

    worker_task_handle = None
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    try:
        worker_task_handle = asyncio.create_task(worker.run(new_task_event))
        logger.info("Started background TaskWorker (fixture).")
        await asyncio.sleep(0.1)  # Give worker time to start
        yield worker, new_task_event, shutdown_event
    finally:
        if worker_task_handle:
            logger.info("Stopping background TaskWorker (fixture)...")
            shutdown_event.set()
            new_task_event.set()  # Wake up worker if it's waiting on this
            try:
                await asyncio.wait_for(worker_task_handle, timeout=5.0)
                logger.info("Background TaskWorker (fixture) stopped gracefully.")
            except asyncio.TimeoutError:
                logger.warning("Timeout stopping TaskWorker (fixture). Cancelling.")
                worker_task_handle.cancel()
                try:
                    await worker_task_handle
                except asyncio.CancelledError:
                    logger.info("TaskWorker (fixture) cancellation confirmed.")
            except Exception as e:
                logger.error(
                    f"Error during TaskWorker (fixture) shutdown: {e}", exc_info=True
                )
