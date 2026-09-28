"""End-to-end test for frontend error handler initialization."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_chat_page_loads_with_error_handlers_initialized(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the chat page loads with error handlers initialized."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

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
