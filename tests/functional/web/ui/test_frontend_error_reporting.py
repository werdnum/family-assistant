"""End-to-end tests for frontend error reporting integration.

These tests verify that the frontend error handling components are properly
integrated. Detailed testing of the error client and ErrorBoundary is done
in unit tests (frontend/src/**/__tests__/*.test.ts).
"""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_chat_page_loads_with_error_handlers_initialized(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the chat page loads with error handlers initialized."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to chat page
    await page.goto(f"{base_url}/chat")
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)

    # Verify the error handlers are initialized by checking the global handlers exist
    handlers_initialized = await page.evaluate(
        """
        () => {
            return window.onerror !== null && window.onunhandledrejection !== null;
        }
        """
    )

    assert handlers_initialized, "Error handlers should be initialized on page load"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_chat_app_wrapped_in_error_boundary(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that the ChatApp is wrapped in an ErrorBoundary.

    When no errors occur, the ErrorBoundary should render its children
    (the ChatApp) normally.
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to chat page
    await page.goto(f"{base_url}/chat")
    await page.wait_for_selector('[data-app-ready="true"]', timeout=10000)

    # Verify normal chat UI is displayed (meaning ErrorBoundary is rendering children)
    chat_input = page.locator('[data-testid="chat-input"]')
    await chat_input.wait_for(state="visible", timeout=5000)

    assert await chat_input.is_visible(), "Chat input should be visible when no errors"
