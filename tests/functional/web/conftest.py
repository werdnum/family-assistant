"""Simplified test fixtures for Playwright-based web UI integration tests."""

import asyncio
import contextlib
import os
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
import pytest_asyncio
from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.storage.context import DatabaseContext
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


def find_free_port() -> int:
    """Find a free port on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def vite_and_api_ports() -> tuple[int, int]:
    """Get random free ports for both Vite and API servers."""
    # For the simplified approach, we only need the API port
    api_port = find_free_port()
    return 0, api_port  # Vite port is 0 since we're not using it


@pytest.fixture(scope="module", autouse=True)
def build_frontend_assets() -> None:
    """Build frontend assets before running web tests."""
    frontend_dir = Path(__file__).parent.parent.parent.parent / "frontend"
    dist_dir = frontend_dir.parent / "src" / "family_assistant" / "static" / "dist"

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

    # Check if we need to rebuild (simple timestamp check)
    need_rebuild = True
    if dist_dir.exists() and any(dist_dir.iterdir()):
        # Check if source files are newer than build
        src_files = (
            list(frontend_dir.glob("src/**/*.js"))
            + list(frontend_dir.glob("src/**/*.jsx"))
            + list(frontend_dir.glob("src/**/*.css"))
        )
        if src_files:
            newest_src = max(f.stat().st_mtime for f in src_files)
            oldest_dist = min(f.stat().st_mtime for f in dist_dir.iterdir())
            if oldest_dist > newest_src:
                print("Frontend assets are up to date, skipping build")
                need_rebuild = False

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
    vite_and_api_ports: tuple[int, int],
    build_frontend_assets: None,  # Ensure assets are built
    mock_llm_client: RuleBasedMockLLMClient,
) -> AsyncGenerator[Assistant, None]:
    """Start Assistant in web-only mode for testing."""
    # Auth is already disabled at module level
    # DEV_MODE is already set to false at module level

    # Get the API port
    _, api_port = vite_and_api_ports
    print(f"\n=== Starting API server on port {api_port} ===")

    # Create minimal test configuration
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
        "document_storage_path": "/tmp/test_docs",
        "attachment_storage_path": "/tmp/test_attachments",
        "litellm_debug": False,
        "dev_mode": False,  # Explicitly set dev_mode to False for tests
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
                    "enable_local_tools": ["add_or_update_note", "search_documents"],
                    "confirm_tools": [],
                    "mcp_initialization_timeout_seconds": 5,
                },
                "chat_id_to_name_map": {},
                "slash_commands": [],
            }
        ],
        "mcp_config": {"mcpServers": {}},
        "indexing_pipeline_config": {"processors": []},
        "event_system": {"enabled": False},
    }

    # Create Assistant instance using the provided mock LLM client
    assistant = Assistant(
        config=test_config,
        llm_client_overrides={
            "default_assistant": mock_llm_client
        },  # Key by profile ID, not model name
    )

    # Enable debug mode for tests to get detailed error messages
    from family_assistant.web.app_creator import configure_app_debug

    configure_app_debug(debug=True)

    # Set up dependencies
    print("Setting up dependencies...")
    await assistant.setup_dependencies()
    print("Dependencies set up")

    # Set SERVER_URL env var so document upload knows where to send API calls
    os.environ["SERVER_URL"] = f"http://localhost:{api_port}"

    # Start services in background task
    print(f"Starting API services on port {api_port}...")
    start_task = asyncio.create_task(assistant.start_services())

    # Give the server a moment to start initialization
    print("Waiting for server initialization...")
    await asyncio.sleep(2)
    print("Starting health checks...")

    # Wait for web server to be ready
    try:
        await wait_for_server(
            f"http://localhost:{api_port}", timeout=120
        )  # Give it 2 minutes
        print(f"\n=== API server ready on port {api_port} ===")
    except Exception as e:
        print(f"\n=== Failed to start API server: {e} ===")
        # Cancel the start task
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task
        raise

    yield assistant

    # Cleanup
    assistant.initiate_shutdown("TEST")

    # Stop task worker
    if hasattr(assistant, "task_worker_instance") and assistant.task_worker_instance:
        assistant.task_worker_instance.shutdown_event.set()

    # Wait for services to stop
    try:
        await asyncio.wait_for(start_task, timeout=5.0)
    except asyncio.TimeoutError:
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task


@pytest_asyncio.fixture(scope="function")
async def web_test_fixture(
    page: Page,
    web_only_assistant: Assistant,
    vite_and_api_ports: tuple[int, int],
    build_frontend_assets: None,  # Ensure frontend is built before tests
) -> WebTestFixture:
    """Combined fixture providing all web test dependencies."""

    # Set up console message logging for debugging
    def log_console(msg: Any) -> None:
        print(f"[Browser Console] {msg.type}: {msg.text}")
        # Also log location for errors
        if msg.type == "error":
            print(f"  Location: {msg.location}")

    page.on("console", log_console)

    # Set up request/response logging for debugging
    # Always log API requests to help debug
    def log_request(req: Any) -> None:
        if "/api/" in req.url:
            print(f"[API Request] {req.method} {req.url}")
            if req.method == "POST":
                print(f"  Body: {req.post_data}")
        elif req.url.endswith(".js") or req.url.endswith(".css"):
            print(f"[Asset Request] {req.method} {req.url}")

    def log_response(res: Any) -> None:
        if "/api/" in res.url:
            print(f"[API Response] {res.status} {res.url}")
        elif res.url.endswith(".js") or res.url.endswith(".css"):
            print(f"[Asset Response] {res.status} {res.url}")

    page.on("request", log_request)
    page.on("response", log_response)

    _, api_port = vite_and_api_ports
    return WebTestFixture(
        assistant=web_only_assistant,
        page=page,
        base_url=f"http://localhost:{api_port}",  # Direct to API server (serves built assets)
    )


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

        def handle_console_message(msg: Any) -> None:
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
