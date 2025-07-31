"""
Playwright-based tests for UI endpoint accessibility.
Migrated from test_ui_endpoints.py to use real browser testing.
"""

import asyncio
from typing import Any

import pytest

from tests.functional.web.pages import BasePage

# Base UI endpoints accessible regardless of auth state
# For pages that expect data (e.g., editing a specific note), we test with a
# non-existent item to ensure it returns appropriate error rather than server error
BASE_UI_ENDPOINTS = [
    ("/", "Notes List Page", ["h1", "nav"]),
    ("/notes/add", "Add Note Form Page", ["form", "input", "button"]),
    (
        "/notes/edit/non_existent_note_for_test",
        "Edit Non-Existent Note Form Page",
        ["body"],
    ),
    ("/docs/", "Documentation Index Page", ["h1", "a"]),
    ("/docs/USER_GUIDE.md", "USER_GUIDE.md Document Page", ["h1", "p"]),
    ("/history", "Message History Page", ["h1"]),
    ("/tools", "Available Tools Page", ["h1", "article"]),
    ("/tasks", "Tasks List Page", ["h1"]),
    ("/vector-search", "Vector Search Page", ["h1", "form", "input"]),
    ("/documents/upload", "Document Upload Page", ["h1", "form"]),
    # Note: tokens page may have issues with auth disabled, but we test it anyway
    ("/settings/tokens", "Manage API Tokens Page", ["h1"]),
    ("/events", "Events List Page", ["main h1"]),
    (
        "/events/non_existent_event",
        "Event Detail Page",
        ["body"],
    ),  # May show error page
    ("/event-listeners", "Event Listeners List Page", ["main h1"]),
    ("/event-listeners/new", "Create Event Listener Page", ["main h1", "form"]),
    (
        "/event-listeners/99999",
        "Event Listener Detail Page",
        ["body"],
    ),  # May show error page
    ("/errors/", "Error Logs List Page", ["main h1"]),
]


async def check_endpoint(
    browser: Any, base_url: str, endpoint_info: tuple[str, str, list[str]]
) -> tuple[list[str], list[str]]:
    """Check a single endpoint and return failures and warnings."""
    path, description, expected_elements = endpoint_info
    failures = []
    warnings = []

    # Create a new page for this endpoint check
    page = await browser.new_page()
    try:
        base_page = BasePage(page, base_url)

        # Navigate to the endpoint
        print(f"Test: Navigating to {base_url}{path}")
        response = await base_page.navigate_to(path)
        print(f"Response status: {response.status if response else 'None'}")

        # Check response status
        if response is None:
            failures.append(f"Failed to navigate to {path}")
            return failures, warnings

        # Log error responses for debugging
        if response.status >= 400:
            print(f"Error Response status: {response.status}")
            page_content = await page.content()
            print(f"Error Page content preview: {page_content[:500]}...")

        if response.status >= 500:
            failures.append(
                f"UI endpoint '{description}' at '{path}' returned server error: "
                f"{response.status}"
            )
            return failures, warnings

        # Wait for page to load (using fast DOM load by default)
        await base_page.wait_for_load()

        # Check for expected elements
        for selector in expected_elements:
            is_visible = await base_page.is_element_visible(selector)
            if not is_visible:
                warnings.append(
                    f"Expected element '{selector}' not found on {description} at {path}"
                )
    finally:
        await page.close()

    return failures, warnings


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_ui_endpoint_accessibility_playwright(
    web_test_fixture: Any,
    console_error_checker: Any,
) -> None:
    """
    Test that all UI endpoints are accessible via Playwright and render without errors.
    Uses parallel execution to speed up testing of multiple endpoints.
    """
    browser = web_test_fixture.page.context.browser
    base_url = web_test_fixture.base_url

    # Split endpoints into batches for parallel processing
    # Process 5 endpoints at a time to avoid overwhelming the server
    batch_size = 5
    all_failures = []
    all_warnings = []

    for i in range(0, len(BASE_UI_ENDPOINTS), batch_size):
        batch = BASE_UI_ENDPOINTS[i : i + batch_size]

        # Run endpoint checks in parallel for this batch
        results = await asyncio.gather(*[
            check_endpoint(browser, base_url, endpoint_info) for endpoint_info in batch
        ])

        # Collect results
        for failures, warnings in results:
            all_failures.extend(failures)
            all_warnings.extend(warnings)

    # Report all failures and warnings
    if all_failures:
        pytest.fail("The following endpoints failed:\n" + "\n".join(all_failures))

    if all_warnings:
        print("Warnings encountered:\n" + "\n".join(all_warnings))


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_links_work(
    web_test_fixture: Any,
    console_error_checker: Any,
) -> None:
    """Test that navigation links in the UI actually work."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    base_page = BasePage(page, base_url)

    # Start at home page
    await base_page.navigate_to("/")
    await base_page.wait_for_load()

    # Find and test navigation links
    nav_links = await page.locator("nav a").all()
    assert len(nav_links) > 0, "No navigation links found"

    # Test first few nav links to avoid long test
    for i in range(min(3, len(nav_links))):
        link = nav_links[i]
        href = await link.get_attribute("href")
        if href and not href.startswith("http"):  # Skip external links
            # Click the link
            await link.click()
            await base_page.wait_for_load()

            # Verify we navigated somewhere
            current_url = page.url
            assert base_url in current_url, f"Navigation failed for link: {href}"

            # Go back to home for next test
            await base_page.navigate_to("/")
            await base_page.wait_for_load()

    # Assert no console errors throughout navigation
    console_error_checker.assert_no_errors()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_responsive_design_mobile(
    web_test_fixture: Any,
    console_error_checker: Any,
) -> None:
    """Test that pages work on mobile viewport sizes."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    base_page = BasePage(page, base_url)

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Test a few key pages
    mobile_test_pages = [
        ("/", "Home"),
        ("/notes/add", "Add Note"),
        ("/vector-search", "Search"),
    ]

    for path, name in mobile_test_pages:
        await base_page.navigate_to(path)
        await base_page.wait_for_load()

        # Check that page renders without horizontal scroll
        body_width = await page.evaluate("document.body.scrollWidth")
        viewport_width = await page.evaluate("window.innerWidth")
        assert body_width <= viewport_width + 20, (  # Allow small margin
            f"{name} page has horizontal scroll on mobile: "
            f"body width {body_width}px > viewport {viewport_width}px"
        )

    # Assert no console errors
    console_error_checker.assert_no_errors()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_form_interactions(
    web_test_fixture: Any,
    console_error_checker: Any,
) -> None:
    """Test basic form interactions work without errors."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    base_page = BasePage(page, base_url)

    # Navigate to vector search page (has a simple search form)
    print(f"Navigating to vector search page at {base_url}")
    await base_page.navigate_to("/vector-search")
    await base_page.wait_for_load()

    # Check if we got HTML or JSON
    page_content = await page.content()
    print(f"Page content type check - starts with: {page_content[:100]}")

    # Find search input
    search_input = page.locator('input[type="text"], input[type="search"]').first
    assert await search_input.is_visible(), "Search input not found"

    # Type in search box
    await search_input.fill("test search query")

    # Find and click search button
    search_button = page.locator('button[type="submit"], button:has-text("Search")')
    if await search_button.count() > 0:
        await search_button.first.click()
        # Wait for any response
        await page.wait_for_timeout(1000)

    # Assert no console errors
    console_error_checker.assert_no_errors()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_loading_states(
    web_test_fixture: Any,
    console_error_checker: Any,
) -> None:
    """Test that pages show appropriate loading states."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url
    base_page = BasePage(page, base_url)

    # Navigate to tasks page (likely to have loading states)
    await base_page.navigate_to("/tasks")

    # Check for loading indicators or quick page load
    # Most pages should either show content quickly or show a loader
    try:
        # Wait for either main content or loading indicator
        await page.wait_for_selector(
            "h1, .loading, .spinner, [role='progressbar']",
            timeout=3000,
        )
    except Exception:
        pytest.fail("Page did not show content or loading state within 3 seconds")

    # Wait for final load
    await base_page.wait_for_load()

    # Assert no console errors
    console_error_checker.assert_no_errors()
