"""Basic Playwright tests to verify the web UI loads correctly."""

import pytest
from playwright.async_api import expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.base_page import BasePage


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_homepage_loads_with_playwright(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the homepage loads successfully using Playwright."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Register console error handler BEFORE navigation to catch all errors
    console_errors = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg) if msg.type == "error" else None,
    )

    await page.goto(base_url)
    await expect(page.locator("h1")).to_have_text("Family Assistant", timeout=15000)
    await BasePage(page, base_url).wait_for_page_idle()

    assert len(console_errors) == 0, (
        f"Page should not have console errors, but found: {[err.text for err in console_errors]}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_homepage_content_visible_on_mobile(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """The landing page remains usable at a narrow mobile width."""
    page = web_test_fixture_readonly.page
    await page.set_viewport_size({"width": 375, "height": 667})
    await page.goto(web_test_fixture_readonly.base_url)

    await expect(page.locator("main")).to_be_visible()
    await expect(page.locator("main h1")).to_have_text("Family Assistant")
    await expect(page.get_by_placeholder("How can I help you today?")).to_be_visible()
    assert await page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_backend_api_accessible(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that the backend API is accessible from the frontend."""
    page = web_test_fixture_readonly.page

    # Get the actual API port from the assistant's configuration
    api_port = web_test_fixture_readonly.assistant.config.server_port

    # Make a direct API request through the page context to the backend directly
    response = await page.request.get(f"http://localhost:{api_port}/health")

    # Health check should return 200 OK
    assert response.ok, (
        f"API health check should return OK status, got {response.status}"
    )

    # Check response content - with Telegram disabled, it should report healthy
    data = await response.json()
    assert data.get("status") == "healthy", (
        f"API should report healthy status, got {data.get('status')}: {data.get('reason')}"
    )

    # Verify the Vite proxy is working by checking a health endpoint through Vite server
    vite_proxied_response = await page.request.get(
        f"{web_test_fixture_readonly.base_url}/health"
    )
    assert vite_proxied_response.ok, "API should be accessible through Vite proxy"

    # Check response data through proxy
    proxy_data = await vite_proxied_response.json()
    assert proxy_data.get("status") == "healthy", (
        f"API through Vite proxy should report healthy status, got {proxy_data.get('status')}"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_add_note_with_javascript(web_test_fixture: WebTestFixture) -> None:
    """Test adding a note using the UI with JavaScript/CSS functionality."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to notes page
    await page.goto(f"{base_url}/notes")

    # Click on Add Note link/button
    await page.click("a[href='/notes/add'], button:has-text('Add Note')")

    # Wait for navigation to add note page
    await page.wait_for_url("**/notes/add")

    # Fill in the note form
    # Wait for form to be visible
    await page.wait_for_selector("form", state="visible")

    # Fill in title field
    title_input = await page.wait_for_selector("input[name='title']", state="visible")
    assert title_input is not None, "Title input field not found"
    await title_input.fill("Test Note from Playwright")

    # Fill in content field (might be textarea or other input)
    content_input = await page.wait_for_selector(
        "textarea[name='content'], input[name='content'], #content", state="visible"
    )
    assert content_input is not None, "Content input field not found"
    await content_input.fill(
        "This is a test note created by Playwright with full JS support"
    )

    # Submit the form
    submit_button = await page.wait_for_selector(
        "button[type='submit'], input[type='submit'], button:has-text('Save')",
        state="visible",
    )
    assert submit_button is not None, "Submit button not found"
    await submit_button.click()

    # Should redirect back to notes list after successful creation
    await page.wait_for_url("**/notes", timeout=10000)

    # Verify the note appears in the list
    # Wait for the note to appear (might need a moment for the page to update)
    await page.wait_for_selector("text=Test Note from Playwright", timeout=5000)

    # Verify content is visible or accessible
    # The exact selector depends on how notes are displayed
    note_element = page.locator("text=Test Note from Playwright").first
    assert await note_element.is_visible(), "Created note should be visible in the list"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_css_and_styling_loads(web_test_fixture_readonly: WebTestFixture) -> None:
    """Test that CSS stylesheets are properly loaded through Vite."""
    page = web_test_fixture_readonly.page
    base_url = web_test_fixture_readonly.base_url

    # Navigate to homepage
    await page.goto(base_url)

    await page.wait_for_selector("main", state="visible")
    background = await page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--background').trim()"
    )
    assert background, "The app theme should be applied"

    # Check that CSS is loaded properly (either Vite dev or production build)
    style_tags = await page.evaluate("""
        () => {
            const styles = document.querySelectorAll('style, link[rel="stylesheet"]');
            const styleInfo = Array.from(styles).map(el => ({
                tagName: el.tagName,
                href: el.href || null,
                rel: el.getAttribute('rel'),
                hasContent: el.textContent ? el.textContent.length > 0 : false
            }));
            return {
                count: styles.length,
                hasViteStyles: Array.from(styles).some(el => 
                    el.href?.includes('/src/') || 
                    el.textContent?.includes('--vite-') ||
                    el.getAttribute('data-vite-dev-id')
                ),
                hasProductionStyles: Array.from(styles).some(el =>
                    el.href?.includes('/static/dist/') ||
                    el.href?.includes('/assets/')
                ),
                styleInfo: styleInfo
            };
        }
    """)

    assert style_tags["count"] > 0, (
        f"Should have at least one style tag or stylesheet link. Found: {style_tags}"
    )
    # In tests we run in production mode, so check for production styles
    assert style_tags["hasViteStyles"] or style_tags["hasProductionStyles"], (
        "Should have either Vite dev styles or production build styles"
    )
