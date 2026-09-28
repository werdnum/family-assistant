"""
End-to-end tests for basic UI endpoint accessibility.
Tests both basic HTTP accessibility and browser-based rendering.
"""

import asyncio
import re
from typing import Any

import httpx
import pytest
from playwright.async_api import Page, expect

from family_assistant.assistant import Assistant
from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.auth import AUTH_ENABLED
from tests.functional.web.conftest import ConsoleErrorCollector, WebTestFixture
from tests.functional.web.pages import BasePage

# Base UI endpoints accessible regardless of auth state.
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
    ("/settings/accounts", "Connected Accounts UI Page"),
    ("/events", "Events List Page"),
    ("/events/non_existent_event", "Event Detail Page"),
    ("/errors", "Error Logs List Page"),
]

# UI endpoints related to authentication, typically only active if AUTH_ENABLED is true
AUTH_UI_ENDPOINTS = [
    ("/auth/login", "Login Page"),
    # Add other auth-related UI GET endpoints here if necessary, e.g., a registration page
]

ALL_UI_ENDPOINTS_TO_TEST = BASE_UI_ENDPOINTS
if AUTH_ENABLED:
    ALL_UI_ENDPOINTS_TO_TEST.extend(AUTH_UI_ENDPOINTS)


async def _test_navigation_link(
    test_page: Page,
    base_url: str,
    link_info: dict[str, str],
    failures: list[str],
) -> None:
    page_error_checker = ConsoleErrorCollector(test_page)
    test_base_page = BasePage(test_page, base_url)

    await test_base_page.navigate_to("/notes")
    await test_base_page.wait_for_load()
    await test_page.wait_for_selector("nav a", timeout=10000)

    target_link = test_page.locator(f'nav a[href="{link_info["href"]}"]')
    await target_link.click()
    expected_url = re.compile(
        rf"^{re.escape(base_url + link_info['href'].rstrip('/'))}/?(?:\?.*)?$"
    )
    await expect(test_page).to_have_url(expected_url)
    await expect(test_page.locator('[data-app-ready="true"]')).to_be_visible()

    if page_error_checker.errors:
        failures.append(
            f"Console errors on '{link_info['text']}' ({link_info['href']}): "
            + ", ".join(page_error_checker.errors)
        )


# Extended endpoints with expected elements for Playwright tests
BASE_UI_ENDPOINTS_WITH_ELEMENTS = [
    ("/", "Root Redirect Page", ["body"]),  # Now redirects to /chat
    ("/notes", "Notes List Page", ["h1", "nav"]),
    ("/notes/add", "Add Note Form Page", ["form", "input", "button"]),
    (
        "/notes/edit/non_existent_note_for_test",
        "Edit Non-Existent Note Form Page",
        ["body"],
    ),
    ("/docs/", "Documentation Index Page", ["h1", "a"]),
    ("/docs/USER_GUIDE.md", "USER_GUIDE.md Document Page", ["h1", "p"]),
    ("/history", "Message History Page", ["h1"]),
    ("/tools", "Available Tools Page", ["h1", "h2"]),
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
    ("/errors", "Error Logs List Page", ["main h1"]),
]


@pytest.mark.asyncio
async def test_ui_endpoint_accessibility(web_only_assistant: Assistant) -> None:
    """
    Tests that all supported UI endpoints serve HTML.
    This single test checks all endpoints to avoid the overhead of setting up
    the web_only_assistant fixture multiple times.
    """
    # Use the fastapi app directly - the web_only_assistant fixture ensures it's properly configured
    # with database setup and all dependencies
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=True
    ) as client:
        failures = []
        for path, description in ALL_UI_ENDPOINTS_TO_TEST:
            try:
                response = await client.get(path)
                if (
                    response.status_code != 200
                    or "text/html" not in response.headers.get("content-type", "")
                ):
                    failures.append(
                        f"UI endpoint '{description}' at '{path}' returned "
                        f"{response.status_code} {response.headers.get('content-type')}. "
                        f"Response text (first 500 chars): {response.text[:500]}"
                    )
            except Exception as e:
                failures.append(
                    f"UI endpoint '{description}' at '{path}' raised exception: {type(e).__name__}: {e!s}"
                )

        if failures:
            pytest.fail("The following endpoints failed:\n" + "\n".join(failures))


async def check_endpoint(
    browser: Any,  # noqa: ANN401  # playwright browser object
    base_url: str,
    endpoint_info: tuple[str, str, list[str]],
) -> list[str]:
    """Check a single endpoint and return failures."""
    path, description, expected_elements = endpoint_info
    failures = []

    # Create a new page for this endpoint check
    page = await browser.new_page()
    page_error_checker = ConsoleErrorCollector(page)
    try:
        base_page = BasePage(page, base_url)

        # Navigate to the endpoint (using fast DOM load only, not React-specific)
        # These are diverse endpoints, not all are React apps
        print(f"Test: Navigating to {base_url}{path}")
        response = await base_page.navigate_to(path, wait_for_app_ready=False)
        print(f"Response status: {response.status if response else 'None'}")

        # Check response status
        if response is None:
            failures.append(f"Failed to navigate to {path}")
            return failures

        # Log error responses for debugging
        if response.status >= 400:
            print(f"Error Response status: {response.status}")
            page_content = await page.content()
            print(f"Error Page content preview: {page_content[:500]}...")

        if response.status != 200:
            failures.append(
                f"UI endpoint '{description}' at '{path}' returned: {response.status}"
            )
            return failures

        # Check for expected elements
        for selector in expected_elements:
            is_visible = await base_page.is_element_visible(selector)
            if not is_visible:
                failures.append(
                    f"Expected element '{selector}' not found on {description} at {path}"
                )
    finally:
        await page.close()
        failures.extend(
            f"Console error on {description} at {path}: {error}"
            for error in page_error_checker.errors
        )

    return failures


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_ui_endpoint_accessibility_playwright(
    web_test_readonly_with_console_check: WebTestFixture,
) -> None:
    """
    Test that all UI endpoints are accessible via Playwright and render without errors.
    Uses parallel execution to speed up testing of multiple endpoints.
    """
    browser = web_test_readonly_with_console_check.page.context.browser
    base_url = web_test_readonly_with_console_check.base_url

    # Split endpoints into batches for parallel processing
    # Process 5 endpoints at a time to avoid overwhelming the server
    batch_size = 5
    all_failures = []

    for i in range(0, len(BASE_UI_ENDPOINTS_WITH_ELEMENTS), batch_size):
        batch = BASE_UI_ENDPOINTS_WITH_ELEMENTS[i : i + batch_size]

        # Run endpoint checks in parallel for this batch
        results = await asyncio.gather(*[
            check_endpoint(browser, base_url, endpoint_info) for endpoint_info in batch
        ])

        # Collect results
        for failures in results:
            all_failures.extend(failures)

    # Report all failures
    if all_failures:
        pytest.fail("The following endpoints failed:\n" + "\n".join(all_failures))


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_links_work(
    web_test_readonly_with_console_check: WebTestFixture,
) -> None:
    """Test that all navigation links in the UI work correctly.

    This is a smoke test that discovers all nav links dynamically and tests each one
    in isolation to avoid stale element references and ensure proper testing.
    """
    browser = web_test_readonly_with_console_check.page.context.browser
    assert browser is not None, "Browser not available"
    base_url = web_test_readonly_with_console_check.base_url

    # Discover all navigation links using the existing page
    page = web_test_readonly_with_console_check.page
    base_page = BasePage(page, base_url)

    await base_page.navigate_to("/notes")
    await base_page.wait_for_load()
    await page.wait_for_selector("nav a", timeout=10000)

    # Collect all navigation links (data only, not element references)
    nav_links = await page.locator("nav a").all()
    link_data: list[dict[str, str]] = []
    for link in nav_links:
        href = await link.get_attribute("href")
        text = await link.text_content()
        if href and not href.startswith("http"):
            link_data.append({"href": href, "text": text or href})

    if not link_data:
        pytest.fail("No internal navigation links found")

    # Test each link in isolation with a fresh page
    failures: list[str] = []
    for link_info in link_data:
        test_page = await browser.new_page()
        try:
            await _test_navigation_link(test_page, base_url, link_info, failures)
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
    web_test_readonly_with_console_check: WebTestFixture,
) -> None:
    """Test that pages work on mobile viewport sizes."""
    page = web_test_readonly_with_console_check.page
    base_url = web_test_readonly_with_console_check.base_url
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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tasks_page_renders(
    web_test_readonly_with_console_check: WebTestFixture,
) -> None:
    """Test that the task queue renders after loading."""
    page = web_test_readonly_with_console_check.page
    base_url = web_test_readonly_with_console_check.base_url
    base_page = BasePage(page, base_url)

    await base_page.navigate_to("/tasks")
    await expect(page.locator("main h1")).to_have_text("Task Queue")
