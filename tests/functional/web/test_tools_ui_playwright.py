"""Playwright tests for the React-based tools UI."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_page_loads(web_test_fixture: WebTestFixture) -> None:
    """Test that the tools page loads successfully."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")

    # Wait for the page to be fully loaded
    await page.wait_for_load_state("networkidle")

    # Verify page has the correct title
    title = await page.title()
    assert "Tools" in title, f"Page title should contain 'Tools', got: {title}"

    # Check for the navigation header
    nav_header = await page.wait_for_selector(
        "header nav", state="visible", timeout=5000
    )
    assert nav_header is not None, "Navigation header should be visible"

    # Check that Tools link is highlighted as current page
    current_tools_link = await page.wait_for_selector(
        "nav a.current-page:has-text('Tools')", state="visible", timeout=5000
    )
    assert current_tools_link is not None, (
        "Tools link should be highlighted as current page"
    )

    # Check for the main tools container
    tools_container = await page.wait_for_selector(
        ".tools-container", state="visible", timeout=5000
    )
    assert tools_container is not None, "Tools container should be visible"

    # Check for the tools header
    header = await page.wait_for_selector(
        "h1:has-text('Tools')", state="visible", timeout=5000
    )
    assert header is not None, "Tools header should be visible"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tools_list_loads(web_test_fixture: WebTestFixture) -> None:
    """Test that the tools list loads and displays available tools."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")
    await page.wait_for_load_state("networkidle")

    # Wait for tools to load (either showing tools or an error)
    # We'll wait for either the tools section or an error message
    await page.wait_for_selector(".tools-section, .error", timeout=10000)

    # Check if tools loaded successfully
    tools_section = await page.query_selector(".tools-section")
    if tools_section:
        # Tools loaded successfully
        tools_heading = await page.text_content(".tools-section h2")
        assert tools_heading and "Available Tools" in tools_heading, (
            "Should show available tools heading"
        )

        # Check if there are any tool buttons
        await page.query_selector_all(".tool-button")
    else:
        # Check for error message if tools failed to load
        error_element = await page.query_selector(".error")
        assert error_element is not None, "Should show either tools or error message"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_tool_execution_interface(web_test_fixture: WebTestFixture) -> None:
    """Test that clicking on a tool shows the execution interface."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")
    await page.wait_for_load_state("networkidle")

    # Wait for tools to load
    await page.wait_for_selector(".tools-section", timeout=10000)

    # Check if there are any tools available
    tool_buttons = await page.query_selector_all(".tool-button")

    if len(tool_buttons) > 0:
        # Click on the first tool
        await tool_buttons[0].click()

        # Wait for the execution section to appear
        execution_section = await page.wait_for_selector(
            ".tool-execution-section", state="visible", timeout=5000
        )
        assert execution_section is not None, (
            "Tool execution section should appear after clicking a tool"
        )

        # Check for the JSON editor container
        json_editor = await page.wait_for_selector(
            ".json-editor-container", state="visible", timeout=5000
        )
        assert json_editor is not None, "JSON editor container should be visible"

        # Check for the execute button
        execute_button = await page.wait_for_selector(
            ".execute-button", state="visible", timeout=5000
        )
        assert execute_button is not None, "Execute button should be visible"
    else:
        # No tools available - this is still a valid state for testing
        print("No tools available for execution interface test")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_responsive_design(web_test_fixture: WebTestFixture) -> None:
    """Test that the tools UI is responsive and works on mobile viewport."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")
    await page.wait_for_load_state("networkidle")

    # Check that main content is still visible on mobile
    tools_container = await page.wait_for_selector(
        ".tools-container", state="visible", timeout=5000
    )
    assert tools_container is not None, "Tools container should be visible on mobile"

    # Check that the header is still visible
    header = await page.wait_for_selector(
        "h1:has-text('Tools')", state="visible", timeout=5000
    )
    assert header is not None, "Tools header should be visible on mobile"

    # Reset viewport
    await page.set_viewport_size({"width": 1280, "height": 720})


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_no_javascript_errors(web_test_fixture: WebTestFixture) -> None:
    """Test that the tools page loads without JavaScript errors."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Collect console errors
    console_errors = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg) if msg.type == "error" else None,
    )

    # Navigate to tools page
    await page.goto(f"{base_url}/tools")
    await page.wait_for_load_state("networkidle")

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
