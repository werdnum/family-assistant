"""Simplified test fixtures for Playwright-based web UI integration tests."""

import asyncio
import contextlib
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from playwright.async_api import Page, async_playwright
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from family_assistant.assistant import Assistant
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.llm import LLMInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import (
    AttachmentRegistry,
)
from family_assistant.storage import init_db
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.app_creator import configure_app_debug
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class WebTestFixture(NamedTuple):
    """Container for web test dependencies."""

    assistant: Assistant
    page: Page
    base_url: str


async def wait_for_server(url: str, timeout: int = 60) -> None:
    """Wait for a server to become available by polling health endpoint."""
    start_time = time.time()
    health_url = f"{url}/health"  # Changed from /api/health to /health
    last_error = None

    print(f"Waiting for server at {url} (health check: {health_url})")

    async with httpx.AsyncClient() as client:
        while time.time() - start_time < timeout:
            elapsed = time.time() - start_time
            try:
                # First check if the health endpoint is responding
                response = await client.get(health_url, timeout=5)
                print(f"[{elapsed:.1f}s] Health check response: {response.status_code}")

                if response.status_code == 200:
                    print(f"[{elapsed:.1f}s] ✓ Health check passed for {health_url}")
                    # Also check the root endpoint
                    root_response = await client.get(url, timeout=5)
                    print(
                        f"[{elapsed:.1f}s] Root endpoint response: {root_response.status_code}"
                    )
                    return
                else:
                    # Non-200 response
                    print(
                        f"[{elapsed:.1f}s] Health check returned {response.status_code}: {response.text[:200]}"
                    )

            except httpx.ConnectError as e:
                # Can't connect yet
                if str(e) != str(last_error):
                    print(f"[{elapsed:.1f}s] Cannot connect to {health_url}: {e}")
                    last_error = str(e)
            except httpx.ReadTimeout:
                print(f"[{elapsed:.1f}s] Read timeout from {health_url}")
            except Exception as e:
                print(f"[{elapsed:.1f}s] Unexpected error: {type(e).__name__}: {e}")

            await asyncio.sleep(1)
    raise TimeoutError(
        f"Server at {url} did not become available within {timeout} seconds"
    )


# Port allocation now handled by worker-specific ranges - no global tracking needed


def find_free_port_with_socket() -> tuple[int, socket.socket]:
    """Find a free port and return both port and socket to prevent race conditions.

    Returns:
        tuple[int, socket.socket]: Port number and bound socket. Caller must close the socket.
    """
    # Simple approach: let the OS choose a free port and keep the socket bound
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))  # Let OS choose a free port
    port = s.getsockname()[1]
    return port, s


def find_free_port() -> int:
    """Find a free port (legacy function for backward compatibility).

    Warning: This has a race condition. Use find_free_port_with_socket() instead.
    """
    port, sock = find_free_port_with_socket()
    sock.close()
    return port


@pytest.fixture(scope="function")
def vite_and_api_ports() -> tuple[int, int]:
    """Get random free ports for both Vite and API servers."""
    # For the simplified approach, we only need the API port
    api_port = find_free_port()
    return 0, api_port  # Vite port is 0 since we're not using it


@pytest.fixture(scope="function")
def api_socket_and_port() -> Generator[tuple[int, socket.socket], None, None]:
    """Get a bound socket and port for the API server to prevent race conditions."""
    port, sock = find_free_port_with_socket()
    try:
        yield port, sock
    finally:
        sock.close()


@pytest.fixture(scope="session", autouse=True)
def build_frontend_assets() -> None:
    """Build frontend assets before running web tests."""
    frontend_dir = Path(__file__).parent.parent.parent.parent / "frontend"
    dist_dir = frontend_dir.parent / "src" / "family_assistant" / "static" / "dist"

    def log_dist_state(prefix: str) -> None:
        """Helper to log dist directory state."""
        print(f"\n=== {prefix}: Dist Directory State ===")
        print(f"Path: {dist_dir}")
        print(f"Exists: {dist_dir.exists()}")
        if dist_dir.exists():
            files = list(dist_dir.iterdir())
            print(f"File count: {len(files)}")
            # Check critical files
            router_html = dist_dir / "router.html"
            manifest = dist_dir / ".vite" / "manifest.json"
            print(f"router.html: {'EXISTS' if router_html.exists() else 'MISSING'}")
            print(f"manifest.json: {'EXISTS' if manifest.exists() else 'MISSING'}")
            if not router_html.exists() or not manifest.exists():
                # List what IS there if critical files are missing
                print("Available files:")
                for f in sorted(files)[:10]:
                    print(f"  - {f.name}")

    # Log initial state
    log_dist_state("FIXTURE START")

    # Skip building in CI - assets are pre-built and copied in the CI workflow
    if os.getenv("CI") == "true":
        print("\n=== Skipping frontend build in CI (using pre-built assets) ===")
        log_dist_state("CI CHECK")
        return

    print("\n=== Building frontend assets ===")
    print(f"Frontend directory: {frontend_dir}")

    # Check if npm is available
    try:
        result = subprocess.run(
            ["npm", "--version"], capture_output=True, text=True, check=True
        )
        print(f"npm version: {result.stdout.strip()}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.fail("npm is not available - required for building frontend assets")

    # Check if node_modules exists
    if not (frontend_dir / "node_modules").exists():
        print("node_modules not found. Running npm install...")
        # Don't capture output to avoid hanging on large output
        result = subprocess.run(
            ["npm", "install"],
            cwd=str(frontend_dir),
            check=True,
        )
        if result.returncode != 0:
            pytest.fail("npm install failed")

    # Check if we need to rebuild (comprehensive timestamp check)
    need_rebuild = True
    if dist_dir.exists() and any(dist_dir.iterdir()):
        # Check if source files are newer than build
        # Include all relevant file types that could affect the build
        src_files = (
            list(frontend_dir.glob("src/**/*.js"))
            + list(frontend_dir.glob("src/**/*.jsx"))
            + list(frontend_dir.glob("src/**/*.ts"))
            + list(frontend_dir.glob("src/**/*.tsx"))
            + list(frontend_dir.glob("src/**/*.css"))
            + list(frontend_dir.glob("*.json"))  # package.json, tsconfig.json, etc.
            + list(frontend_dir.glob("*.config.js"))  # vite.config.js, etc.
            + list(frontend_dir.glob("*.html"))  # HTML entry points
        )
        # Also check the vite_pages.py file since route changes affect the build
        vite_pages_file = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "family_assistant"
            / "web"
            / "routers"
            / "vite_pages.py"
        )
        if vite_pages_file.exists():
            src_files.append(vite_pages_file)

        if src_files:
            newest_src = max(f.stat().st_mtime for f in src_files if f.exists())
            oldest_dist = min(f.stat().st_mtime for f in dist_dir.iterdir())
            if oldest_dist > newest_src:
                print("Frontend assets are up to date, skipping build")
                need_rebuild = False
            else:
                print("Source files have been modified, rebuilding assets")
                print(f"  Newest source: {newest_src}")
                print(f"  Oldest dist: {oldest_dist}")

    if need_rebuild:
        print("Building frontend assets...")
        # Don't capture output to avoid hanging on large output
        result = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(frontend_dir),
            check=True,
        )

        if result.returncode != 0:
            pytest.fail("Failed to build frontend assets")

        print("✓ Frontend assets built successfully")

    # Verify dist directory exists
    if not dist_dir.exists() or not any(dist_dir.iterdir()):
        pytest.fail(f"No built assets found in {dist_dir}")


# ==================================================================================
# Session-Scoped Fixtures for Read-Only Tests (Performance Optimization)
# ==================================================================================
# These fixtures enable sharing a single Assistant instance across multiple tests
# to reduce setup time from ~9s per test to ~0.01s per test.
# Only use for read-only tests that don't modify persistent state.
# ==================================================================================


async def _create_web_assistant(
    db_engine: AsyncEngine,
    api_socket_and_port: tuple[int, socket.socket],
    mock_llm_client: RuleBasedMockLLMClient,
    scope_label: str = "",
) -> AsyncGenerator[Assistant, None]:
    """Helper function to create a web-only Assistant instance.

    Args:
        db_engine: Database engine to use
        api_socket_and_port: Tuple of (port, socket) for the API server
        mock_llm_client: Mock LLM client instance
        scope_label: Label for logging (e.g., "SESSION" or "")
    """
    api_port, api_socket = api_socket_and_port
    log_prefix = f"{scope_label} " if scope_label else ""
    print(f"\n=== Starting {log_prefix}API server on port {api_port} ===")

    # Create minimal test configuration
    storage_suffix = f"_{scope_label.lower()}" if scope_label else ""
    test_config: dict[str, Any] = {
        "telegram_enabled": False,
        "telegram_token": None,
        "allowed_user_ids": [],
        "developer_chat_id": None,
        "model": "mock-model-for-testing",
        "embedding_model": "mock-deterministic-embedder",
        "embedding_dimensions": 10,
        "database_url": str(db_engine.url),
        "server_url": f"http://localhost:{api_port}",
        "server_port": api_port,
        "document_storage_path": f"/tmp/test_docs{storage_suffix}",
        "attachment_storage_path": f"/tmp/test_attachments{storage_suffix}",
        "litellm_debug": False,
        "dev_mode": False,
        "oidc": {
            "client_id": "",
            "client_secret": "",
            "discovery_url": "",
        },
        "default_service_profile_id": "default_assistant",
        "service_profiles": [
            {
                "id": "default_assistant",
                "description": "Test profile for web UI",
                "processing_config": {
                    "prompts": {"system_prompt": "You are a helpful test assistant."},
                    "calendar_config": {},
                    "timezone": "UTC",
                    "max_history_messages": 5,
                    "history_max_age_hours": 1,
                    "llm_model": "mock-model-for-testing",
                    "delegation_security_level": "none",
                },
                "tools_config": {
                    "enable_local_tools": [
                        "add_or_update_note",
                        "search_documents",
                        "delete_calendar_event",
                        "attach_to_response",
                    ],
                    "confirm_tools": ["delete_calendar_event", "add_or_update_note"],
                    "confirmation_timeout_seconds": 10.0,
                    "mcp_initialization_timeout_seconds": 5,
                },
                "chat_id_to_name_map": {},
                "slash_commands": [],
            },
            {
                "id": "test_browser",
                "description": "Test browser profile for web UI",
                "processing_config": {
                    "prompts": {"system_prompt": "You are a test browser assistant."},
                    "calendar_config": {},
                    "timezone": "UTC",
                    "max_history_messages": 5,
                    "history_max_age_hours": 1,
                    "llm_model": "mock-browser-model-for-testing",
                    "delegation_security_level": "none",
                },
                "tools_config": {
                    "enable_local_tools": ["search_documents"],
                    "confirm_tools": [],
                    "confirmation_timeout_seconds": 10.0,
                    "mcp_initialization_timeout_seconds": 5,
                },
                "chat_id_to_name_map": {},
                "slash_commands": ["/browse"],
            },
            {
                "id": "test_research",
                "description": "Test research profile for web UI",
                "processing_config": {
                    "prompts": {"system_prompt": "You are a test research assistant."},
                    "calendar_config": {},
                    "timezone": "UTC",
                    "max_history_messages": 5,
                    "history_max_age_hours": 1,
                    "llm_model": "mock-research-model-for-testing",
                    "delegation_security_level": "none",
                },
                "tools_config": {
                    "enable_local_tools": [],
                    "confirm_tools": [],
                    "confirmation_timeout_seconds": 10.0,
                    "mcp_initialization_timeout_seconds": 5,
                },
                "chat_id_to_name_map": {},
                "slash_commands": ["/research"],
            },
        ],
        "mcp_config": {"mcpServers": {}},
        "indexing_pipeline_config": {"processors": []},
        "event_system": {"enabled": False},
    }

    # Create Assistant instance
    assistant = Assistant(
        config=test_config,
        llm_client_overrides={
            "default_assistant": mock_llm_client,
            "test_browser": mock_llm_client,
            "test_research": mock_llm_client,
        },
        database_engine=db_engine,
        server_socket=api_socket,
    )

    configure_app_debug(debug=True)

    # Set up dependencies
    print(f"Setting up {log_prefix}dependencies...")
    await assistant.setup_dependencies()
    print(f"{log_prefix}dependencies set up")

    # Set SERVER_URL env var
    os.environ["SERVER_URL"] = f"http://localhost:{api_port}"

    # Start services
    print(f"Starting {log_prefix}API services on port {api_port}...")
    start_task = asyncio.create_task(assistant.start_services())

    # Wait for server
    await asyncio.sleep(2)
    print(f"Waiting for {log_prefix}server...")

    try:
        await wait_for_server(f"http://localhost:{api_port}", timeout=120)
        print(f"\n=== {log_prefix}API server ready on port {api_port} ===")
    except Exception as e:
        print(f"\n=== Failed to start {log_prefix}API server: {e} ===")
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
        raise

    yield assistant

    # Cleanup
    shutdown_label = f"{log_prefix}TEST_END" if scope_label else "TEST"
    print(f"\n=== Shutting down {log_prefix}API server ===")
    assistant.initiate_shutdown(shutdown_label)

    if hasattr(assistant, "task_worker_instance") and assistant.task_worker_instance:
        assistant.task_worker_instance.shutdown_event.set()

    try:
        await asyncio.wait_for(start_task, timeout=5.0)
    except asyncio.TimeoutError:
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task


@pytest_asyncio.fixture(scope="session")
async def session_db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Create a session-scoped SQLite database for read-only tests.

    Uses a file-based database (not in-memory) to work correctly with
    pytest-xdist parallel testing. Each worker gets its own database file.
    """
    # Get worker ID from environment (for pytest-xdist)
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")

    # Use file-based database for pytest-xdist compatibility
    # Each worker gets its own database file
    with tempfile.NamedTemporaryFile(
        suffix=f"_playwright_session_{worker_id}.db", delete=False
    ) as db_file:
        db_path = db_file.name

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )

    # Initialize schema
    await init_db(engine)

    yield engine

    await engine.dispose()

    # Clean up database file
    with contextlib.suppress(OSError):
        os.unlink(db_path)


@pytest.fixture(scope="session")
def session_api_socket_and_port() -> Generator[tuple[int, socket.socket], None, None]:
    """Create a session-scoped socket and port for the API server."""
    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    api_socket.bind(("", 0))  # Bind to any available port
    api_socket.listen(1)
    api_port = api_socket.getsockname()[1]

    print(f"\n=== Session API socket bound to port {api_port} ===")

    yield (api_port, api_socket)

    api_socket.close()


@pytest.fixture(scope="session")
def session_mock_llm_client() -> RuleBasedMockLLMClient:
    """Create a session-scoped mock LLM client for read-only tests."""
    return RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(
            content="Test response from session-scoped mock LLM"
        ),
    )


@pytest_asyncio.fixture(scope="session")
async def web_readonly_assistant(
    session_db_engine: AsyncEngine,
    session_api_socket_and_port: tuple[int, socket.socket],
    build_frontend_assets: None,  # Ensure assets are built
    session_mock_llm_client: RuleBasedMockLLMClient,
) -> AsyncGenerator[Assistant, None]:
    """Session-scoped Assistant for read-only tests.

    This fixture creates a single Assistant instance shared across all read-only tests.
    The shared instance includes a shared database, which means:
    - Tests can view data created by previous tests
    - Tests MUST NOT assume empty database state
    - Tests MUST filter results by unique IDs (e.g., UUIDs)
    - Tests MUST NOT make strict count assertions (use >= not ==)

    Use this fixture for tests that only:
    - Navigate pages
    - View UI elements
    - Check element visibility/styling
    - Perform read-only operations

    For tests that create/modify data, use web_test_fixture (function-scoped) instead.
    """
    async for assistant in _create_web_assistant(
        session_db_engine,
        session_api_socket_and_port,
        session_mock_llm_client,
        scope_label="SESSION",
    ):
        yield assistant


@pytest.fixture(scope="function")
def mock_llm_client() -> RuleBasedMockLLMClient:
    """Create a mock LLM client that tests can configure."""
    return RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(content="Test response from mock LLM"),
    )


@pytest_asyncio.fixture(scope="function")
async def web_only_assistant(
    db_engine: AsyncEngine,
    api_socket_and_port: tuple[int, socket.socket],
    build_frontend_assets: None,  # Ensure assets are built
    mock_llm_client: RuleBasedMockLLMClient,
) -> AsyncGenerator[Assistant, None]:
    """Start Assistant in web-only mode for testing."""
    async for assistant in _create_web_assistant(
        db_engine,
        api_socket_and_port,
        mock_llm_client,
    ):
        yield assistant


@pytest_asyncio.fixture(scope="function")
async def web_test_fixture(
    page: Page,
    web_only_assistant: Assistant,
    api_socket_and_port: tuple[int, socket.socket],
    build_frontend_assets: None,  # Ensure frontend is built before tests
) -> AsyncGenerator[WebTestFixture, None]:
    """Combined fixture providing all web test dependencies."""

    # Set up console message logging for debugging
    def log_console(msg: Any) -> None:  # noqa: ANN401  # playwright console message
        with open("/tmp/browser_console.log", "a", encoding="utf-8") as f:
            f.write(f"{msg.type}: {msg.text}\n")
        # Also log location for errors
        if msg.type == "error":
            with open("/tmp/browser_console.log", "a", encoding="utf-8") as f:
                f.write(f"  Location: {msg.location}\n")

    page.on("console", log_console)

    # Set up request/response logging for debugging
    # Always log API requests to help debug
    def log_request(req: Any) -> None:  # noqa: ANN401  # playwright request object
        print(f"[Request] {req.method} {req.url}")

    def log_response(res: Any) -> None:  # noqa: ANN401  # playwright response object
        print(f"[Response] {res.status} {res.url}")

    page.on("request", log_request)
    page.on("response", log_response)

    api_port, _ = api_socket_and_port
    base_url = f"http://localhost:{api_port}"

    # Navigate to base URL for test readiness
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle", timeout=15000)

    # TODO: Replace this sleep with a more deterministic wait condition
    # The sleep is a workaround for dynamic import race conditions in Vite.
    # Ideally, we should wait for a specific signal that all lazy-loaded
    # components are ready, but Vite doesn't provide such a mechanism.
    # This is only used in test setup, not in actual tests, so the
    # performance impact is minimal (adds 1s to fixture setup, not per test).
    await asyncio.sleep(1)
    print("Router and dynamic imports initialization complete")

    fixture = WebTestFixture(
        assistant=web_only_assistant,
        page=page,
        base_url=base_url,  # Direct to API server (serves built assets)
    )

    yield fixture

    # Teardown: Wait for any in-flight requests to complete before assistant shutdown
    # This prevents "database closed" errors during teardown when requests are still processing
    print("Waiting for in-flight requests to complete...")
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception as e:
        print(f"Warning: Could not wait for network idle during teardown: {e}")

    # Close the page to terminate any active SSE streams
    print("Closing page to terminate streaming connections...")
    try:
        await page.close()
    except Exception as e:
        print(f"Warning: Error closing page: {e}")

    # Additional delay to ensure all connections are fully closed and cleanup completes
    print("Waiting for connection cleanup...")
    await asyncio.sleep(1.0)


@pytest_asyncio.fixture(scope="function")
async def web_test_fixture_readonly(
    page: Page,
    web_readonly_assistant: Assistant,
    session_api_socket_and_port: tuple[int, socket.socket],
    build_frontend_assets: None,  # Ensure frontend is built before tests
) -> AsyncGenerator[WebTestFixture, None]:
    """Combined fixture providing web test dependencies with session-scoped Assistant.

    This fixture uses a session-scoped Assistant instance for better performance.
    Use for read-only tests that don't modify persistent state.

    For tests that create/modify data, use web_test_fixture instead.
    """

    # Set up console message logging for debugging
    def log_console(msg: Any) -> None:  # noqa: ANN401  # playwright console message
        with open("/tmp/browser_console.log", "a", encoding="utf-8") as f:
            f.write(f"{msg.type}: {msg.text}\n")
        # Also log location for errors
        if msg.type == "error":
            with open("/tmp/browser_console.log", "a", encoding="utf-8") as f:
                f.write(f"  Location: {msg.location}\n")

    page.on("console", log_console)

    # Set up request/response logging for debugging
    def log_request(req: Any) -> None:  # noqa: ANN401  # playwright request object
        print(f"[Request] {req.method} {req.url}")

    def log_response(res: Any) -> None:  # noqa: ANN401  # playwright response object
        print(f"[Response] {res.status} {res.url}")

    page.on("request", log_request)
    page.on("response", log_response)

    api_port, _ = session_api_socket_and_port
    base_url = f"http://localhost:{api_port}"

    # Navigate to base URL for test readiness
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle", timeout=15000)

    await asyncio.sleep(1)
    print("Router and dynamic imports initialization complete")

    fixture = WebTestFixture(
        assistant=web_readonly_assistant,
        page=page,
        base_url=base_url,
    )

    yield fixture

    # Teardown: Wait for any in-flight requests to complete
    print("Waiting for in-flight requests to complete...")
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception as e:
        print(f"Warning: Could not wait for network idle during teardown: {e}")

    # Close the page to terminate any active SSE streams
    print("Closing page to terminate streaming connections...")
    try:
        await page.close()
    except Exception as e:
        print(f"Warning: Error closing page: {e}")

    # Additional delay to ensure all connections are fully closed
    print("Waiting for connection cleanup...")
    await asyncio.sleep(1.0)


@pytest_asyncio.fixture(scope="function")
async def authenticated_page(web_test_fixture: WebTestFixture) -> Page:
    """Page with simulated authentication if needed.

    Currently returns the same page since auth is disabled in tests.
    This fixture provides a consistent interface for future auth testing.
    """
    # In the future, this could:
    # - Set auth cookies/tokens
    # - Navigate through login flow
    # - Set up mock auth state
    return web_test_fixture.page


class TestDataFactory:
    """Factory for creating test data consistently."""

    def __init__(self, db_context: DatabaseContext) -> None:
        self.db_context = db_context
        self._note_counter = 0
        self._document_counter = 0

    async def create_note(
        self,
        content: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a test note."""
        self._note_counter += 1
        if content is None:
            content = f"Test note {self._note_counter}"

        # Implementation would use the db_context to create note
        # This is a placeholder for the pattern
        return {
            "id": f"test_note_{self._note_counter}",
            "content": content,
            "tags": tags or [],
            "metadata": metadata or {},
        }

    async def create_document(
        self,
        title: str | None = None,
        content: str | None = None,
        file_type: str = "text/plain",
    ) -> dict[str, Any]:
        """Create a test document."""
        self._document_counter += 1
        if title is None:
            title = f"Test Document {self._document_counter}"

        return {
            "id": f"test_doc_{self._document_counter}",
            "title": title,
            "content": content or f"Content for {title}",
            "file_type": file_type,
        }


@pytest.fixture
def test_data_factory(db_engine: AsyncEngine) -> TestDataFactory:
    """Factory for creating test data."""
    # In real implementation, would create a DatabaseContext
    # For now, return a factory that creates mock data
    return TestDataFactory(None)  # type: ignore[arg-type]


class ConsoleErrorCollector:
    """Collector for browser console errors during tests."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._setup_listeners()

    def _setup_listeners(self) -> None:
        """Set up console message listeners."""

        def handle_console_message(msg: Any) -> None:  # noqa: ANN401  # playwright console message
            if msg.type == "error":
                self.errors.append(
                    f"{msg.location.get('url', 'unknown')}:{msg.location.get('lineNumber', '?')} - {msg.text}"
                )
            elif msg.type == "warning":
                self.warnings.append(msg.text)

        self.page.on("console", handle_console_message)

    def assert_no_errors(self) -> None:
        """Assert that no console errors were collected."""
        assert len(self.errors) == 0, (
            f"Found {len(self.errors)} console errors:\n" + "\n".join(self.errors)
        )

    def assert_no_warnings(self) -> None:
        """Assert that no console warnings were collected."""
        assert len(self.warnings) == 0, (
            f"Found {len(self.warnings)} console warnings:\n" + "\n".join(self.warnings)
        )

    def clear(self) -> None:
        """Clear collected errors and warnings."""
        self.errors.clear()
        self.warnings.clear()


@pytest.fixture
def console_error_checker(web_test_fixture: WebTestFixture) -> ConsoleErrorCollector:
    """Fixture that collects and checks console errors."""
    return ConsoleErrorCollector(web_test_fixture.page)


@pytest.fixture(scope="session")
def connect_options() -> dict[str, str] | None:
    """Configure Playwright browser connection options.

    Supports connecting to remote browser instances via the PLAYWRIGHT_WS_ENDPOINT
    environment variable. This is useful for containerized environments or CI/CD
    systems where browsers run in separate containers.

    Example:
        export PLAYWRIGHT_WS_ENDPOINT="ws://localhost:1234"
        pytest tests/functional/web/
    """
    ws_endpoint = os.getenv("PLAYWRIGHT_WS_ENDPOINT")
    if ws_endpoint:
        return {
            "ws_endpoint": ws_endpoint,
        }
    return None


# Override the playwright browser fixture to add timeout on close
@pytest_asyncio.fixture(scope="session")
async def browser(
    launch_browser: Any,  # noqa: ANN401  # From playwright's fixtures
) -> AsyncGenerator[Any, None]:  # noqa: ANN401  # playwright browser object
    """Override playwright's browser fixture to add timeout on close.

    This prevents the test suite from hanging when browser.close() gets stuck
    waiting for protocol callbacks during session teardown.

    This is a workaround for an issue with pytest-asyncio session-scoped event loops
    and playwright browser fixtures. See:
    - https://github.com/microsoft/playwright-pytest/issues/167
    - https://github.com/pytest-dev/pytest-asyncio/issues/730
    """
    # Launch the browser using playwright's launch function
    browser = await launch_browser()
    yield browser

    # Close with timeout to prevent hanging
    try:
        await asyncio.wait_for(browser.close(), timeout=10.0)
        print("\n=== Browser closed successfully ===")
    except asyncio.TimeoutError:
        print("\n=== WARNING: Browser close timed out after 10s, forcing cleanup ===")
    # If timeout occurs, we let pytest-playwright handle any remaining cleanup


# Override the playwright fixture to add timeout on stop
@pytest_asyncio.fixture(scope="session")
async def playwright() -> AsyncGenerator[Any, None]:
    """Override playwright's main fixture to add timeout on stop.

    This prevents the test suite from hanging when playwright.stop() gets stuck
    during session teardown.

    This is part of the workaround for pytest-asyncio session-scoped event loops.
    """

    # Start playwright using the context manager
    pw_context_manager = async_playwright()
    pw = await pw_context_manager.start()

    yield pw

    # Stop with timeout to prevent hanging
    try:
        await asyncio.wait_for(
            pw_context_manager.__aexit__(None, None, None), timeout=10.0
        )
        print("\n=== Playwright stopped successfully ===")
    except asyncio.TimeoutError:
        print("\n=== WARNING: Playwright stop timed out after 10s, forcing cleanup ===")


# --- Shared API Test Fixtures ---


@pytest_asyncio.fixture(scope="function")
async def api_db_context(
    db_engine: AsyncEngine,
) -> AsyncGenerator[DatabaseContext, None]:
    """Provides a DatabaseContext for API tests."""
    async with get_db_context(engine=db_engine) as ctx:
        yield ctx


@pytest.fixture(scope="function")
def api_mock_processing_service_config() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for API tests."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": (
                "You are a test assistant. Current time: {current_time}. "
                "Server URL: {server_url}. "
                "Context: {aggregated_other_context}"
            )
        },
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": [
                "add_or_update_note"
            ],  # Ensure our target tool is enabled
            "enable_mcp_server_ids": [],
            "confirm_tools": [],  # Ensure add_or_update_note is NOT here for API test
        },
        delegation_security_level="confirm",
        id="chat_api_test_profile",
    )


@pytest.fixture(scope="function")
def api_mock_llm_client() -> RuleBasedMockLLMClient:
    """Provides a RuleBasedMockLLMClient for API tests."""
    return RuleBasedMockLLMClient(rules=[])  # Rules will be set per-test


@pytest_asyncio.fixture(scope="function")
async def api_test_tools_provider(
    api_mock_processing_service_config: ProcessingServiceConfig,
) -> ToolsProvider:
    """
    Provides a ToolsProvider stack (Local, MCP, Composite, Confirming)
    configured for testing.
    """
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,  # Use actual definitions
        implementations=local_tool_implementations,  # Use actual implementations
        embedding_generator=None,  # Not needed for add_note
        calendar_config={},  # Empty calendar config for tests
    )
    # Mock MCP provider as it's not the focus here
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()  # Initialize

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=set(
            api_mock_processing_service_config.tools_config.get("confirm_tools", [])
        ),
    )
    await confirming_provider.get_tool_definitions()  # Initialize
    return confirming_provider


@pytest.fixture(scope="function")
def api_test_processing_service(
    api_mock_llm_client: RuleBasedMockLLMClient,
    api_test_tools_provider: ToolsProvider,
    api_mock_processing_service_config: ProcessingServiceConfig,
    api_db_context: DatabaseContext,
    attachment_registry_fixture: AttachmentRegistry,
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components."""

    # NotesContextProvider expects get_db_context_func to be Callable[[], Awaitable[DatabaseContext]]
    # This means it wants a function that, when called and awaited, returns an *entered* DatabaseContext.
    # The db_context fixture provides an already entered DatabaseContext instance.
    # We need its engine to create new contexts for the provider if it manages its own lifecycle.
    captured_engine = api_db_context.engine

    async def get_entered_db_context_for_provider() -> DatabaseContext:
        """
        Returns an awaitable that resolves to an entered DatabaseContext.
        This matches the expected type for NotesContextProvider's get_db_context_func.
        """
        async with get_db_context(engine=captured_engine) as new_ctx:
            return new_ctx

    # Create mock context providers
    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=api_mock_processing_service_config.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config={},  # Empty calendar config for tests
        timezone_str=api_mock_processing_service_config.timezone_str,
        prompts=api_mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=api_mock_processing_service_config.prompts
    )
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=api_mock_llm_client,
        tools_provider=api_test_tools_provider,
        service_config=api_mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config={},  # Minimal app_config for this test
        attachment_registry=attachment_registry_fixture,
    )


@pytest.fixture(scope="function")
def attachment_registry_fixture(db_engine: AsyncEngine) -> AttachmentRegistry:
    """Create a real AttachmentRegistry for functional tests."""
    attachment_temp_dir = tempfile.mkdtemp()
    return AttachmentRegistry(
        storage_path=attachment_temp_dir, db_engine=db_engine, config=None
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine,
    attachment_registry_fixture: AttachmentRegistry,
    api_test_processing_service: ProcessingService,
    api_test_tools_provider: ToolsProvider,
    api_mock_llm_client: LLMInterface,
) -> FastAPI:
    """
    Creates a FastAPI application instance for testing, with dependency-injected
    AttachmentRegistry.
    """
    # Make a copy of the actual app to avoid modifying it globally
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware,  # Use actual middleware
    )
    app.include_router(actual_app.router)  # Include actual routers

    # Override dependencies in app.state
    app.state.processing_service = api_test_processing_service
    app.state.tools_provider = (
        api_test_tools_provider  # For /api/tools/execute if needed
    )
    app.state.database_engine = db_engine  # For get_db dependency
    app.state.config = {  # Minimal config for dependencies
        "auth_enabled": False,  # Authentication is OFF for tests
        "database_url": str(db_engine.url),
        "default_profile_settings": {  # For KnownUsersContextProvider
            "chat_id_to_name_map": {},
            "processing_config": {"prompts": {}},
        },
    }
    app.state.llm_client = api_mock_llm_client  # For other parts that might use it
    app.state.debug_mode = False  # Explicitly set for tests

    # Use the dependency-injected attachment registry
    app.state.attachment_registry = attachment_registry_fixture

    # Ensure database is initialized for this app instance
    async with get_db_context(engine=db_engine) as temp_db_ctx:
        await init_db(db_engine)  # Initialize main schema
        await temp_db_ctx.init_vector_db()  # Initialize vector schema

    return app


@pytest_asyncio.fixture(scope="function")
async def api_test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Provides an HTTPX AsyncClient for the test FastAPI app."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
