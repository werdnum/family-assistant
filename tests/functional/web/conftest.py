"""Test fixtures for Playwright-based web UI integration tests."""

# IMPORTANT: Disable auth BEFORE importing any web modules
import os

os.environ["OIDC_CLIENT_ID"] = ""  # Empty to ensure auth is disabled
os.environ["OIDC_CLIENT_SECRET"] = ""
os.environ["OIDC_DISCOVERY_URL"] = ""
os.environ["SESSION_SECRET_KEY"] = ""

import asyncio
import contextlib
import subprocess
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
import pytest_asyncio
from playwright.async_api import Page, async_playwright
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


class WebTestFixture(NamedTuple):
    """Container for web test dependencies."""

    assistant: Assistant
    page: Page
    base_url: str


async def wait_for_server(url: str, timeout: int = 30) -> None:
    """Wait for a server to become available."""
    start_time = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start_time < timeout:
            try:
                response = await client.get(url)
                if response.status_code < 500:
                    return
            except (httpx.ConnectError, httpx.ReadTimeout):
                pass
            await asyncio.sleep(0.5)
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
    # Get random ports for both servers to avoid conflicts in parallel tests
    vite_port = find_free_port()
    api_port = find_free_port()
    return vite_port, api_port


@pytest.fixture(scope="module")
def vite_server(vite_and_api_ports: tuple[int, int]) -> Generator[str, None, None]:
    """Start Vite dev server for frontend assets on a random port."""
    # Check if npm is available
    try:
        # Use shell=True to ensure PATH is properly loaded
        result = subprocess.run(
            "npm --version", shell=True, check=True, capture_output=True, text=True
        )
        print(f"npm version: {result.stdout.strip()}")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"npm check failed: {e}")
        pytest.skip("npm is not available, skipping Vite server tests")

    # Change to frontend directory
    frontend_dir = Path(__file__).parent.parent.parent.parent / "frontend"
    print(f"Frontend directory: {frontend_dir}")
    if not frontend_dir.exists():
        pytest.skip(f"Frontend directory not found at {frontend_dir}")

    # Check if node_modules exists
    node_modules = frontend_dir / "node_modules"
    print(f"Node modules directory: {node_modules}")
    print(f"Node modules exists: {node_modules.exists()}")
    if not node_modules.exists():
        pytest.skip(
            "node_modules not found. Run 'npm install' in frontend directory first."
        )

    # Get the ports
    vite_port, api_port = vite_and_api_ports
    print(f"Starting Vite on port {vite_port}, proxying to API on port {api_port}")

    # Start Vite dev server with custom port and API port via env var
    process = subprocess.Popen(
        f"npm run dev -- --port {vite_port} --host 127.0.0.1",
        shell=True,
        cwd=str(frontend_dir),
        stdout=None,  # Inherit parent's stdout
        stderr=None,  # Inherit parent's stderr
        env={**os.environ, "NODE_ENV": "test", "VITE_API_PORT": str(api_port)},
    )

    vite_url = f"http://localhost:{vite_port}"

    # Wait for Vite to be ready - simple synchronous check
    def wait_for_vite() -> bool:
        import socket
        import time

        start_time = time.time()
        while time.time() - start_time < 30:
            try:
                # Just check if port is open
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(("127.0.0.1", vite_port))
                sock.close()
                if result == 0:
                    print(f"Vite port {vite_port} is open")
                    # Give it a moment to fully initialize
                    time.sleep(2)
                    return True
            except Exception as e:
                print(f"Vite check error: {e}")
            time.sleep(0.5)
        return False

    if not wait_for_vite():
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        pytest.skip(f"Vite server did not start within 30 seconds on port {vite_port}")

    print("Vite server is ready!")

    yield vite_url

    # Cleanup: terminate Vite server
    print(f"Stopping Vite server on port {vite_port}")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("Vite didn't stop gracefully, killing it")
        process.kill()
        process.wait()


@pytest_asyncio.fixture(scope="function")
async def web_only_assistant(
    db_engine: AsyncEngine,
    vite_server: str,  # Vite server is required for full UI testing
    vite_and_api_ports: tuple[int, int],
) -> AsyncGenerator[Assistant, None]:
    """Start Assistant in web-only mode for testing."""
    # Auth is already disabled at module level

    # Force dev mode for tests
    os.environ["DEV_MODE"] = "true"

    # Get the API port
    _, api_port = vite_and_api_ports
    print(f"Starting FastAPI on port {api_port}")

    # Create minimal test configuration
    test_config: dict[str, Any] = {
        "telegram_enabled": False,  # Disable Telegram for web-only tests
        "telegram_token": None,  # Not needed when disabled
        "allowed_user_ids": [],  # Not needed when disabled
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
        "oidc": {
            "client_id": "",
            "client_secret": "",
            "discovery_url": "",
        },
        "default_service_profile_id": "web_test_profile",
        "service_profiles": [
            {
                "id": "web_test_profile",
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

    # Create mock LLM client
    mock_llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(content="Test response from mock LLM"),
    )

    # Create Assistant instance
    assistant = Assistant(
        config=test_config,
        llm_client_overrides={"mock-model-for-testing": mock_llm_client},
    )

    # Set up dependencies
    await assistant.setup_dependencies()

    # Start services in background task
    start_task = asyncio.create_task(assistant.start_services())

    # Wait for web server to be ready on the configured port
    await wait_for_server(f"http://localhost:{api_port}")

    yield assistant

    # Cleanup
    assistant.initiate_shutdown("TEST")

    # First stop the task worker properly
    if hasattr(assistant, "task_worker_instance") and assistant.task_worker_instance:
        assistant.task_worker_instance.shutdown_event.set()
        # The task_worker_task is created but not stored as an attribute
        # so we can't wait for it directly

    # Then wait for services to stop
    try:
        await asyncio.wait_for(start_task, timeout=5.0)
    except asyncio.TimeoutError:
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task


@pytest_asyncio.fixture(scope="function")
async def playwright_page(
    web_only_assistant: Assistant,
) -> AsyncGenerator[Page, None]:
    """Provide Playwright browser page for tests."""
    async with async_playwright() as p:
        # Launch browser in headless mode by default
        browser = await p.chromium.launch(
            headless=os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        )

        # Create browser context with viewport size
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )

        # Create a new page
        page = await context.new_page()

        # Set up console message logging for debugging
        page.on(
            "console", lambda msg: print(f"[Browser Console] {msg.type}: {msg.text}")
        )

        # Set up request/response logging for debugging (optional)
        if os.getenv("PLAYWRIGHT_DEBUG_NETWORK"):
            page.on(
                "request",
                lambda req: print(f"[Network Request] {req.method} {req.url}"),
            )
            page.on(
                "response",
                lambda res: print(f"[Network Response] {res.status} {res.url}"),
            )

        yield page

        # Cleanup
        await context.close()
        await browser.close()


@pytest_asyncio.fixture(scope="function")
async def web_test_fixture(
    playwright_page: Page,
    web_only_assistant: Assistant,
    vite_server: str,
) -> WebTestFixture:
    """Combined fixture providing all web test dependencies."""
    return WebTestFixture(
        assistant=web_only_assistant,
        page=playwright_page,
        base_url=vite_server,  # Use Vite dev server for full JS/CSS support
    )
