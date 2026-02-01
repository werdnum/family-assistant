"""
End-to-end tests for basic UI endpoint accessibility.
Tests both basic HTTP accessibility and browser-based rendering.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from family_assistant.assistant import Assistant
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.auth import AUTH_ENABLED
from tests.functional.web.conftest import ConsoleErrorCollector, WebTestFixture
from tests.functional.web.pages import BasePage

# Base UI endpoints accessible regardless of auth state (or will redirect to login if auth is on)
# For pages that expect data (e.g., editing a specific note), we test with a
# non-existent item to ensure it returns a client error (like 404) rather than a server error (500).
BASE_UI_ENDPOINTS = [
    ("/", "Root Page (Redirects to Chat)"),
    ("/notes", "Notes List Page"),
    ("/notes/add", "Add Note Form Page"),
    ("/notes/edit/non_existent_note_for_test", "Edit Non-Existent Note Form Page"),
    ("/docs/", "Documentation Index Page (may redirect)"),
    ("/docs/USER_GUIDE.md", "USER_GUIDE.md Document Page"),
    ("/history", "Message History Page"),
    ("/tools", "Available Tools Page"),
    ("/tasks", "Tasks List Page"),
    ("/vector-search", "Vector Search Page"),
    ("/documents", "Documents List Page"),
    ("/documents/upload", "Document Upload Page"),
    ("/chat", "Chat Interface Page"),
    ("/settings/tokens", "Manage API Tokens UI Page"),
    ("/events", "Events List Page"),
    ("/events/non_existent_event", "Event Detail Page"),
    ("/event-listeners", "Event Listeners List Page"),
    ("/event-listeners/new", "Create Event Listener Page"),
    ("/event-listeners/99999", "Event Listener Detail Page"),
    ("/errors/", "Error Logs List Page"),
]

# UI endpoints related to authentication, typically only active if AUTH_ENABLED is true
AUTH_UI_ENDPOINTS = [
    ("/auth/login", "Login Page"),
    # Add other auth-related UI GET endpoints here if necessary, e.g., a registration page
]

ALL_UI_ENDPOINTS_TO_TEST = BASE_UI_ENDPOINTS
if AUTH_ENABLED:
    ALL_UI_ENDPOINTS_TO_TEST.extend(AUTH_UI_ENDPOINTS)

# Extended endpoints with expected elements for Playwright tests.
# Data structure: (path, description, expected_elements, screenshot_name)
BASE_UI_ENDPOINTS_WITH_ELEMENTS = [
    ("/", "Landing Page", ["h1", "form", "input"], "root"),
    ("/notes", "Notes List Page", ["h1", "nav"], "notes"),
    ("/notes/add", "Add Note Form Page", ["form", "input", "button"], "notes-add"),
    (
        "/notes/edit/non_existent_note_for_test",
        "Edit Non-Existent Note Form Page",
        ["body"],
        "notes-edit-404",
    ),
    ("/docs/", "Documentation Index Page", ["h1", "a"], "docs"),
    (
        "/docs/USER_GUIDE.md",
        "USER_GUIDE.md Document Page",
        ["h1", "p"],
        "docs-user-guide",
    ),
    ("/history", "Message History Page", ["h1"], "history"),
    ("/tools", "Available Tools Page", ["h1", "article"], "tools"),
    ("/tasks", "Tasks List Page", ["h1"], "tasks"),
    ("/vector-search", "Vector Search Page", ["h1", "form", "input"], "vector-search"),
    ("/documents/upload", "Document Upload Page", ["h1", "form"], "documents-upload"),
    # Note: tokens page may have issues with auth disabled, but we test it anyway
    ("/settings/tokens", "Manage API Tokens Page", ["h1"], "settings-tokens"),
    ("/events", "Events List Page", ["main h1"], "events"),
    (
        "/events/non_existent_event",
        "Event Detail Page",
        ["body"],
        "events-404",
    ),  # May show error page
    ("/event-listeners", "Event Listeners List Page", ["main h1"], "event-listeners"),
    (
        "/event-listeners/new",
        "Create Event Listener Page",
        ["main h1", "form"],
        "event-listeners-new",
    ),
    (
        "/event-listeners/99999",
        "Event Listener Detail Page",
        ["body"],
        "event-listeners-404",
    ),  # May show error page
    ("/errors/", "Error Logs List Page", ["main h1"], "errors"),
]


@pytest.mark.asyncio
async def test_ui_endpoint_accessibility(web_only_assistant: Assistant) -> None:
    """
    Tests that all UI endpoints are accessible and do not return server errors.
    This single test checks all endpoints to avoid the overhead of setting up
    the web_only_assistant fixture multiple times.
    """
    # Use the fastapi app directly - the web_only_assistant fixture ensures it's properly configured
    # with database setup and all dependencies
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        failures = []
        for path, description in ALL_UI_ENDPOINTS_TO_TEST:
            try:
                response = await client.get(path)
                if response.status_code >= 500:
                    failures.append(
                        f"UI endpoint '{description}' at '{path}' returned {response.status_code}. "
                        f"Response text (first 500 chars): {response.text[:500]}"
                    )
            except Exception as e:
                failures.append(
                    f"UI endpoint '{description}' at '{path}' raised exception: {type(e).__name__}: {str(e)}"
                )

        if failures:
            pytest.fail("The following endpoints failed:\n" + "\n".join(failures))


async def check_endpoint(
    browser: Any,  # noqa: ANN401  # playwright browser object
    base_url: str,
    endpoint_info: tuple[str, str, list[str], str],
    take_screenshot: Callable[[Any, str], Awaitable[None]] | None = None,
) -> tuple[list[str], list[str]]:
    """Check a single endpoint and return failures and warnings."""
    path, description, expected_elements, screenshot_name = endpoint_info
    failures = []
    warnings = []

    # Create a new page for this endpoint check
    page = await browser.new_page()
    try:
        base_page = BasePage(page, base_url)

        # Navigate to the endpoint
        # We try to wait for the React app to be ready, but fall back if it's not a React app
        print(f"Test: Navigating to {base_url}{path}")
        response = await base_page.navigate_to(path, wait_for_app_ready=False)
        print(f"Response status: {response.status if response else 'None'}")

        # Try to wait for React app readiness if applicable
        with contextlib.suppress(Exception):
            # Wait for data-app-ready="true" to appear, indicating React has mounted and finished initial loading
            await page.wait_for_selector('[data-app-ready="true"]', timeout=5000)

        # Wait for any generic loading indicators to disappear
        with contextlib.suppress(Exception):
            loading_indicator = page.locator(
                '[data-loading-indicator="true"], .loading, .spinner'
            )
            if await loading_indicator.count() > 0:
                await loading_indicator.first.wait_for(state="hidden", timeout=5000)

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

        # Check for expected elements
        for selector in expected_elements:
            is_visible = await base_page.is_element_visible(selector)
            if not is_visible:
                warnings.append(
                    f"Expected element '{selector}' not found on {description} at {path}"
                )

        # Take screenshot if requested
        if take_screenshot and screenshot_name:
            await take_screenshot(page, screenshot_name)
    finally:
        await page.close()

    return failures, warnings


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_ui_endpoint_accessibility_playwright(
    web_test_with_console_check: WebTestFixture,
    take_screenshot: Callable[[Any, str], Awaitable[None]],
) -> None:
    """
    Test that all UI endpoints are accessible via Playwright and render without errors.
    Uses parallel execution to speed up testing of multiple endpoints.
    """
    browser = web_test_with_console_check.page.context.browser
    base_url = web_test_with_console_check.base_url

    # Split endpoints into batches for parallel processing
    # Process 5 endpoints at a time to avoid overwhelming the server
    batch_size = 5
    all_failures = []
    all_warnings = []

    for i in range(0, len(BASE_UI_ENDPOINTS_WITH_ELEMENTS), batch_size):
        batch = BASE_UI_ENDPOINTS_WITH_ELEMENTS[i : i + batch_size]

        # Run endpoint checks in parallel for this batch
        results = await asyncio.gather(*[
            check_endpoint(browser, base_url, endpoint_info, take_screenshot)
            for endpoint_info in batch
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
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test that all navigation links in the UI work correctly.

    This is a smoke test that discovers all nav links dynamically and tests each one
    in isolation to avoid stale element references and ensure proper testing.
    """
    browser = web_test_with_console_check.page.context.browser
    assert browser is not None, "Browser not available"
    base_url = web_test_with_console_check.base_url

    # Discover all navigation links using the existing page
    page = web_test_with_console_check.page
    base_page = BasePage(page, base_url)

    await base_page.navigate_to("/notes")
    await base_page.wait_for_load()
    await page.wait_for_selector("nav a", timeout=10000)

    # Collect all navigation links (data only, not element references)
    nav_links = await page.locator("nav a").all()
    link_data = []
    for link in nav_links:
        href = await link.get_attribute("href")
        text = await link.text_content()
        if href and not href.startswith("http"):
            link_data.append({"href": href, "text": text or href})

    if not link_data:
        pytest.fail("No internal navigation links found")

    # Test each link in isolation with a fresh page
    failures = []
    for link_info in link_data:
        test_page = await browser.new_page()
        try:
            # Set up console error checking for this page
            page_error_checker = ConsoleErrorCollector(test_page)

            test_base_page = BasePage(test_page, base_url)

            # Navigate to notes page
            await test_base_page.navigate_to("/notes")
            await test_base_page.wait_for_load()
            await test_page.wait_for_selector("nav a", timeout=10000)

            # Find and click the specific link by href
            target_link = test_page.locator(f'nav a[href="{link_info["href"]}"]')
            await target_link.click()

            # Wait for navigation based on destination
            if link_info["href"] == "/chat":
                await test_page.wait_for_selector(
                    '[data-app-ready="true"]', timeout=10000
                )
                await test_page.wait_for_selector(
                    "main .flex.flex-1.flex-col", timeout=5000
                )
            else:
                # For other pages, just wait for DOM content loaded
                # Don't use networkidle as SSE connections prevent it
                await test_page.wait_for_load_state("domcontentloaded", timeout=10000)

            # Verify navigation succeeded
            current_url = test_page.url
            if base_url not in current_url:
                failures.append(
                    f"Navigation failed for link '{link_info['text']}' "
                    f"({link_info['href']}): URL is {current_url}"
                )

            # Check for console errors on the destination page
            if page_error_checker.errors:
                failures.append(
                    f"Console errors on '{link_info['text']}' ({link_info['href']}): "
                    + ", ".join(page_error_checker.errors)
                )
        except Exception as e:
            failures.append(
                f"Link '{link_info['text']}' ({link_info['href']}) failed: {e}"
            )
        finally:
            await test_page.close()

    # Report all failures together
    if failures:
        pytest.fail(
            f"Navigation test failed for {len(failures)}/{len(link_data)} links:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_responsive_design_mobile(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test that pages work on mobile viewport sizes."""
    page = web_test_with_console_check.page
    base_url = web_test_with_console_check.base_url
    base_page = BasePage(page, base_url)

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Test a few key pages
    mobile_test_pages = [
        ("/notes", "Notes"),
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


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_form_interactions(
    web_test_with_console_check: WebTestFixture,
) -> None:
    """Test basic form interactions work without errors."""
    page = web_test_with_console_check.page
    base_url = web_test_with_console_check.base_url
    base_page = BasePage(page, base_url)

    # Navigate to vector search page (has a simple search form)
    print(f"Navigating to vector search page at {base_url}")
    await base_page.navigate_to("/vector-search")
    await base_page.wait_for_load()

    # Check if we got HTML or JSON
    page_content = await page.content()
    print(f"Page content type check - starts with: {page_content[:100]}")

    # Wait for search input to be available
    await page.wait_for_selector(
        'input[type="text"], input[type="search"]',
        timeout=10000,  # 10 seconds should be plenty
    )

    # Find search input
    search_input = page.locator('input[type="text"], input[type="search"]').first
    assert await search_input.is_visible(), "Search input not found"

    # Type in search box
    await search_input.fill("test search query")

    # Find and click search button
    search_button = page.locator('button[type="submit"], button:has-text("Search")')
    if await search_button.count() > 0:
        await search_button.first.click()
        # No need to wait - this test only checks that the button click doesn't cause errors


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_loading_states(
    web_test_readonly_with_console_check: WebTestFixture,
) -> None:
    """Test that pages show appropriate loading states."""
    page = web_test_readonly_with_console_check.page
    base_url = web_test_readonly_with_console_check.base_url
    base_page = BasePage(page, base_url)

    # Navigate to tasks page (likely to have loading states)
    await base_page.navigate_to("/tasks")

    # Check for loading indicators or quick page load
    # Most pages should either show content quickly or show a loader
    try:
        # Wait for either main content (h1, task items) or loading indicator
        await page.wait_for_selector(
            "h1, .task-item, .loading, .spinner, [role='progressbar']",
            timeout=3000,
        )
    except Exception:
        pytest.fail("Page did not show content or loading state within 3 seconds")

    # Wait for final load
    await base_page.wait_for_load()
