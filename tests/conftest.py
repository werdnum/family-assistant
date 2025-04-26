import pytest
import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine
from unittest.mock import patch

# Import the metadata and the original engine object from your storage base
from family_assistant.storage.base import metadata, engine as original_engine
from family_assistant.storage import init_db  # Import init_db

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


# You can add other fixtures here later, e.g., for mocking LLM or config
