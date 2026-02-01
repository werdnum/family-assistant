"""Basic test to verify Page Object Model infrastructure works."""

import pytest

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages import BasePage


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_base_page_navigation(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the base page can navigate to different routes."""
    page = BasePage(web_test_fixture_readonly.page, web_test_fixture_readonly.base_url)

    # Navigate to homepage (which shows landing page)
    await page.navigate_to("/")
    # Check that we're on the landing page
    assert await page.is_element_visible('h1:has-text("Family Assistant")')

    # Navigate to notes page
    await page.navigate_to("/notes")
    # The page should exist even if there are no notes
    assert "/notes" in web_test_fixture_readonly.page.url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_base_page_console_errors(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that we can capture console errors."""
    page = BasePage(web_test_fixture_readonly.page, web_test_fixture_readonly.base_url)

    # Start capturing console errors
    errors = page.setup_console_error_collection()

    # Enable request logging to debug what's happening
    page.setup_request_logging()

    # Navigate to a page
    await page.navigate_to("/")

    # Wait for page to be truly idle before checking console errors
    # This prevents race conditions with background data fetching
    await page.wait_for_page_idle()

    # Should have no console errors on homepage
    assert len(errors) == 0, f"Found console errors: {errors}"
