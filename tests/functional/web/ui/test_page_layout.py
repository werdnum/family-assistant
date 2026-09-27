"""Simplified tests for page layout and navigation components."""

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("menu_name", "link_name", "minimum_x"),
    [("Data", "Notes", 10), ("Internal", "Tools", 100)],
)
async def test_navigation_dropdowns_open_and_position(
    web_test_fixture_readonly: WebTestFixture,
    menu_name: str,
    link_name: str,
    minimum_x: int,
) -> None:
    """Each desktop dropdown opens on one click and places its link below."""
    page = web_test_fixture_readonly.page
    await page.goto(f"{web_test_fixture_readonly.base_url}/notes")

    trigger = page.get_by_role("button", name=menu_name)
    await expect(trigger).to_be_visible()
    await trigger.click()
    await expect(trigger).to_have_attribute("aria-expanded", "true")

    link = page.get_by_role("link", name=link_name, exact=True)
    await expect(link).to_be_in_viewport()
    trigger_box = await trigger.bounding_box()
    link_box = await link.bounding_box()
    assert trigger_box is not None
    assert link_box is not None
    assert link_box["y"] > trigger_box["y"]
    assert link_box["x"] > minimum_x

    await page.mouse.click(1, 1)
    await expect(trigger).to_have_attribute("aria-expanded", "false")
    await expect(link).to_be_hidden()


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
    """Hovering a dropdown link changes its visible background before navigation."""
    page = web_test_fixture_readonly.page
    await page.goto(f"{web_test_fixture_readonly.base_url}/notes")

    internal_trigger = page.get_by_role("button", name="Internal")
    await internal_trigger.click()
    await expect(internal_trigger).to_have_attribute("aria-expanded", "true")
    tools_link = page.get_by_role("link", name="Tools", exact=True)
    await expect(tools_link).to_be_visible()

    background_before = await tools_link.evaluate(
        "element => getComputedStyle(element).backgroundColor"
    )
    await tools_link.hover()
    await page.wait_for_function(
        "([element, before]) => getComputedStyle(element).backgroundColor !== before",
        arg=[await tools_link.element_handle(), background_before],
    )

    await tools_link.click()
    await expect(page).to_have_url(f"{web_test_fixture_readonly.base_url}/tools")
