import asyncio
import contextlib
import hashlib
import logging
import os
import pathlib
import random
import shutil
import socket
import subprocess
import sys  # Import sys module
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI

from unittest.mock import MagicMock

import caldav
import pytest
import pytest_asyncio  # Import the correct decorator
import vcr

# Try to import pgserver, but it's optional if TEST_DATABASE_URL is provided
try:
    import pgserver  # pyright: ignore[reportMissingImports]
except ImportError:
    pgserver = None  # type: ignore[assignment]
from caldav.lib import error as caldav_error  # Import the error module
from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import family_assistant.storage.tasks as tasks_module

# Import for task_worker_manager fixture
from family_assistant.processing import ProcessingService  # Import ProcessingService
from family_assistant.services.attachment_registry import AttachmentRegistry

# Import the metadata and the original engine object from your storage base
from family_assistant.storage import init_db  # Import init_db
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.context import DatabaseContext

# Explicitly import the module defining the tasks table to ensure metadata registration
# Import vector storage init and context
from family_assistant.storage.vector import init_vector_db  # Corrected import path
from family_assistant.task_worker import TaskWorker
from family_assistant.utils.clock import MockClock
from family_assistant.web.app_creator import app as fastapi_app

# Configure logging for tests (optional, but can be helpful)
logging.basicConfig(level=logging.INFO)

# Disable SQLAlchemy error handler for tests to avoid connection issues
# when the database is disposed
os.environ["FAMILY_ASSISTANT_DISABLE_DB_ERROR_LOGGING"] = "1"

logger = logging.getLogger(__name__)


@pytest.fixture(scope="function")
def app(db_engine: AsyncEngine) -> Generator[Any, None, None]:
    """Provide the FastAPI app instance for testing."""
    fastapi_app.state.database_engine = db_engine
    yield fastapi_app


@pytest.fixture
def attachment_registry_fixture(
    app: "FastAPI", db_engine: AsyncEngine
) -> AttachmentRegistry:
    """Provide attachment registry for tests that need it."""
    if (
        not hasattr(app.state, "attachment_registry")
        or app.state.attachment_registry is None
    ):
        storage_path = tempfile.mkdtemp()
        registry = AttachmentRegistry(
            storage_path=storage_path,
            db_engine=db_engine,
            config=None,
        )
        app.state.attachment_registry = registry
    return app.state.attachment_registry


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options for pytest."""
    parser.addoption(
        "--postgres",
        action="store_true",
        default=False,
        help="Run tests with PostgreSQL instead of SQLite (deprecated, use --db)",
    )
    parser.addoption(
        "--db",
        action="store",
        default="all",
        choices=("sqlite", "postgres", "all"),
        help="Database backend to test against: sqlite, postgres, or all (default)",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """
    Dynamically parameterizes fixtures based on the --db option.

    This hook is called by pytest for each test function. It checks if the test
    is requesting the `db_engine` fixture. If so, it parameterizes it with the
    database backends selected via the `--db` command-line flag.
    """
    # Skip database parameterization for tests marked with no_db
    if metafunc.definition.get_closest_marker("no_db"):
        return

    # Check if db_engine is needed - either directly or through autouse fixture
    # Since db_engine (autouse) depends on db_engine, we need to parameterize
    # db_engine for ALL tests now
    if "db_engine" in metafunc.fixturenames:
        # Get the --db option value, with backwards compatibility for --postgres
        db_option = metafunc.config.getoption("--db")
        use_postgres_flag = metafunc.config.getoption("--postgres")

        # Handle backwards compatibility
        if use_postgres_flag and db_option == "all":
            db_option = "postgres"

        db_backends = []
        if db_option in {"sqlite", "all"}:
            db_backends.append("sqlite")
        if db_option in {"postgres", "all"}:
            db_backends.append("postgres")

        # Check if the test is requesting pg_vector_db_engine
        # These tests should only run with postgres
        if "pg_vector_db_engine" in metafunc.fixturenames:
            # Only add postgres parameter for these tests
            if "postgres" in db_backends:
                metafunc.parametrize("db_engine", ["postgres"], indirect=True)
            else:
                # Skip this test if postgres is not in the selected backends
                metafunc.parametrize("db_engine", [], indirect=True)
        elif metafunc.definition.get_closest_marker("postgres"):
            # Postgres-only tests
            if "postgres" in db_backends:
                metafunc.parametrize("db_engine", ["postgres"], indirect=True)
            else:
                # Skip this test if postgres is not in the selected backends
                metafunc.parametrize("db_engine", [], indirect=True)
        else:
            # Regular tests get all selected backends
            metafunc.parametrize("db_engine", db_backends, indirect=True)


# Port allocation now handled by worker-specific ranges - no global tracking needed


def find_free_port() -> int:
    """Find a free port, using worker-specific ranges when running under pytest-xdist."""

    # Check if we're running under pytest-xdist
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")

    if worker_id and worker_id.startswith("gw"):
        # Extract worker number (gw0 -> 0, gw1 -> 1, etc.)
        worker_num = int(worker_id[2:])

        # Each worker gets 2000 ports (enough for any test suite)
        base_port = 40000 + (worker_num * 2000)
        max_port = base_port + 1999

        # Try random ports in our range until we find a free one
        for _ in range(100):  # Max 100 attempts
            port = random.randint(base_port, max_port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue  # Port in use, try another

        raise RuntimeError(f"Could not find free port in range {base_port}-{max_port}")

    else:
        # Not running under xdist or single worker - use traditional approach
        # Just find any free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


@pytest.fixture(autouse=True)
def reset_task_event() -> Generator[None, None, None]:
    """Reset the global task event for each test to ensure isolation."""

    # Reset before test
    tasks_module._task_event = None

    yield

    # Reset after test
    if tasks_module._task_event is not None:
        # Clear any pending notifications
        with contextlib.suppress(Exception):
            tasks_module._task_event.clear()
    tasks_module._task_event = None


# This fixture has been removed - tests should use db_engine directly


@pytest_asyncio.fixture(scope="function")
async def db_engine(
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncEngine, None]:
    """
    A parameterized fixture that provides a database engine.

    The parameter (e.g., 'sqlite' or 'postgres') is injected by the
    `pytest_generate_tests` hook based on the --db command-line option.

    This fixture is designed to replace db_engine once all tests
    are migrated to use explicit fixture dependencies.
    """
    db_backend = request.param

    engine = None
    unique_db_name = None
    admin_url = None
    tmp_name: str | None = None

    if db_backend == "sqlite":
        # Use a temporary on-disk SQLite database to avoid connection scope issues
        with tempfile.NamedTemporaryFile(
            prefix="fa_test_", suffix=".sqlite", delete=False
        ) as tmp_file:
            tmp_name = tmp_file.name
        engine = create_engine_with_sqlite_optimizations(
            f"sqlite+aiosqlite:///{tmp_name}"
        )
        logger.info(f"\n--- SQLite Test DB Setup ({request.node.name}) ---")
        logger.info(f"Created SQLite test engine: {engine.url}")

    elif db_backend == "postgres":
        # Lazily request the session-scoped container fixture
        postgres_container = request.getfixturevalue("postgres_container")

        # Create a unique database name for this test
        test_name_safe = "".join(
            c if c.isalnum() or c == "_" else "_" for c in request.node.name
        )

        # Handle parameterized test names (remove [postgres] suffix)
        if test_name_safe.endswith("_postgres"):
            test_name_safe = test_name_safe[:-9]

        # PostgreSQL has a 63 character limit for identifiers
        max_test_name_length = 49

        if len(test_name_safe) > max_test_name_length:
            name_hash = hashlib.md5(test_name_safe.encode()).hexdigest()[:4]
            test_name_safe = f"{test_name_safe[:45]}{name_hash}"

        unique_db_name = f"test_{test_name_safe}_{uuid.uuid4().hex[:8]}".lower()

        # Get the main connection URL and create an admin engine
        admin_url = postgres_container.get_connection_url().replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
        admin_engine = create_async_engine(
            admin_url, echo=False, isolation_level="AUTOCOMMIT"
        )

        logger.info(f"\n--- PostgreSQL Test DB Setup ({request.node.name}) ---")
        logger.info(f"Creating unique database: {unique_db_name}")

        # Create the unique database
        async with admin_engine.begin() as conn:
            # Check if database exists and drop it if so (cleanup from previous failed run)
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                {"dbname": unique_db_name},
            )
            if result.scalar():
                await conn.execute(text(f'DROP DATABASE "{unique_db_name}"'))

            # Create the new database
            await conn.execute(text(f'CREATE DATABASE "{unique_db_name}"'))

        # Close admin engine
        await admin_engine.dispose()

        # Create engine for the new test database
        # Preserve query parameters (like ?host=/tmp/socket_path for Unix sockets)
        if "?" in admin_url:
            base_url, query_params = admin_url.rsplit("?", 1)
            db_part = base_url.rsplit("/", 1)[0]
            test_db_url = f"{db_part}/{unique_db_name}?{query_params}"
        else:
            test_db_url = admin_url.rsplit("/", 1)[0] + f"/{unique_db_name}"
        engine = create_async_engine(test_db_url, echo=False)
        logger.info(f"Created PostgreSQL test engine for database: {unique_db_name}")

        # Initialize vector extension first for PostgreSQL
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("PostgreSQL vector database components initialized.")

    if not engine:
        raise ValueError(f"Unsupported database backend: {db_backend}")

    # No global engine to patch anymore - engine is passed via dependency injection
    logger.info(f"Using {db_backend} test engine.")

    try:
        # Initialize the database schema using the test engine
        # Pass the engine to init_db for dependency injection
        await init_db(engine)
        logger.info("Database schema initialized.")

        # Yield control to the test function
        yield engine

    finally:
        # Cleanup: dispose the engine
        logger.info(f"--- Test DB Teardown ({request.node.name}) ---")

        # Force close all connections before disposing
        await engine.dispose()

        # For PostgreSQL, ensure all connections are truly closed
        if db_backend == "postgres":
            # PostgreSQL connections may take a moment to fully close after engine.dispose()
            # This sleep helps prevent "database is being accessed by other users" errors
            # when dropping the test database. This is particularly important when using
            # a session-scoped event loop where many tests run in sequence.
            # TODO: Investigate if asyncpg or SQLAlchemy provides a more deterministic way
            # to wait for all connections to be closed.
            await asyncio.sleep(0.1)

        logger.info("Test engine disposed.")
        if db_backend == "sqlite" and tmp_name:
            os.unlink(tmp_name)
            logger.info("Removed temporary SQLite database file.")

        # Drop the PostgreSQL database if we created one
        if db_backend == "postgres" and unique_db_name and admin_url:
            # Recreate admin engine for cleanup
            admin_engine = create_async_engine(
                admin_url, echo=False, isolation_level="AUTOCOMMIT"
            )
            try:
                async with admin_engine.begin() as conn:
                    # Terminate any remaining connections to the test database
                    await conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                        ),
                        {"dbname": unique_db_name},
                    )
                    # Drop the test database
                    await conn.execute(
                        text(f'DROP DATABASE IF EXISTS "{unique_db_name}"')
                    )
                logger.info(f"Dropped test database: {unique_db_name}")
            finally:
                await admin_engine.dispose()


# --- PostgreSQL Test Fixtures (using pgserver) ---


# Protocol for container-like objects
class ContainerProtocol(Protocol):
    def get_connection_url(
        self, host: str | None = None, driver: str | None = None
    ) -> str: ...
    def get_container_host_ip(self) -> str: ...


class PgServerContainer:
    """Wrapper for pgserver that implements ContainerProtocol."""

    def __init__(self, pg_server: Any) -> None:  # noqa: ANN401
        self.pg_server = pg_server
        self.db_name = "postgres"
        self.user = "postgres"
        self.password = ""

    def get_connection_url(
        self, host: str | None = None, driver: str | None = None
    ) -> str:
        # Get the URI from pgserver
        uri = self.pg_server.get_uri()

        # pgserver returns URLs like: postgresql://postgres:@/postgres?host=/tmp/pytest_pgserver_xxx
        # asyncpg needs the host parameter passed directly, not as a query param
        # Parse the URL to extract the socket directory
        if "?host=" in uri:
            # Extract the Unix socket path from the query parameter
            base_uri, query = uri.split("?", 1)
            socket_path = query.replace("host=", "")
            # Construct asyncpg-compatible URL with host as the socket directory
            # asyncpg uses the format: postgresql://user@?host=/socket/path
            return f"postgresql://{self.user}@/{self.db_name}?host={socket_path}"

        # If no socket path, return as-is (shouldn't happen with pgserver)
        return uri

    def get_container_host_ip(self) -> str:
        return "127.0.0.1"


@pytest.fixture(scope="session")
def postgres_container() -> Generator[ContainerProtocol, None, None]:
    """
    Starts and manages a PostgreSQL server for the test session using pgserver.

    Priority order:
    1. If TEST_DATABASE_URL is set, use that external database
    2. Use pgserver (pip-installable embedded PostgreSQL)
    """
    # Check for external PostgreSQL first
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if test_database_url:
        logger.info(
            f"Using external PostgreSQL from TEST_DATABASE_URL: {test_database_url}"
        )

        # Create a mock container that returns the external URL
        class MockContainer:
            def get_connection_url(
                self, host: str | None = None, driver: str | None = None
            ) -> str:
                # Convert asyncpg URL to standard postgresql URL if needed
                return test_database_url.replace(
                    "postgresql+asyncpg://", "postgresql://"
                )

            def get_container_host_ip(self) -> str:
                return "external"

        yield MockContainer()
        logger.info("External PostgreSQL usage completed.")
        return

    # Use pgserver - it's always available as a pip dependency
    if pgserver is None:
        raise RuntimeError(
            "pgserver is not installed and TEST_DATABASE_URL is not set. "
            "Set TEST_DATABASE_URL to use an external PostgreSQL, or install pgserver."
        )

    logger.info("Starting PostgreSQL using pgserver...")
    temp_dir = tempfile.mkdtemp(prefix="pytest_pgserver_")
    pg = None

    try:
        # Start pgserver with cleanup_mode='stop' so we can clean up properly
        pg = pgserver.get_server(temp_dir, cleanup_mode="stop")  # type: ignore[attr-defined]
        logger.info(f"PostgreSQL server started with pgserver at {pg.get_uri()}")

        # Wrap in our protocol-compatible container
        container = PgServerContainer(pg)
        yield container

    finally:
        # Clean up the server
        if pg is not None:
            logger.info("Stopping pgserver PostgreSQL...")
            try:
                pg.cleanup()
                logger.info("pgserver PostgreSQL stopped and cleaned up")
            except Exception as e:
                logger.warning(f"Error during pgserver cleanup: {e}")

        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest_asyncio.fixture(
    scope="function"
)  # Use function scope for engine to ensure isolation
async def pg_vector_db_engine(
    postgres_container: ContainerProtocol,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncEngine, None]:
    """
    PostgreSQL database engine with vector support for tests that require pgvector.
    Always provides PostgreSQL regardless of --postgres flag.
    Creates a unique database for each test to ensure complete isolation.
    """
    # Create a unique database name for this test
    test_name_safe = "".join(
        c if c.isalnum() or c == "_" else "_" for c in request.node.name
    )

    # PostgreSQL has a 63 character limit for identifiers
    # We need: "test_pgvec_" (11) + "_" (1) + uuid (8) = 20 chars overhead
    # This leaves 43 chars for the test name
    max_test_name_length = 43

    if len(test_name_safe) > max_test_name_length:
        # For long names, truncate and add a short hash for uniqueness

        name_hash = hashlib.md5(test_name_safe.encode()).hexdigest()[:4]
        # Keep first 39 chars + 4 char hash = 43 chars total
        test_name_safe = f"{test_name_safe[:39]}{name_hash}"

    unique_db_name = f"test_pgvec_{test_name_safe}_{uuid.uuid4().hex[:8]}".lower()

    # Get the main connection URL and create an admin engine
    admin_url = postgres_container.get_connection_url().replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    admin_engine = create_async_engine(
        admin_url, echo=False, isolation_level="AUTOCOMMIT"
    )

    logger.info("Creating PostgreSQL engine with pgvector support")
    logger.info(f"Creating unique database: {unique_db_name}")

    # Create the unique database
    async with admin_engine.begin() as conn:
        # Check if database exists and drop it if so (cleanup from previous failed run)
        result = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
            {"dbname": unique_db_name},
        )
        if result.scalar():
            await conn.execute(text(f'DROP DATABASE "{unique_db_name}"'))

        # Create the new database
        await conn.execute(text(f'CREATE DATABASE "{unique_db_name}"'))

    # Close admin engine
    await admin_engine.dispose()

    # Create engine for the new test database
    # Preserve query parameters (like ?host=/tmp/socket_path for Unix sockets)
    if "?" in admin_url:
        base_url, query_params = admin_url.rsplit("?", 1)
        db_part = base_url.rsplit("/", 1)[0]
        test_db_url = f"{db_part}/{unique_db_name}?{query_params}"
    else:
        test_db_url = admin_url.rsplit("/", 1)[0] + f"/{unique_db_name}"

    engine = create_engine_with_sqlite_optimizations(test_db_url)

    # No global engine to patch anymore - engine is passed via dependency injection
    logger.info("Using PostgreSQL test engine with vector support.")

    try:
        # Initialize vector extension first for PostgreSQL
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("PostgreSQL vector database components initialized.")

        # Initialize the database schema
        logger.info("Initializing PostgreSQL database schema...")
        await init_db(engine)  # Pass engine for dependency injection
        logger.info("PostgreSQL database schema initialized.")

        yield engine
    finally:
        logger.info(f"--- PostgreSQL Test DB Teardown ({unique_db_name}) ---")
        await engine.dispose()

        # Drop the PostgreSQL database
        admin_engine = create_async_engine(
            admin_url, echo=False, isolation_level="AUTOCOMMIT"
        )
        try:
            async with admin_engine.begin() as conn:
                # Terminate any remaining connections to the test database
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :dbname AND pid <> pg_backend_pid()"
                    ),
                    {"dbname": unique_db_name},
                )
                # Drop the test database
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{unique_db_name}"'))
            logger.info(f"Dropped test database: {unique_db_name}")
        finally:
            await admin_engine.dispose()


# Note: We don't provide a DatabaseContext fixture directly.
# Tests should create their own context using the pg_vector_db_engine fixture:
#
# async def test_something(pg_vector_db_engine):
#     async with DatabaseContext(engine=pg_vector_db_engine) as db:
#         # Use db.fetch_all, db.execute_with_retry, etc.
#         ...


async def cleanup_task_worker(
    worker_task: asyncio.Task,
    shutdown_event: asyncio.Event,
    new_task_event: asyncio.Event | None = None,
    test_name: str = "",
    timeout: float = 5.0,
) -> None:
    """
    Properly clean up a TaskWorker task. Ensures the task is fully stopped
    even with a session-scoped event loop.

    Args:
        worker_task: The asyncio task running the worker
        shutdown_event: The shutdown event to signal
        new_task_event: Optional new task event to signal (to wake worker)
        test_name: Optional test name for logging
        timeout: Maximum time to wait for graceful shutdown
    """
    label = f"TaskWorker-{test_name}" if test_name else "TaskWorker"

    # Signal shutdown
    shutdown_event.set()
    if new_task_event:
        new_task_event.set()  # Wake up worker if waiting

    # Wait for graceful shutdown with timeout
    try:
        await asyncio.wait_for(worker_task, timeout=timeout)
        logger.info(f"{label} stopped gracefully")
    except asyncio.TimeoutError:
        logger.warning(f"Timeout stopping {label}. Cancelling.")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info(f"{label} cancellation confirmed")
    except Exception as e:
        logger.error(f"Error stopping {label}: {e}", exc_info=True)
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    # Give a moment for database connections to fully close
    # This is crucial when using PostgreSQL to avoid "database is being accessed by other users" errors
    await asyncio.sleep(0.5)

    # Force all pending tasks to complete
    pending = [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task != asyncio.current_task()
    ]
    if pending:
        logger.warning(f"Found {len(pending)} pending tasks after {label} cleanup")
        # Give them a moment to complete
        await asyncio.sleep(0.1)


@pytest.fixture(scope="function")
def mock_clock() -> MockClock:
    """Provides a mock clock for controlling time in tests."""
    return MockClock()


@pytest_asyncio.fixture(scope="function")
async def task_worker_manager(
    request: pytest.FixtureRequest, db_engine: AsyncEngine, mock_clock: MockClock
) -> AsyncGenerator[
    Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]], None
]:
    """
    Manages the lifecycle of a TaskWorker instance via a factory.

    Yields a factory function that creates and starts a TaskWorker.
    The factory is responsible for creating the worker with the correct
    dependencies, and the fixture ensures it is properly shut down.
    """
    worker_task_handle = None
    shutdown_event = asyncio.Event()
    new_task_event_for_worker = asyncio.Event()

    def worker_factory(
        processing_service: ProcessingService,
        chat_interface: MagicMock,
        **kwargs: Any,  # noqa: ANN401
    ) -> tuple[TaskWorker, asyncio.Event, asyncio.Event]:
        nonlocal worker_task_handle
        # Extract timezone_str from kwargs with default of "UTC"
        timezone_str = kwargs.pop("timezone_str", "UTC")
        worker = TaskWorker(
            processing_service=processing_service,
            chat_interface=chat_interface,
            calendar_config={},
            timezone_str=timezone_str,
            embedding_generator=MagicMock(),
            shutdown_event_instance=shutdown_event,
            engine=db_engine,
            clock=mock_clock,  # Use the mock_clock fixture
            **kwargs,
        )
        worker_task_handle = asyncio.create_task(worker.run(new_task_event_for_worker))
        logger.info("Started background TaskWorker (factory).")
        return worker, new_task_event_for_worker, shutdown_event

    try:
        yield worker_factory
    finally:
        if worker_task_handle:
            await cleanup_task_worker(
                worker_task_handle,
                shutdown_event,
                new_task_event_for_worker,
                test_name=request.node.name,
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
    db_engine: AsyncEngine,  # Use the unified test engine
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

        assert new_calendar_obj is not None, (
            "make_calendar did not return a calendar object."
        )
        assert new_calendar_obj.url is not None, "Created calendar has no URL."
        unique_calendar_url = str(new_calendar_obj.url)  # Ensure it's a string
        logger.info(
            f"Successfully created unique calendar '{unique_calendar_name}' with URL: {unique_calendar_url}"
        )

        # Verification: Check if the calendar is listable or has events (should be 0)
        # This also implicitly checks if the calendar exists on the server.
        events = await asyncio.to_thread(new_calendar_obj.events)
        assert len(events) == 0, (
            f"Newly created calendar '{unique_calendar_name}' should have 0 events, found {len(events)}."
        )
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


# --- VCR.py Configuration for LLM Integration Tests ---


@pytest.fixture(scope="module")
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def vcr_config() -> dict[str, Any]:
    """Configure VCR for recording and replaying HTTP interactions."""
    # Only use "once" mode if explicitly requested via environment variable
    # Default to "none" mode which will only use existing cassettes
    record_mode = os.getenv("VCR_RECORD_MODE", "none")

    return {
        # Filter sensitive headers
        "filter_headers": [
            "authorization",
            "x-api-key",
            "api-key",
            "x-goog-api-key",
            "openai-api-key",
        ],
        # Filter sensitive query parameters
        "filter_query_parameters": ["api_key", "key"],
        # Default to "none" mode - only replay existing cassettes, don't record
        "record_mode": record_mode,
        # Match requests on these attributes
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
        # Store cassettes in organized directory structure
        "cassette_library_dir": "tests/cassettes/llm",
        # Allow cassettes to be replayed multiple times
        "allow_playback_repeats": True,
        # Don't record on exceptions (avoid recording failed requests)
        "record_on_exception": False,
    }


@pytest.fixture(scope="module")
def vcr_cassette_dir(request: pytest.FixtureRequest) -> str:
    """Return the cassette directory for the current test module."""
    test_dir = pathlib.Path(request.node.fspath).parent
    return str(test_dir / "cassettes")


# --- VCR Bypass Mechanism ---
@pytest.fixture(autouse=True)
def vcr_bypass_for_streaming(request: pytest.FixtureRequest) -> None:
    """
    Automatically disable VCR for tests marked with 'no_vcr'.

    This fixture addresses VCR.py issue #927 where MockStream doesn't implement
    the readany() method required by aiohttp 3.12+ for streaming responses.

    Usage:
    ------
    @pytest.mark.no_vcr
    @pytest.mark.llm_integration
    async def test_streaming_functionality():
        # This test will make real API calls, bypassing VCR.py
        pass

    Benefits:
    ---------
    - Allows streaming tests to work with aiohttp 3.12+
    - Preserves VCR.py benefits for non-streaming tests
    - Clean separation between recorded and live tests
    - Automatic test skipping in CI without API keys
    """
    # Check if the test is marked with 'no_vcr'
    if request.node.get_closest_marker("no_vcr"):
        # Import here to avoid circular imports

        # Monkey patch VCR to be a no-op for this test
        original_use_cassette = vcr.VCR.use_cassette

        def disabled_use_cassette(
            self: Any,  # noqa: ANN401  # VCR patching requires Any type
            path: str | None = None,
            **kwargs: Any,  # noqa: ANN401
        ) -> Any:  # noqa: ANN401
            """Return a no-op context manager that doesn't record or replay."""

            return nullcontext()

        # Apply the patch for the duration of this test
        vcr.VCR.use_cassette = disabled_use_cassette

        # Restore original behavior after test
        def restore() -> None:
            vcr.VCR.use_cassette = original_use_cassette

        request.addfinalizer(restore)


@pytest.fixture(scope="session")
def built_frontend() -> Generator[None, None, None]:
    """
    Ensures the frontend is built before tests that require it.
    This fixture runs npm install and npm run build if needed.
    """
    frontend_dir = pathlib.Path(__file__).parent.parent / "frontend"
    dist_dir = (
        pathlib.Path(__file__).parent.parent
        / "src"
        / "family_assistant"
        / "static"
        / "dist"
    )

    # Check if we need to build
    needs_install = not (frontend_dir / "node_modules").exists()
    needs_build = not (dist_dir / "chat.html").exists()

    if needs_install or needs_build:
        logger.info("Building frontend for tests...")

        # Run npm install if needed
        if needs_install:
            logger.info("Running npm install...")
            try:
                subprocess.run(
                    ["npm", "install"],
                    cwd=frontend_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as err:
                pytest.fail(f"npm install failed: {err.stderr}")

        # Run npm run build
        if needs_build:
            logger.info("Running npm run build...")
            try:
                subprocess.run(
                    ["npm", "run", "build"],
                    cwd=frontend_dir,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as err:
                pytest.fail(f"npm run build failed: {err.stderr}")

            logger.info(
                f"Frontend built successfully. Files in dist: {list(dist_dir.glob('*.html'))}"
            )
    else:
        logger.info("Frontend already built, skipping build step")

    yield

    # No cleanup needed - we want to keep the built files


@pytest.fixture(scope="session", autouse=True)
def setup_fastapi_test_config() -> Generator[None, None, None]:
    """
    Sets up a default test configuration for the FastAPI app.
    This ensures all tests have a valid, writable document_storage_path.
    """

    # Store original config to restore later
    original_config = getattr(fastapi_app.state, "config", None)

    # Create temporary directories for the test session
    with (
        tempfile.TemporaryDirectory(prefix="fa_test_docs_") as doc_dir,
        tempfile.TemporaryDirectory(prefix="fa_test_attach_") as attach_dir,
    ):
        # Create subdirectory for mailbox
        mailbox_dir = pathlib.Path(attach_dir) / "raw_mailbox_dumps"
        mailbox_dir.mkdir(exist_ok=True)

        # Create a test config with writable paths
        test_config = {
            "document_storage_path": doc_dir,
            "attachment_storage_path": attach_dir,
            "mailbox_raw_dir": str(mailbox_dir),  # Convert back to string for config
            "auth_enabled": False,  # Disable auth for tests
            "dev_mode": False,  # Use production mode for tests
        }

        # If there's an existing config, preserve other values
        if original_config:
            test_config = {**original_config, **test_config}

        # Set the test config
        fastapi_app.state.config = test_config

        # Also ensure docs_user_dir is properly set for documentation API tests
        docs_user_dir = pathlib.Path(__file__).parent.parent / "docs" / "user"
        if docs_user_dir.exists():
            fastapi_app.state.docs_user_dir = docs_user_dir
            logger.info(f"Set docs_user_dir for tests: {docs_user_dir}")

        logger.info(f"Set up global test config for FastAPI app: {test_config}")

        yield

        # Restore original config
        if original_config is not None:
            fastapi_app.state.config = original_config
        elif hasattr(fastapi_app.state, "config"):
            # Remove config if it didn't exist before
            del fastapi_app.state.config
