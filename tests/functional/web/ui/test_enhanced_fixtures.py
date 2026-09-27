"""Simple tests to verify enhanced Playwright fixtures work correctly."""

from typing import Any

import pytest

from tests.functional.web.conftest import ConsoleErrorCollector
from tests.helpers import wait_for_condition


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_console_error_checker_basic(
    page: Any,  # noqa: ANN401  # playwright fixture
    console_error_checker: ConsoleErrorCollector,
) -> None:
    """Test that console_error_checker captures errors."""

    # Initially no errors
    console_error_checker.assert_no_errors()

    # Navigate to a blank page
    await page.goto("about:blank")

    # Inject a console error
    await page.evaluate("console.error('Test error from test');")
    await wait_for_condition(
        lambda: len(console_error_checker.errors) == 1,
        timeout=10,
        description="browser console error",
    )

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_console_error_checker_warnings(
    page: Any,  # noqa: ANN401  # playwright fixture
    console_error_checker: ConsoleErrorCollector,
) -> None:
    """Test that console_error_checker captures warnings separately."""

    # Navigate to blank page
    await page.goto("about:blank")

    # Inject a warning
    await page.evaluate("console.warn('Test warning');")
    await wait_for_condition(
        lambda: len(console_error_checker.warnings) == 1,
        timeout=10,
        description="browser console warning",
    )

    # Should have warning but no errors
    console_error_checker.assert_no_errors()
    assert len(console_error_checker.warnings) == 1
    assert "Test warning" in console_error_checker.warnings[0]

    # Test warning assertion
    with pytest.raises(AssertionError) as exc_info:
        console_error_checker.assert_no_warnings()
    assert "Found 1 console warnings" in str(exc_info.value)
