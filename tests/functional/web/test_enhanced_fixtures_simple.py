"""Simple tests to verify enhanced Playwright fixtures work correctly."""

from typing import Any

import pytest

from tests.functional.web.pages import BasePage


@pytest.mark.asyncio
async def test_authenticated_page_fixture(authenticated_page: Any) -> None:
    """Test that authenticated_page fixture provides a valid page."""
    # Since auth is disabled in tests, authenticated_page should work like regular page
    assert authenticated_page is not None
    # Should be able to navigate
    await authenticated_page.goto("about:blank")
    assert "blank" in authenticated_page.url


@pytest.mark.asyncio
async def test_console_error_checker_basic(
    page: Any,
    console_error_checker: Any,
) -> None:
    """Test that console_error_checker captures errors."""

    # Initially no errors
    console_error_checker.assert_no_errors()

    # Navigate to a blank page
    await page.goto("about:blank")

    # Inject a console error
    await page.evaluate("console.error('Test error from test');")

    # Should now have an error
    assert len(console_error_checker.errors) == 1
    assert "Test error from test" in console_error_checker.errors[0]

    # Test that assert_no_errors fails when there are errors
    with pytest.raises(AssertionError) as exc_info:
        console_error_checker.assert_no_errors()
    assert "Found 1 console errors" in str(exc_info.value)

    # Clear errors
    console_error_checker.clear()
    console_error_checker.assert_no_errors()  # Should pass now


@pytest.mark.asyncio
async def test_console_error_checker_warnings(
    page: Any,
    console_error_checker: Any,
) -> None:
    """Test that console_error_checker captures warnings separately."""

    # Navigate to blank page
    await page.goto("about:blank")

    # Inject a warning
    await page.evaluate("console.warn('Test warning');")

    # Should have warning but no errors
    console_error_checker.assert_no_errors()
    assert len(console_error_checker.warnings) == 1
    assert "Test warning" in console_error_checker.warnings[0]

    # Test warning assertion
    with pytest.raises(AssertionError) as exc_info:
        console_error_checker.assert_no_warnings()
    assert "Found 1 console warnings" in str(exc_info.value)


@pytest.mark.asyncio
async def test_base_page_with_fixtures(web_test_fixture: Any) -> None:
    """Test that BasePage works with web fixtures."""
    page = BasePage(web_test_fixture.page, web_test_fixture.base_url)

    # Should be able to navigate
    await page.navigate_to("/")

    # Should have basic page methods
    assert hasattr(page, "wait_for_load")
    assert hasattr(page, "fill_form_field")
    assert hasattr(page, "click_and_wait")
