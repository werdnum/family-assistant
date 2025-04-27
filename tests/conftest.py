import pytest
import asyncio
import logging
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from unittest.mock import patch
from testcontainers.postgres import PostgresContainer

# Import the metadata and the original engine object from your storage base
from family_assistant.storage.base import metadata, engine as original_engine
from family_assistant.storage import init_db  # Import init_db
# Import vector storage init and context
from family_assistant.storage.vector import init_vector_db
from family_assistant.storage.context import DatabaseContext

# Configure logging for tests (optional, but can be helpful)
logging.basicConfig(level=logging.INFO)
import pytest_asyncio  # Import the correct decorator

logger = logging.getLogger(__name__)


@pytest_asyncio.fixture(scope="function", autouse=True)  # Use pytest_asyncio.fixture
async def test_db_engine(request):  # Add request fixture
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
def postgres_container():
    """Starts and manages a PostgreSQL container for the test session."""
    # Ensure Docker is running and accessible
    docker_socket = "/var/run/docker.sock"
    if not os.path.exists(docker_socket):
        # Fail the test session if Docker socket is missing
        pytest.fail(f"Docker socket not found at {docker_socket}. Is Docker running? PostgreSQL tests require Docker.", pytrace=False)
    elif not os.access(docker_socket, os.R_OK | os.W_OK):
         # Fail the test session if Docker socket has incorrect permissions
         pytest.fail(f"Insufficient permissions for Docker socket at {docker_socket}. Check user permissions. PostgreSQL tests require Docker access.", pytrace=False)
    # Add more robust checks here if needed (e.g., try connecting with docker client)

    # Use an image that includes postgresql-contrib for extensions like pgvector
    # Note: pgvector might need explicit installation depending on the base image.
    # If using a standard postgres image, you might need to execute
    # `CREATE EXTENSION IF NOT EXISTS vector;` after connection.
    # Using a dedicated pgvector image simplifies this.
    # image = "postgres:16-alpine" # Standard image, might need manual extension creation
    image = "ankane/pgvector:v0.7.0-pg16" # Image with pgvector pre-installed
    logger.info(f"Starting PostgreSQL container with image: {image}")
    with PostgresContainer(image=image) as container:
        logger.info("PostgreSQL container started.")
        # Optional: Add readiness checks if needed
        yield container
    logger.info("PostgreSQL container stopped.")


@pytest_asyncio.fixture(scope="function") # Use function scope for engine to ensure isolation
async def pg_vector_db_engine(postgres_container: PostgresContainer) -> AsyncEngine:
    """
    Creates an AsyncEngine connected to the test PostgreSQL container,
    initializes the schema (including vector components), and yields the engine.
    """
    # Get sync connection URL and adapt for asyncpg
    sync_url = postgres_container.get_connection_url()
    # Replace postgresql:// with postgresql+asyncpg://
    async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    logger.info(f"Creating async engine for test PostgreSQL: {async_url.split('@')[-1]}")
    engine = create_async_engine(async_url, echo=False) # Set echo=True for debugging SQL

    try:
        # Initialize the main database schema (all tables)
        logger.info("Initializing main database schema...")
        await init_db(engine=engine) # Pass the engine explicitly
        logger.info("Main database schema initialized.")

        # Initialize vector-specific components (extension, indexes)
        # This needs to run after tables are created by init_db
        logger.info("Initializing vector database components...")
        # Use DatabaseContext to manage transaction for init_vector_db
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("Vector database components initialized.")

        yield engine # Provide the initialized engine to tests

    finally:
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
