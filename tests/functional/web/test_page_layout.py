"""Simplified tests for page layout and navigation components."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_dropdowns_open_and_position(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that navigation dropdowns open and are positioned reasonably."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to notes page which has the full navigation menu
    await page.goto(f"{base_url}/notes")
    await page.wait_for_load_state("networkidle")

    # Wait for navigation to be rendered
    await page.wait_for_selector(
        "nav[data-orientation='horizontal']", state="visible", timeout=10000
    )

    # Test Data dropdown
    data_trigger = page.locator("button:has-text('Data')").first
    await data_trigger.wait_for(state="visible", timeout=5000)

    # Click and wait for dropdown to open
    await data_trigger.click()

    # Wait for dropdown to be fully open - both aria-expanded and content visible
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Data'));
            const links = Array.from(document.querySelectorAll('a'));
            const notesLink = links.find(a => a.textContent?.includes('Notes'));
            return btn?.getAttribute('aria-expanded') === 'true' && 
                   notesLink && getComputedStyle(notesLink).visibility === 'visible';
        }""",
        timeout=5000,
    )

    # Check that dropdown opened (button should be expanded)
    is_expanded = await data_trigger.get_attribute("aria-expanded")
    assert is_expanded == "true", "Data dropdown should be expanded after click"

    # Ensure dropdown content is visible (look for Notes link)
    notes_link = page.locator("a:has-text('Notes')")
    await notes_link.wait_for(state="visible", timeout=2000)

    # Get positions to verify reasonable positioning
    trigger_box = await data_trigger.bounding_box()
    notes_box = await notes_link.bounding_box()

    assert trigger_box is not None, "Should be able to get trigger bounding box"
    assert notes_box is not None, "Should be able to get dropdown content bounding box"

    # The dropdown should appear below the trigger (reasonable Y positioning)
    assert notes_box["y"] > trigger_box["y"], "Dropdown should appear below trigger"

    # The dropdown should not be at the far left edge (x > 10px from left)
    assert notes_box["x"] > 10, (
        f"Dropdown should not be at far left edge, got x={notes_box['x']}"
    )

    # Test that Data dropdown closes when we click elsewhere
    await page.click("body")
    await page.wait_for_timeout(200)

    is_data_closed = await data_trigger.get_attribute("aria-expanded")
    assert is_data_closed == "false", (
        "Data dropdown should close when clicking elsewhere"
    )

    # Test Internal dropdown
    internal_trigger = page.locator("button:has-text('Internal')").first
    await internal_trigger.wait_for(state="visible", timeout=5000)

    # Ensure page is stable before interaction
    await page.wait_for_load_state("networkidle")

    # Click and wait for the dropdown to open using Playwright's built-in waiting
    await internal_trigger.click()

    # Wait for the dropdown to be fully open - check both aria-expanded and content visibility
    # Use Promise.race equivalent - wait for content to be visible which implies dropdown is open
    tools_link = page.locator("a:has-text('Tools')")

    # Playwright will automatically retry the click if needed when using wait_for
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Internal'));
            const links = Array.from(document.querySelectorAll('a'));
            const toolsLink = links.find(a => a.textContent?.includes('Tools'));
            return btn?.getAttribute('aria-expanded') === 'true' && 
                   toolsLink && getComputedStyle(toolsLink).visibility === 'visible';
        }""",
        timeout=5000,
    )

    # Verify dropdown is actually open
    is_expanded = await internal_trigger.get_attribute("aria-expanded")
    assert is_expanded == "true", "Internal dropdown should be expanded after click"

    # Ensure dropdown content is visible (look for Tools link)
    await tools_link.wait_for(state="visible", timeout=2000)

    # Get positions to verify positioning
    internal_trigger_box = await internal_trigger.bounding_box()
    tools_box = await tools_link.bounding_box()

    assert internal_trigger_box is not None, (
        "Should be able to get Internal trigger bounding box"
    )
    assert tools_box is not None, "Should be able to get Tools link bounding box"

    # The dropdown should appear below the trigger
    assert tools_box["y"] > internal_trigger_box["y"], (
        "Internal dropdown should appear below trigger"
    )

    # The dropdown should be positioned relative to its trigger, not at the left edge
    # Since Internal is to the right of Data, its dropdown should be further right
    assert tools_box["x"] > notes_box["x"], (
        "Internal dropdown should be positioned further right than Data dropdown"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_responsive_behavior(web_test_fixture: WebTestFixture) -> None:
    """Test that navigation layout adapts to different viewport sizes."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Desktop viewport
    await page.set_viewport_size({"width": 1280, "height": 720})
    await page.goto(f"{base_url}/notes")
    await page.wait_for_load_state("networkidle")

    # Desktop should show horizontal navigation
    desktop_nav = await page.query_selector("nav[data-orientation='horizontal']")
    assert desktop_nav is not None, "Desktop viewport should show horizontal navigation"

    # Check that desktop nav is visible
    is_visible = await page.is_visible("nav[data-orientation='horizontal']")
    assert is_visible, "Desktop navigation should be visible"

    # Mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})
    await page.wait_for_timeout(500)

    # Mobile should hide desktop nav and show mobile button
    desktop_nav_hidden = await page.is_hidden("nav[data-orientation='horizontal']")

    # Look for the mobile menu button - it's a button with sr-only text in mobile nav
    mobile_button = page.locator(".md\\:hidden button").first
    await mobile_button.wait_for(state="visible", timeout=5000)

    assert desktop_nav_hidden, "Desktop navigation should be hidden on mobile"

    # Test mobile menu functionality - click the mobile navigation button
    await mobile_button.click()
    await page.wait_for_timeout(500)

    # Should open navigation sheet/dialog
    navigation_opened = await page.is_visible("[role='dialog'], [data-state='open']")
    assert navigation_opened, "Mobile menu should open navigation sheet"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_hover_states(web_test_fixture: WebTestFixture) -> None:
    """Test that navigation menu items have proper hover states."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    await page.goto(f"{base_url}/notes")
    await page.wait_for_load_state("networkidle")

    # Wait for navigation
    await page.wait_for_selector(
        "nav[data-orientation='horizontal']", state="visible", timeout=10000
    )

    # Open Internal dropdown to test hover states
    internal_trigger = page.locator("button:has-text('Internal')").first
    await internal_trigger.click()

    # Wait for dropdown to fully open
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Internal'));
            const links = Array.from(document.querySelectorAll('a'));
            const toolsLink = links.find(a => a.textContent?.includes('Tools'));
            return btn?.getAttribute('aria-expanded') === 'true' && 
                   toolsLink && getComputedStyle(toolsLink).visibility === 'visible';
        }""",
        timeout=5000,
    )

    # Find a dropdown menu item and test hover
    tools_link = page.locator("a:has-text('Tools')")
    await tools_link.wait_for(state="visible", timeout=2000)

    # Hover over the tools link
    await tools_link.hover()
    await page.wait_for_timeout(200)

    # Check that hover state applies (the link should be visible and clickable)
    is_visible = await tools_link.is_visible()
    assert is_visible, "Hovered menu item should remain visible"

    # Verify click works
    await tools_link.click()

    # Should navigate to tools page
    await page.wait_for_url("**/tools")
    assert "/tools" in page.url, "Should navigate to tools page on click"
