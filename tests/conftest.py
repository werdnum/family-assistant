import pytest
import asyncio
import logging
import os
import sqlalchemy as sa  # Add this import
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine
from unittest.mock import patch
from testcontainers.postgres import PostgresContainer


# Import the metadata and the original engine object from your storage base
from family_assistant.storage.base import metadata, engine as original_engine
from family_assistant.storage import init_db  # Import init_db

# Explicitly import the module defining the tasks table to ensure metadata registration
import family_assistant.storage.tasks

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

import docker  # Import the docker library to catch its exceptions


@pytest.fixture(scope="session")
def postgres_container():
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

        # --- Create PostgreSQL-specific Indexes AFTER tables exist ---
        logger.info("Creating PostgreSQL-specific indexes (HNSW, FTS)...")
        async with DatabaseContext(engine=engine) as db_context_for_indexes:
            # HNSW Index (copy logic from original init_vector_db)
            await db_context_for_indexes.execute_with_retry(
                sa.text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_doc_embeddings_gemini_1536_hnsw_cos ON document_embeddings
                    USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64) WHERE embedding_model = 'gemini-exp-03-07';
                    """
                )
            )
            logger.info(
                "Ensured HNSW index idx_doc_embeddings_gemini_1536_hnsw_cos exists."
            )

            # FTS Index (copy logic from original init_vector_db)
            await db_context_for_indexes.execute_with_retry(
                sa.text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_doc_embeddings_content_fts_gin ON document_embeddings
                    USING gin (to_tsvector('english', content))
                    WHERE content IS NOT NULL;
                    """
                )
            )
            logger.info("Ensured FTS index idx_doc_embeddings_content_fts_gin exists.")
        logger.info("PostgreSQL-specific indexes created.")

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
