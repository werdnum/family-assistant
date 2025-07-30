"""Basic test to verify Page Object Model infrastructure works."""

from typing import Any

import pytest

from tests.functional.web.pages import BasePage


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_base_page_navigation(web_test_fixture: Any) -> None:
    """Test that the base page can navigate to different routes."""
    page = BasePage(web_test_fixture.page, web_test_fixture.base_url)

    # Navigate to homepage (which shows notes)
    await page.navigate_to("/")
    # Check that we're on the notes page (homepage)
    assert await page.is_element_visible("h1") or await page.is_element_visible("nav")

    # Navigate to notes page
    await page.navigate_to("/notes")
    # The page should exist even if there are no notes
    assert "/notes" in web_test_fixture.page.url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_base_page_console_errors(web_test_fixture: Any) -> None:
    """Test that we can capture console errors."""
    page = BasePage(web_test_fixture.page, web_test_fixture.base_url)

    # Start capturing console errors
    errors = page.setup_console_error_collection()

    # Enable request logging to debug what's happening
    page.setup_request_logging()

    # Navigate to a page
    await page.navigate_to("/")

    # Should have no console errors on homepage
    assert len(errors) == 0, f"Found console errors: {errors}"
