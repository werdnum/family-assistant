"""Playwright tests for the React-based tools UI."""

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_page_loads(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test that the tools page loads successfully."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for the page to be fully loaded

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-app-ready') === 'true';
        }""",
        timeout=15000,
    )

    # Verify page has the correct title
    title = await page.title()
    assert "Tools" in title, f"Page title should contain 'Tools', got: {title}"

    # Check for the navigation header
    nav_header = await page.wait_for_selector(
        "header nav", state="visible", timeout=5000
    )
    assert nav_header is not None, "Navigation header should be visible"

    # Check that Tools link exists in navigation
    # First open the Internal menu since Tools is in a dropdown
    internal_menu = page.get_by_role("button", name="Internal", exact=True)
    if await internal_menu.is_visible():
        await internal_menu.click()
        # Wait for the Tools link to become visible in the dropdown
        await page.wait_for_selector(
            "nav a:has-text('Tools')", state="visible", timeout=5000
        )

    # Now check for the Tools link
    tools_link = await page.query_selector("nav a:has-text('Tools')")
    assert tools_link is not None, "Tools link should be present in navigation"

    # Check for the main tools container
    tools_container = await page.wait_for_selector(
        ".tools-container", state="visible", timeout=5000
    )
    assert tools_container is not None, "Tools container should be visible"

    # Check for the tools header - updated to match actual text
    header = await page.wait_for_selector(
        "h1:has-text('Tool Explorer')", state="visible", timeout=5000
    )
    assert header is not None, "Tool Explorer header should be visible"

    # Take screenshot of tools page
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "tools-list", viewport)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_list_loads(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the tools list loads and displays available tools."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-app-ready') === 'true';
        }""",
        timeout=15000,
    )

    await expect(page.get_by_role("heading", name="Available Tools")).to_be_visible(
        timeout=10000
    )
    await expect(
        page.get_by_role("button", name=re.compile(r"^list_notes\b"))
    ).to_be_visible()
    assert await page.locator(".tool-item").count() > 0
    await expect(page.locator(".tools-error")).to_have_count(0)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that clicking on a tool shows the execution interface."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-app-ready') === 'true';
        }""",
        timeout=15000,
    )

    tool = page.get_by_role("button", name=re.compile(r"^list_notes\b"))
    await expect(tool).to_be_visible(timeout=10000)
    await tool.click()

    await expect(page.get_by_role("heading", name="list_notes")).to_be_visible()
    await expect(page.locator(".json-editor-container")).not_to_be_empty()
    await page.get_by_role("button", name="Execute Tool").click()

    await expect(page.get_by_text("✓ Success")).to_be_visible(timeout=10000)
    result_text = await page.locator(".execution-results pre").inner_text()
    assert isinstance(json.loads(result_text), list)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_responsive_design(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the tools UI is responsive and works on mobile viewport."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-app-ready') === 'true';
        }""",
        timeout=15000,
    )

    await expect(page.get_by_role("heading", name="Tool Explorer")).to_be_visible()
    await expect(
        page.get_by_role("button", name=re.compile(r"^list_notes\b"))
    ).to_be_visible(timeout=10000)
    sidebar = await page.locator(".tools-sidebar").bounding_box()
    main = await page.locator(".tools-main").bounding_box()
    assert sidebar is not None and main is not None
    assert main["y"] >= sidebar["y"] + sidebar["height"]
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )

    # Reset viewport
    await page.set_viewport_size({"width": 1280, "height": 720})


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_no_javascript_errors(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the tools page loads without JavaScript errors."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Collect console errors
    console_errors = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg) if msg.type == "error" else None,
    )

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-app-ready') === 'true';
        }""",
        timeout=15000,
    )

    await expect(
        page.get_by_role("button", name=re.compile(r"^list_notes\b"))
    ).to_be_visible(timeout=10000)

    critical_errors = [
        err for err in console_errors if "sourcemap" not in err.text.lower()
    ]

    assert len(critical_errors) == 0, (
        f"Page should not have critical JavaScript errors, but found: {[err.text for err in critical_errors]}"
    )
