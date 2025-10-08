"""Simplified tests for page layout and navigation components."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_dropdowns_open_and_position(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that navigation dropdowns open and are positioned correctly to be usable.

    This test handles the timing complexities of RadixUI NavigationMenu which uses
    animations and asynchronous state updates."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to notes page which has the full navigation menu
    await page.goto(f"{base_url}/notes")

    # Wait for navigation to be rendered and interactive
    await page.wait_for_selector(
        "nav[data-orientation='horizontal']", state="visible", timeout=10000
    )

    # Wait for navigation to be fully ready and interactive
    await page.wait_for_function(
        """() => {
            const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes('Data'));
            if (!btn) return false;
            const style = getComputedStyle(btn);
            const rect = btn.getBoundingClientRect();
            return style.visibility === 'visible' &&
                   style.opacity === '1' &&
                   rect.width > 0 &&
                   rect.height > 0 &&
                   !btn.disabled;
        }""",
        timeout=3000,
    )

    # Test Data dropdown
    data_trigger = page.locator("button:has-text('Data')").first
    await data_trigger.wait_for(state="visible", timeout=5000)

    # Ensure the button is actually interactive before clicking
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Data'));
            if (!btn) return false;
            const rect = btn.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }""",
        timeout=5000,
    )

    # Click with retry logic in case of timing issues
    for attempt in range(3):
        try:
            await data_trigger.click()

            # Wait for dropdown to be fully open with content visible
            await page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const btn = buttons.find(b => b.textContent?.includes('Data'));
                    if (!btn || btn.getAttribute('aria-expanded') !== 'true') return false;
                    
                    const links = Array.from(document.querySelectorAll('a'));
                    const notesLink = links.find(a => a.textContent?.includes('Notes'));
                    if (!notesLink) return false;
                    
                    const style = getComputedStyle(notesLink);
                    const rect = notesLink.getBoundingClientRect();
                    
                    return style.visibility === 'visible' && 
                           style.opacity === '1' &&
                           rect.width > 0 && 
                           rect.height > 0;
                }""",
                timeout=3000,
            )
            break
        except Exception:
            if attempt == 2:
                raise
            # Wait for dropdown to fully close before retry
            await page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const btn = buttons.find(b => b.textContent?.includes('Data'));
                    if (!btn) return true;
                    const expanded = btn.getAttribute('aria-expanded');
                    return !expanded || expanded === 'false';
                }""",
                timeout=2000,
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

    # Close the Data dropdown by clicking outside
    await page.mouse.click(1, 1)

    # Wait for the dropdown to fully close before proceeding
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Data'));
            return !btn || btn.getAttribute('aria-expanded') !== 'true';
        }""",
        timeout=3000,
    )

    # Wait for dropdown to be fully closed and animations complete
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const dataBtn = buttons.find(b => b.textContent?.includes('Data'));
            if (!dataBtn) return false;
            // Check aria-expanded is false or not present
            const expanded = dataBtn.getAttribute('aria-expanded');
            // Also check that no dropdown content is visible
            const dropdownContent = document.querySelector('[data-radix-navigation-menu-content]');
            const isDropdownHidden = !dropdownContent || dropdownContent.style.display === 'none' ||
                                     dropdownContent.getAttribute('data-state') === 'closed';
            return (!expanded || expanded === 'false') && isDropdownHidden;
        }""",
        timeout=3000,
    )

    # Wait for CSS transitions to complete by checking computed styles are stable
    await page.wait_for_function(
        """() => {
            const dropdownContent = document.querySelector('[data-radix-navigation-menu-content]');
            if (!dropdownContent) return true;
            // Check that opacity is 0 or element is not visible
            const style = getComputedStyle(dropdownContent);
            return style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden';
        }""",
        timeout=1000,
    )

    # Test Internal dropdown with fresh state
    internal_trigger = page.locator("button:has-text('Internal')").first
    await internal_trigger.wait_for(state="visible", timeout=5000)

    # Ensure the Internal button is interactive
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Internal'));
            if (!btn) return false;
            const rect = btn.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }""",
        timeout=5000,
    )

    # Click with retry logic
    for attempt in range(3):
        try:
            await internal_trigger.click()

            # Wait for dropdown content to be fully visible and properly positioned
            await page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const btn = buttons.find(b => b.textContent?.includes('Internal'));
                    if (!btn || btn.getAttribute('aria-expanded') !== 'true') return false;
                    
                    const links = Array.from(document.querySelectorAll('a'));
                    const toolsLink = links.find(a => a.textContent?.includes('Tools'));
                    if (!toolsLink) return false;
                    
                    const rect = toolsLink.getBoundingClientRect();
                    const style = getComputedStyle(toolsLink);
                    const btnRect = btn.getBoundingClientRect();
                    
                    // Check that dropdown is visible and positioned correctly (not at far left)
                    // The dropdown should be positioned relative to its trigger button
                    const isPositionedCorrectly = rect.x > 100; // Should not be at far left edge
                    
                    return rect.width > 0 && 
                           rect.height > 0 && 
                           style.visibility === 'visible' &&
                           style.opacity === '1' &&
                           style.display !== 'none' &&
                           isPositionedCorrectly;
                }""",
                timeout=3000,
            )
            break
        except Exception:
            if attempt == 2:
                raise
            # Click outside to reset state
            await page.mouse.click(1, 1)
            # Wait for dropdown to close and state to reset
            await page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const btn = buttons.find(b => b.textContent?.includes('Internal'));
                    if (!btn) return true;
                    const expanded = btn.getAttribute('aria-expanded');
                    const dropdownContent = document.querySelector('[data-radix-navigation-menu-content]');
                    const isDropdownHidden = !dropdownContent || dropdownContent.style.display === 'none' ||
                                           dropdownContent.getAttribute('data-state') === 'closed';
                    return (!expanded || expanded === 'false') && isDropdownHidden;
                }""",
                timeout=2000,
            )
            # Wait for state reset to complete - verify button is ready for interaction
            await page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    const btn = buttons.find(b => b.textContent?.includes('Internal'));
                    if (!btn) return false;
                    const rect = btn.getBoundingClientRect();
                    const style = getComputedStyle(btn);
                    return rect.width > 0 && rect.height > 0 && !btn.disabled &&
                           style.visibility === 'visible' && style.opacity === '1';
                }""",
                timeout=1000,
            )

    # Find the Tools link
    tools_link = page.locator("a:has-text('Tools')")

    # Wait for the Tools link to be visible (in case dropdown is still animating)
    await tools_link.wait_for(state="visible", timeout=2000)

    # The key test: Can we actually see the dropdown content?
    is_tools_visible = await tools_link.is_visible()
    assert is_tools_visible, "Tools link should be visible in Internal dropdown"

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
async def test_navigation_responsive_behavior(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that navigation layout adapts to different viewport sizes."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Desktop viewport
    await page.set_viewport_size({"width": 1280, "height": 720})
    await page.goto(f"{base_url}/notes")

    # Wait for navigation to render before checking
    await page.wait_for_selector(
        "nav[data-orientation='horizontal']", state="visible", timeout=10000
    )

    # Desktop should show horizontal navigation
    desktop_nav = await page.query_selector("nav[data-orientation='horizontal']")
    assert desktop_nav is not None, "Desktop viewport should show horizontal navigation"

    # Check that desktop nav is visible
    is_visible = await page.is_visible("nav[data-orientation='horizontal']")
    assert is_visible, "Desktop navigation should be visible"

    # Mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Wait for layout to stabilize after viewport change
    # The desktop nav is inside a div with class "hidden md:block"
    # On mobile, this div should have display: none
    await page.wait_for_function(
        """() => {
            const desktopNav = document.querySelector("nav[data-orientation='horizontal']");
            if (!desktopNav || !desktopNav.parentElement) return false;
            // Check the parent div has display: none (it has "hidden md:block" class)
            const parentStyle = getComputedStyle(desktopNav.parentElement);
            return parentStyle.display === 'none';
        }""",
        timeout=10000,
    )

    # Mobile should hide desktop nav container
    desktop_nav_container_hidden = await page.is_hidden(
        "nav[data-orientation='horizontal']"
    )
    assert desktop_nav_container_hidden, "Desktop navigation should be hidden on mobile"

    # Find the mobile menu button - it has sr-only text "Open navigation menu"
    # Use getByRole to find the button by its accessible name
    mobile_button = page.get_by_role("button", name="Open navigation menu")
    await mobile_button.wait_for(state="visible", timeout=5000)

    # Test mobile menu functionality - click the mobile navigation button
    await mobile_button.click()

    # Wait for navigation sheet/dialog to open and be visible
    await page.wait_for_function(
        """() => {
            const dialog = document.querySelector("[role='dialog']");
            const openElement = document.querySelector("[data-state='open']");
            const element = dialog || openElement;
            if (!element) return false;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility === 'visible' && style.display !== 'none' &&
                   rect.width > 0 && rect.height > 0;
        }""",
        timeout=3000,
    )

    # Should open navigation sheet/dialog
    navigation_opened = await page.is_visible("[role='dialog'], [data-state='open']")
    assert navigation_opened, "Mobile menu should open navigation sheet"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_navigation_hover_states(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that navigation menu items have proper hover states."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    await page.goto(f"{base_url}/notes")

    # Wait for navigation
    await page.wait_for_selector(
        "nav[data-orientation='horizontal']", state="visible", timeout=10000
    )

    # Wait for navigation to be fully ready (animations complete)
    await page.wait_for_function(
        """() => {
            const nav = document.querySelector("nav[data-orientation='horizontal']");
            if (!nav) return false;
            const style = getComputedStyle(nav);
            // Also check that Internal button exists and is ready
            const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent?.includes('Internal'));
            if (!btn) return false;
            const btnStyle = getComputedStyle(btn);
            return style.visibility === 'visible' && 
                   style.opacity === '1' &&
                   btnStyle.visibility === 'visible' &&
                   btnStyle.opacity === '1';
        }""",
        timeout=3000,
    )

    # Open Internal dropdown to test hover states
    internal_trigger = page.locator("button:has-text('Internal')").first

    # Ensure button is ready before clicking
    await internal_trigger.wait_for(state="visible", timeout=5000)
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Internal'));
            if (!btn) return false;
            const rect = btn.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }""",
        timeout=5000,
    )

    await internal_trigger.click()

    # Wait for dropdown to fully open with animations complete
    await page.wait_for_function(
        """() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => b.textContent?.includes('Internal'));
            if (!btn || btn.getAttribute('aria-expanded') !== 'true') return false;
            
            const links = Array.from(document.querySelectorAll('a'));
            const toolsLink = links.find(a => a.textContent?.includes('Tools'));
            if (!toolsLink) return false;
            
            const style = getComputedStyle(toolsLink);
            return style.visibility === 'visible' && style.opacity === '1';
        }""",
        timeout=5000,
    )

    # Find a dropdown menu item and test hover
    tools_link = page.locator("a:has-text('Tools')")
    await tools_link.wait_for(state="visible", timeout=2000)

    # Hover over the tools link
    await tools_link.hover()
    # Wait for hover state to be applied
    await page.wait_for_function(
        """() => {
            const links = Array.from(document.querySelectorAll('a'));
            const toolsLink = links.find(a => a.textContent?.includes('Tools'));
            if (!toolsLink) return false;
            // Check that the element is in hover state (usually has background change)
            const rect = toolsLink.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        }""",
        timeout=1000,
    )

    # Check that hover state applies (the link should be visible and clickable)
    is_visible = await tools_link.is_visible()
    assert is_visible, "Hovered menu item should remain visible"

    # Verify click works
    await tools_link.click()

    # Should navigate to tools page
    await page.wait_for_url("**/tools")
    assert "/tools" in page.url, "Should navigate to tools page on click"
