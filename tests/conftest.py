import asyncio
import contextlib
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
from typing import Any, Protocol
from unittest.mock import MagicMock, patch

import caldav
import pytest
import pytest_asyncio  # Import the correct decorator
from caldav.lib import error as caldav_error  # Import the error module
from docker.errors import DockerException  # Import DockerException directly
from passlib.hash import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

# Import for task_worker_manager fixture
from family_assistant.processing import ProcessingService  # Import ProcessingService
from family_assistant.storage import base as storage_base  # Import storage base module

# Import the metadata and the original engine object from your storage base
from family_assistant.storage import init_db  # Import init_db
from family_assistant.storage.context import DatabaseContext

# Explicitly import the module defining the tasks table to ensure metadata registration
# Import vector storage init and context
from family_assistant.storage.vector import init_vector_db  # Corrected import path
from family_assistant.task_worker import TaskWorker

# Configure logging for tests (optional, but can be helpful)
logging.basicConfig(level=logging.INFO)

# Disable SQLAlchemy error handler for tests to avoid connection issues
# when the database is disposed
os.environ["FAMILY_ASSISTANT_DISABLE_DB_ERROR_LOGGING"] = "1"

logger = logging.getLogger(__name__)


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
        if db_option in ("sqlite", "all"):
            db_backends.append("sqlite")
        if db_option in ("postgres", "all"):
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
        else:
            # Check if test has postgres marker
            if metafunc.definition.get_closest_marker("postgres"):
                # Postgres-only tests
                if "postgres" in db_backends:
                    metafunc.parametrize("db_engine", ["postgres"], indirect=True)
                else:
                    # Skip this test if postgres is not in the selected backends
                    metafunc.parametrize("db_engine", [], indirect=True)
            else:
                # Regular tests get all selected backends
                metafunc.parametrize("db_engine", db_backends, indirect=True)


def find_free_port() -> int:
    """Finds a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def reset_task_event() -> Generator[None, None, None]:
    """Reset the global task event for each test to ensure isolation."""
    import family_assistant.storage.tasks as tasks_module

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

    if db_backend == "sqlite":
        # Use an in-memory SQLite database for each test
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
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
            import hashlib

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
        test_db_url = admin_url.rsplit("/", 1)[0] + f"/{unique_db_name}"
        engine = create_async_engine(test_db_url, echo=False)
        logger.info(f"Created PostgreSQL test engine for database: {unique_db_name}")

        # Initialize vector extension first for PostgreSQL
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("PostgreSQL vector database components initialized.")

    if not engine:
        raise ValueError(f"Unsupported database backend: {db_backend}")

    # Patch the global engine used by storage modules
    patcher = patch("family_assistant.storage.base.engine", engine)
    patcher.start()
    logger.info(f"Patched storage.base.engine with {db_backend} test engine.")

    try:
        # Initialize the database schema using the patched engine
        await init_db()
        logger.info("Database schema initialized.")

        # Yield control to the test function
        yield engine

    finally:
        # Cleanup: Stop the patch and dispose the engine
        patcher.stop()
        logger.info(f"--- Test DB Teardown ({request.node.name}) ---")
        await engine.dispose()
        logger.info("Test engine disposed.")
        logger.info("Restored original storage.base.engine.")

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


# --- PostgreSQL Test Fixtures (using testcontainers) ---


# Protocol for container-like objects
class ContainerProtocol(Protocol):
    def get_connection_url(
        self, host: str | None = None, driver: str | None = None
    ) -> str: ...
    def get_container_host_ip(self) -> str: ...


def check_container_runtime() -> bool:
    """Check if Docker or Podman is available."""
    for cmd in ["docker", "podman"]:
        try:
            result = subprocess.run(
                [cmd, "version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                logger.info(f"Container runtime '{cmd}' is available")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return False


def check_postgres_available() -> tuple[bool, str | None]:
    """Check if PostgreSQL is available locally and return path to pg_ctl."""
    # Try common PostgreSQL 17 paths
    pg_ctl_paths = [
        "/usr/lib/postgresql/17/bin/pg_ctl",  # Debian/Ubuntu
        "/usr/pgsql-17/bin/pg_ctl",  # RHEL/CentOS
        "/usr/local/pgsql/bin/pg_ctl",  # Source install
        "pg_ctl",  # In PATH
    ]

    for pg_ctl in pg_ctl_paths:
        try:
            result = subprocess.run(
                [pg_ctl, "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and "17" in result.stdout:
                logger.info(f"Found PostgreSQL 17 at: {pg_ctl}")
                return True, pg_ctl
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return False, None


class SubprocessPostgresContainer:
    """Mock container that manages a subprocess PostgreSQL instance."""

    def __init__(self, port: int, data_dir: str, pg_ctl: str) -> None:
        self.port = port
        self.data_dir = data_dir
        self.pg_ctl = pg_ctl
        self.process = None
        self.db_name = "test"
        self.user = "test"
        self.password = "test"

    def get_connection_url(
        self, host: str | None = None, driver: str | None = None
    ) -> str:
        # Parameters kept for compatibility with ContainerProtocol
        return f"postgresql://{self.user}:{self.password}@127.0.0.1:{self.port}/{self.db_name}"

    def get_container_host_ip(self) -> str:
        return "127.0.0.1"

    def start(self) -> None:
        """Initialize and start PostgreSQL."""
        # Initialize the database cluster
        initdb_path = self.pg_ctl.replace("pg_ctl", "initdb")
        logger.info(f"Initializing PostgreSQL database in {self.data_dir}")

        result = subprocess.run(
            [
                initdb_path,
                "-D",
                self.data_dir,
                "-U",
                self.user,
                "--auth-local=trust",
                "--auth-host=md5",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to initialize PostgreSQL: {result.stderr}")

        # Update postgresql.conf to listen on the specific port
        conf_path = os.path.join(self.data_dir, "postgresql.conf")
        with open(conf_path, "a") as f:
            f.write(f"\nport = {self.port}\n")
            f.write("shared_preload_libraries = 'vector'\n")

        # Update pg_hba.conf for password authentication
        hba_path = os.path.join(self.data_dir, "pg_hba.conf")
        with open(hba_path, "w") as f:
            f.write(
                "local   all             all                                     trust\n"
            )
            f.write(
                "host    all             all             127.0.0.1/32            md5\n"
            )
            f.write(
                "host    all             all             ::1/128                 md5\n"
            )

        # Start PostgreSQL
        logger.info(f"Starting PostgreSQL on port {self.port}")
        result = subprocess.run(
            [
                self.pg_ctl,
                "start",
                "-D",
                self.data_dir,
                "-l",
                os.path.join(self.data_dir, "logfile"),
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start PostgreSQL: {result.stderr}")

        # Set up the test database and user
        psql_path = self.pg_ctl.replace("pg_ctl", "psql")

        # Create user with password
        result = subprocess.run(
            [
                psql_path,
                "-U",
                self.user,
                "-p",
                str(self.port),
                "-c",
                f"ALTER USER {self.user} PASSWORD '{self.password}';",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to set user password: {result.stderr}")

        # Create test database
        result = subprocess.run(
            [
                psql_path,
                "-U",
                self.user,
                "-p",
                str(self.port),
                "-c",
                f"CREATE DATABASE {self.db_name};",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create database: {result.stderr}")

        # Create pgvector extension
        result = subprocess.run(
            [
                psql_path,
                "-U",
                self.user,
                "-p",
                str(self.port),
                "-d",
                self.db_name,
                "-c",
                "CREATE EXTENSION IF NOT EXISTS vector;",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create pgvector extension: {result.stderr}")

        logger.info("PostgreSQL started and configured successfully")

    def stop(self) -> None:
        """Stop PostgreSQL and clean up."""
        if self.pg_ctl and os.path.exists(self.data_dir):
            logger.info("Stopping PostgreSQL subprocess...")
            subprocess.run(
                [self.pg_ctl, "stop", "-D", self.data_dir, "-m", "fast"],
                capture_output=True,
            )

            # Clean up the data directory
            shutil.rmtree(self.data_dir, ignore_errors=True)
            logger.info("PostgreSQL subprocess stopped and cleaned up")


@pytest.fixture(scope="session")
def postgres_container() -> Generator[ContainerProtocol, None, None]:
    """
    Starts and manages a PostgreSQL container for the test session.

    Priority order:
    1. If TEST_DATABASE_URL is set, use that external database
    2. If Docker/Podman is available, use testcontainers
    3. If PostgreSQL 17 is installed locally, use subprocess
    4. Otherwise, fail with helpful error message
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

    # Check if Docker/Podman is available
    if check_container_runtime():
        # Original testcontainer logic
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
            return
        except (DockerException, Exception) as e:
            logger.warning(
                f"Failed to start PostgreSQL container: {e}. "
                f"Will try subprocess PostgreSQL if available."
            )

    # Check if PostgreSQL is available locally
    postgres_available, pg_ctl = check_postgres_available()
    if postgres_available and pg_ctl:
        logger.info("Docker/Podman not available, using subprocess PostgreSQL")

        # Create temporary directory for PostgreSQL data
        temp_dir = tempfile.mkdtemp(prefix="pytest_postgres_")
        port = find_free_port()

        container = SubprocessPostgresContainer(port, temp_dir, pg_ctl)
        try:
            container.start()
            yield container
        finally:
            container.stop()
        return

    # No PostgreSQL available
    pytest.fail(
        "PostgreSQL tests require either:\n"
        "1. Docker or Podman to be installed and running\n"
        "2. PostgreSQL 17 with pgvector to be installed locally\n"
        "3. TEST_DATABASE_URL environment variable pointing to an existing database\n"
        "\nTo install PostgreSQL locally on Ubuntu/Debian:\n"
        "  sudo apt-get install postgresql-17 postgresql-17-pgvector\n"
        "\nTo skip PostgreSQL tests, use: pytest --db sqlite",
        pytrace=False,
    )


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
        import hashlib

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
    test_db_url = admin_url.rsplit("/", 1)[0] + f"/{unique_db_name}"

    from family_assistant.storage.base import create_engine_with_sqlite_optimizations

    engine = create_engine_with_sqlite_optimizations(test_db_url)

    # Patch the global engine
    original_engine = storage_base.engine
    storage_base.engine = engine
    logger.info("Patched storage.base.engine with PostgreSQL test engine.")

    try:
        # Initialize vector extension first for PostgreSQL
        async with DatabaseContext(engine=engine) as db_context:
            await init_vector_db(db_context)
        logger.info("PostgreSQL vector database components initialized.")

        # Initialize the database schema
        logger.info("Initializing PostgreSQL database schema...")
        await init_db()  # init_db uses the global engine
        logger.info("PostgreSQL database schema initialized.")

        yield engine
    finally:
        logger.info(f"--- PostgreSQL Test DB Teardown ({unique_db_name}) ---")
        await engine.dispose()
        storage_base.engine = original_engine
        logger.info("Restored original storage.base.engine.")

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
def vcr_config() -> dict[str, Any]:
    """Configure VCR for recording and replaying HTTP interactions."""
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
        # Default to "once" mode - record if cassette doesn't exist
        "record_mode": os.getenv("VCR_RECORD_MODE", "once"),
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
