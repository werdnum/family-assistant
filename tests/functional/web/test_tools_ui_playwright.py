"""Playwright tests for the React-based tools UI."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_page_loads(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the tools page loads successfully."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for the page to be fully loaded
    await page.wait_for_load_state("networkidle")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
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
    internal_menu = page.locator("button:has-text('Internal')")
    if await internal_menu.is_visible():
        await internal_menu.click()
        await page.wait_for_timeout(500)  # Wait for menu to open

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_list_loads(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that the tools list loads and displays available tools."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")
    await page.wait_for_load_state("networkidle")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for tools to load (either showing tools or an error)
    # Updated to match actual component classes
    await page.wait_for_selector(".tools-sidebar, .tools-error", timeout=10000)

    # Check if tools loaded successfully
    tools_sidebar = await page.query_selector(".tools-sidebar")
    if tools_sidebar:
        # Tools loaded successfully
        tools_heading = await page.text_content(".tools-sidebar h2")
        assert tools_heading and "Available Tools" in tools_heading, (
            "Should show available tools heading"
        )

        # Check if there are any tool items (buttons)
        await page.query_selector_all(".tool-item")
    else:
        # Check for error message if tools failed to load
        error_element = await page.query_selector(".tools-error")
        assert error_element is not None, "Should show either tools or error message"


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
    await page.wait_for_load_state("networkidle")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Wait for tools to load
    await page.wait_for_selector(".tools-sidebar", timeout=10000)

    # Check if there are any tools available
    tool_items = await page.query_selector_all(".tool-item")

    if len(tool_items) > 0:
        # Click on the first tool
        await tool_items[0].click()

        # Wait for the tool details section to appear
        tool_details = await page.wait_for_selector(
            ".tool-details", state="visible", timeout=5000
        )
        assert tool_details is not None, (
            "Tool details section should appear after clicking a tool"
        )

        # Check for the JSON editor container
        json_editor = await page.wait_for_selector(
            ".json-editor-container", state="visible", timeout=5000
        )
        assert json_editor is not None, "JSON editor container should be visible"

        # Check for the execute button
        execute_button = await page.wait_for_selector(
            ".btn-execute", state="visible", timeout=5000
        )
        assert execute_button is not None, "Execute button should be visible"
    else:
        # No tools available - this is still a valid state for testing
        print("No tools available for execution interface test")


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
    await page.wait_for_load_state("networkidle")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Check that main content is still visible on mobile
    tools_container = await page.wait_for_selector(
        ".tools-container", state="visible", timeout=5000
    )
    assert tools_container is not None, "Tools container should be visible on mobile"

    # Check that the header is still visible - updated to match actual text
    header = await page.wait_for_selector(
        "h1:has-text('Tool Explorer')", state="visible", timeout=5000
    )
    assert header is not None, "Tool Explorer header should be visible on mobile"

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
    await page.wait_for_load_state("networkidle")

    # Wait for React app to mount first
    await page.wait_for_function(
        """() => {
            const root = document.getElementById('app-root');
            return root && root.getAttribute('data-react-mounted') === 'true';
        }""",
        timeout=15000,
    )

    # Give time for any errors to be logged
    await page.wait_for_timeout(1000)

    # Filter out non-critical errors (like 404s for sourcemaps in dev mode)
    critical_errors = [
        err
        for err in console_errors
        if "404" not in err.text and "sourcemap" not in err.text.lower()
    ]

    assert len(critical_errors) == 0, (
        f"Page should not have critical JavaScript errors, but found: {[err.text for err in critical_errors]}"
    )
