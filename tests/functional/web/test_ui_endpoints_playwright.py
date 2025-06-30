"""
Playwright-based tests for UI endpoint accessibility.
Migrated from test_ui_endpoints.py to use real browser testing.
"""

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


@pytest.mark.asyncio
@pytest.mark.parametrize("path,description,expected_elements", BASE_UI_ENDPOINTS)
async def test_ui_endpoint_accessibility_playwright(
    web_test_fixture: Any,
    console_error_checker: Any,
    path: str,
    description: str,
    expected_elements: list[str],
) -> None:
    """
    Test that UI endpoints are accessible via Playwright and render without errors.

    This test:
    1. Navigates to each endpoint using real browser
    2. Checks for console errors
    3. Verifies key elements are present
    4. Ensures no server errors (500s)
    """
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Create page object for common operations
    base_page = BasePage(page, base_url)

    # Navigate to the endpoint
    print(f"Test: Navigating to {base_url}{path}")
    response = await base_page.navigate_to(path)
    print(f"Response status: {response.status if response else 'None'}")
    print(f"Current URL: {page.url}")

    # Check response status
    assert response is not None, f"Failed to navigate to {path}"

    # Log the response for debugging
    if response.status >= 400:
        print(f"Error Response status: {response.status}")
        print(f"Error Response URL: {response.url}")
        page_content = await page.content()
        print(f"Error Page content preview: {page_content[:500]}...")

    assert response.status < 500, (
        f"UI endpoint '{description}' at '{path}' returned server error: "
        f"{response.status}"
    )

    # Wait for page to load
    await base_page.wait_for_load()

    # Check for expected elements
    for selector in expected_elements:
        is_visible = await base_page.is_element_visible(selector)
        if not is_visible:
            # Dump HTML content to help debug
            page_content = await page.content()
            html_file = f"scratch/failed_{path.replace('/', '_')}.html"
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(page_content)
            # Also write a summary file that won't be captured by pytest
            with open(
                f"scratch/failed_{path.replace('/', '_')}_summary.txt",
                "w",
                encoding="utf-8",
            ) as f:
                f.write(f"Failed to find selector: {selector}\n")
                f.write(f"Page URL: {page.url}\n")
                f.write(f"Page title: {await page.title()}\n")
                f.write(f"First 1000 chars of HTML:\n{page_content[:1000]}\n")
        assert is_visible, (
            f"Expected element '{selector}' not found on {description} at {path}. "
            f"HTML dumped to scratch/failed_{path.replace('/', '_')}.html"
        )

    # Assert no console errors (except for expected 404s on non-existent resources)
    if "non_existent" in path or "99999" in path:
        # For endpoints testing non-existent resources, filter out 404 errors
        non_404_errors = [
            error
            for error in console_error_checker.errors
            if "404 (Not Found)" not in error
        ]
        assert len(non_404_errors) == 0, (
            f"Found {len(non_404_errors)} non-404 console errors:\n"
            + "\n".join(non_404_errors)
        )
    elif path == "/tools":
        # The tools page has schema rendering that tries to load schema_doc.css
        # which may not exist in test environments
        non_css_404_errors = [
            error
            for error in console_error_checker.errors
            if not ("404 (Not Found)" in error and "schema_doc.css" in error)
        ]
        assert len(non_css_404_errors) == 0, (
            f"Found {len(non_css_404_errors)} non-CSS 404 console errors:\n"
            + "\n".join(non_css_404_errors)
        )
    else:
        console_error_checker.assert_no_errors()


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
