import asyncio
import logging
import os
import pathlib
import shutil
import socket
import subprocess
import sys  # Import sys module
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

import caldav
import pytest
import pytest_asyncio  # Import the correct decorator
from caldav.lib import error as caldav_error  # Import the error module
from docker.errors import DockerException  # Import DockerException directly
from passlib.hash import bcrypt
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# Import for task_worker_manager fixture
from family_assistant.processing import ProcessingService  # Import ProcessingService

# Import the metadata and the original engine object from your storage base
from family_assistant.storage import init_db  # Import init_db
from family_assistant.storage.context import DatabaseContext

# Explicitly import the module defining the tasks table to ensure metadata registration
# Import vector storage init and context
from family_assistant.storage.vector import init_vector_db  # Corrected import path
from family_assistant.task_worker import TaskWorker

# Configure logging for tests (optional, but can be helpful)
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)


def find_free_port() -> int:
    """Finds a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    except DockerException as e:  # Use the directly imported DockerException
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
async def pg_vector_db_engine(
    postgres_container: PostgresContainer,
) -> AsyncGenerator[AsyncEngine, None]:
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
async def task_worker_manager() -> AsyncGenerator[
    tuple[TaskWorker, asyncio.Event, asyncio.Event], None
]:
    """
    Manages the lifecycle of a TaskWorker instance.

    Yields a tuple: (TaskWorker instance, new_task_event, shutdown_event).
    The TaskWorker is initialized with default parameters and has no handlers
    registered by default. Tests using this fixture are responsible for
    registering necessary handlers on the yielded worker instance.
    """
    mock_chat_interface = MagicMock()  # Mock ChatInterface
    mock_embedding_gen = MagicMock()
    new_task_event_for_worker = asyncio.Event()  # Event for the worker itself

    worker = TaskWorker(
        processing_service=MagicMock(spec=ProcessingService),
        chat_interface=mock_chat_interface,  # Pass mock ChatInterface
        new_task_event=new_task_event_for_worker,  # Pass the event for task notification
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=mock_embedding_gen,
    )

    worker_task_handle = None
    shutdown_event = asyncio.Event()
    # new_task_event is now new_task_event_for_worker, which is passed to worker.run

    try:
        # The worker.run method now takes the event it should listen on.
        # This is the same event that tasks will use to notify it.
        worker_task_handle = asyncio.create_task(worker.run(new_task_event_for_worker))
        logger.info("Started background TaskWorker (fixture).")
        await asyncio.sleep(0.1)  # Give worker time to start
        yield worker, new_task_event_for_worker, shutdown_event
    finally:
        if worker_task_handle:
            logger.info("Stopping background TaskWorker (fixture)...")
            shutdown_event.set()
            new_task_event_for_worker.set()  # Wake up worker if it's waiting on this
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


# --- Radicale CalDAV Server Fixture ---

RADICALE_TEST_USER = "testuser"
RADICALE_TEST_PASS = "testpass"
# RADICALE_TEST_CALENDAR_NAME is no longer needed here as calendars are per-function


@pytest.fixture(scope="session")
def radicale_server_session() -> Generator[tuple[str, str, str], None, None]:
    """
    Manages a Radicale CalDAV server instance for the entire test session.
    Sets up the server process and user, but does not create a specific calendar.

    Yields:
        tuple: (base_url, username, password)
    """
    temp_dir = tempfile.mkdtemp(prefix="radicale_test_")
    collections_dir = pathlib.Path(temp_dir) / "collections"
    collections_dir.mkdir(parents=True, exist_ok=True)
    config_file_path = pathlib.Path(temp_dir) / "radicale_config"
    htpasswd_file_path = pathlib.Path(temp_dir) / "users"

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Create htpasswd file
    hashed_password = bcrypt.hash(RADICALE_TEST_PASS)
    with open(htpasswd_file_path, "w", encoding="utf-8") as f:
        f.write(f"{RADICALE_TEST_USER}:{hashed_password}\n")

    # Create Radicale config file
    radicale_config_content = f"""
[server]
hosts = 127.0.0.1:{port}
ssl = false

[auth]
type = htpasswd
htpasswd_filename = {htpasswd_file_path}
htpasswd_encryption = bcrypt

[storage]
filesystem_folder = {collections_dir}

[logging]
level = debug
request_header_on_debug = True
request_content_on_debug = True
backtrace_on_debug = True
    """
    with open(config_file_path, "w", encoding="utf-8") as f:
        f.write(radicale_config_content)

    process = None
    try:
        logger.info(
            f"Starting Radicale server on port {port} with storage at {collections_dir}"
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "radicale", "--config", str(config_file_path)],
        )

        # Wait for Radicale to start
        max_wait_time = 30  # seconds
        start_time = time.time()
        server_ready = False
        while time.time() - start_time < max_wait_time:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    logger.info(f"Radicale server is up on port {port}.")
                    server_ready = True
                    break
            except (TimeoutError, ConnectionRefusedError):
                time.sleep(0.5)
                if process.poll() is not None:  # Check if process terminated
                    process.communicate()  # Ensure process is reaped
                    logger.error(
                        "Radicale process terminated prematurely. Check test output for Radicale logs."
                    )
                    pytest.fail("Radicale server failed to start.")

        if not server_ready:
            process.communicate()  # Ensure process is reaped
            logger.error(
                f"Radicale server did not start within {max_wait_time}s. Check test output for Radicale logs."
            )
            pytest.fail(
                f"Radicale server did not start on port {port} within {max_wait_time} seconds."
            )

        # Give Radicale a moment more to settle after port is open
        time.sleep(2)  # Added delay

        # Session fixture no longer creates a default calendar.
        # It only ensures the server is running and the user exists.
        try:
            # Verify user principal exists by making a client connection
            client = caldav.DAVClient(
                url=base_url,
                username=RADICALE_TEST_USER,
                password=RADICALE_TEST_PASS,
                timeout=30,
            )
            # This call will create the user's collection if it doesn't exist
            client.principal()  # Direct synchronous call
            logger.info(
                f"Radicale server session ready. User '{RADICALE_TEST_USER}' principal checked/created."
            )
        except Exception as e_prin:
            logger.error(
                f"Failed to verify/create user principal for '{RADICALE_TEST_USER}' in Radicale: {e_prin}",
                exc_info=True,
            )
            pytest.fail(f"Radicale user principal setup failed: {e_prin}")

        yield base_url, RADICALE_TEST_USER, RADICALE_TEST_PASS

    finally:
        if process:
            logger.info("Stopping Radicale server...")
            process.terminate()
            try:
                process.wait(timeout=10)
                logger.info("Radicale server stopped.")
            except subprocess.TimeoutExpired:
                logger.warning("Radicale server did not stop in time, killing.")
                process.kill()
                process.wait()
                logger.info("Radicale server killed.")
            # Ensure the process is reaped. Output would have gone to test output.
            process.communicate()

        shutil.rmtree(temp_dir)
        logger.info(f"Cleaned up Radicale temp directory: {temp_dir}")


@pytest_asyncio.fixture(scope="function")
async def radicale_server(
    radicale_server_session: tuple[str, str, str],  # Now yields 3 items
    pg_vector_db_engine: AsyncEngine,
    request: pytest.FixtureRequest,  # To get test name for unique calendar
) -> AsyncGenerator[tuple[str, str, str, str], None]:
    """
    Provides Radicale server details for a single test function.
    Creates a new, unique calendar for each test.
    Yields: (base_url, username, password, unique_calendar_url)
    """
    base_url, username, password = radicale_server_session
    # Generate a unique calendar name for this test function
    # Sanitize test name to be a valid URL component
    test_name_sanitized = "".join(c if c.isalnum() else "_" for c in request.node.name)
    unique_calendar_name = f"testcal_{test_name_sanitized}_{uuid.uuid4().hex[:8]}"
    # For Radicale, cal_id is often the last path component of the calendar URL.
    # It's safer to let make_calendar determine the final URL structure.
    # We'll use unique_calendar_name as the display name and also suggest it for the ID.
    unique_calendar_resource_id = unique_calendar_name.lower()

    client = caldav.DAVClient(
        url=base_url, username=username, password=password, timeout=30
    )
    principal = await asyncio.to_thread(client.principal)
    new_calendar_obj = None  # To store the created calendar object for cleanup

    try:
        logger.info(
            f"Creating unique Radicale calendar with name '{unique_calendar_name}' and id '{unique_calendar_resource_id}' for test {request.node.name}"
        )
        # Use principal.make_calendar()
        # This is a synchronous call, so wrap it with to_thread
        new_calendar_obj = await asyncio.to_thread(
            principal.make_calendar,
            name=unique_calendar_name,
            cal_id=unique_calendar_resource_id,
        )

        assert new_calendar_obj is not None, "make_calendar did not return a calendar object."
        assert new_calendar_obj.url is not None, "Created calendar has no URL."
        unique_calendar_url = str(new_calendar_obj.url) # Ensure it's a string
        logger.info(
            f"Successfully created unique calendar '{unique_calendar_name}' with URL: {unique_calendar_url}"
        )

        # Verification: Check if the calendar is listable or has events (should be 0)
        # This also implicitly checks if the calendar exists on the server.
        events = await asyncio.to_thread(new_calendar_obj.events)
        assert (
            len(events) == 0
        ), f"Newly created calendar '{unique_calendar_name}' should have 0 events, found {len(events)}."
        logger.info(
            f"Verified newly created calendar '{unique_calendar_name}' exists and has 0 events."
        )

        yield base_url, username, password, unique_calendar_url

    except Exception as e_create:
        logger.error(
            f"Failed during setup of unique Radicale calendar '{unique_calendar_name}': {e_create}",
            exc_info=True,
        )
        pytest.fail(
            f"Radicale unique calendar creation failed for test {request.node.name}: {e_create}"
        )
    finally:
        # Attempt to delete the uniquely created calendar using the object if available
        if new_calendar_obj:
            try:
                logger.info(
                    f"Cleaning up: Deleting unique Radicale calendar '{unique_calendar_name}' using its object."
                )
                await asyncio.to_thread(new_calendar_obj.delete)
                logger.info(
                    f"Successfully deleted unique calendar '{unique_calendar_name}'."
                )
            except caldav_error.NotFoundError:
                logger.info(
                    f"Unique calendar '{unique_calendar_name}' (URL: {getattr(new_calendar_obj, 'url', 'N/A')}) was not found during cleanup (already deleted)."
                )
            except Exception as e_delete:
                logger.error(
                    f"Error deleting unique Radicale calendar '{unique_calendar_name}' (URL: {getattr(new_calendar_obj, 'url', 'N/A')}): {e_delete}",
                    exc_info=True,
                )
                # Don't fail the test run for cleanup errors, but log them.
        else:
            # Fallback if new_calendar_obj was not created (e.g., error before assignment)
            # This part might be less reliable if the URL wasn't successfully determined.
            # However, the primary cleanup relies on new_calendar_obj.
            logger.warning(
                f"Calendar object for '{unique_calendar_name}' was not available for direct deletion. Cleanup might be incomplete if creation failed early."
            )
