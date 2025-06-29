"""Basic Playwright tests to verify the web UI loads correctly."""

import pytest

from tests.functional.web.conftest import WebTestFixture


@pytest.mark.asyncio
async def test_homepage_loads_with_playwright(web_test_fixture: WebTestFixture) -> None:
    """Test that the homepage loads successfully using Playwright."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to homepage
    await page.goto(base_url)

    # Wait for the page to be fully loaded
    await page.wait_for_load_state("networkidle")

    # Verify page has a title
    title = await page.title()
    assert title is not None, "Page should have a title"

    # Check for main content area - the UI should have a main element
    main_element = await page.wait_for_selector("main", state="visible", timeout=5000)
    assert main_element is not None, "Page should have a visible main element"

    # Verify no JavaScript errors occurred
    console_errors = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg) if msg.type == "error" else None,
    )

    # Give a moment for any errors to be logged
    await page.wait_for_timeout(500)

    assert len(console_errors) == 0, (
        f"Page should not have console errors, but found: {[err.text for err in console_errors]}"
    )

    # Verify the page contains some expected content
    body_text = await page.text_content("body")
    assert body_text is not None and len(body_text) > 0, "Page should have content"


@pytest.mark.asyncio
async def test_notes_page_accessible(web_test_fixture: WebTestFixture) -> None:
    """Test that the notes page is accessible and renders correctly."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to homepage first
    await page.goto(base_url)

    # Wait for page to load
    await page.wait_for_load_state("networkidle")

    # The homepage is the notes page, so check for notes-specific elements
    # Look for "Add New Note" button or link
    add_note_element = await page.wait_for_selector(
        "a[href='/notes/add'], button:has-text('Add New Note'), a.add-button",
        state="visible",
        timeout=5000,
    )
    assert add_note_element is not None, (
        "Notes page should have an 'Add Note' link or button"
    )

    # Check for notes list container (even if empty)
    # The page should have some container for notes
    await page.wait_for_selector("main", state="visible")

    # Verify page is interactive - click the Add Note link
    await add_note_element.click()

    # Should navigate to add note page
    await page.wait_for_url("**/notes/add", timeout=5000)

    # Verify add note form is present
    form_element = await page.wait_for_selector("form", state="visible", timeout=5000)
    assert form_element is not None, "Add note page should have a form"


@pytest.mark.asyncio
async def test_backend_api_accessible(web_test_fixture: WebTestFixture) -> None:
    """Test that the backend API is accessible from the frontend."""
    page = web_test_fixture.page

    # Get the actual API port from the assistant's configuration
    api_port = web_test_fixture.assistant.config.get("server_port", 8000)

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
        f"{web_test_fixture.base_url}/health"
    )
    assert vite_proxied_response.ok, "API should be accessible through Vite proxy"

    # Check response data through proxy
    proxy_data = await vite_proxied_response.json()
    assert proxy_data.get("status") == "healthy", (
        f"API through Vite proxy should report healthy status, got {proxy_data.get('status')}"
    )


@pytest.mark.asyncio
async def test_page_navigation_elements(web_test_fixture: WebTestFixture) -> None:
    """Test that main navigation elements are present and functional."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to homepage
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle")

    # Check for any navigation links - the app should have some navigation
    # Since the exact navigation structure may vary, just verify links exist
    links = await page.locator("a[href]").all()
    assert len(links) > 0, "Page should have at least one navigation link"

    # Get all link hrefs to verify they're valid
    hrefs = []
    for link in links[:5]:  # Check first 5 links
        href = await link.get_attribute("href")
        if href:
            hrefs.append(href)

    assert len(hrefs) > 0, "Page should have links with href attributes"

    # Verify at least some links are internal (not external)
    internal_links = [h for h in hrefs if h.startswith("/") or "localhost" in h]
    assert len(internal_links) > 0, "Page should have internal navigation links"


@pytest.mark.asyncio
async def test_responsive_design(web_test_fixture: WebTestFixture) -> None:
    """Test that the UI is responsive and works on mobile viewport."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Set mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Navigate to homepage
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle")

    # Check that main content is still visible
    main_element = await page.wait_for_selector("main", state="visible", timeout=5000)
    assert main_element is not None, "Main content should be visible on mobile"

    # Reset viewport
    await page.set_viewport_size({"width": 1280, "height": 720})


@pytest.mark.asyncio
async def test_add_note_with_javascript(web_test_fixture: WebTestFixture) -> None:
    """Test adding a note using the UI with JavaScript/CSS functionality."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to homepage
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle")

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
    await page.wait_for_url("**/", timeout=10000)

    # Verify the note appears in the list
    # Wait for the note to appear (might need a moment for the page to update)
    await page.wait_for_selector("text=Test Note from Playwright", timeout=5000)

    # Verify content is visible or accessible
    # The exact selector depends on how notes are displayed
    note_element = page.locator("text=Test Note from Playwright").first
    assert await note_element.is_visible(), "Created note should be visible in the list"


@pytest.mark.asyncio
async def test_css_and_styling_loads(web_test_fixture: WebTestFixture) -> None:
    """Test that CSS stylesheets are properly loaded through Vite."""
    page = web_test_fixture.page
    base_url = web_test_fixture.base_url

    # Navigate to homepage
    await page.goto(base_url)
    await page.wait_for_load_state("networkidle")

    # Check that CSS is loaded by verifying computed styles
    # Get a main element to check styling
    main_element = await page.wait_for_selector("main", state="visible")

    # Check that the element has some computed styles (not just browser defaults)
    # This verifies CSS is loaded and applied
    assert main_element is not None, "Main element not found"
    computed_style = await main_element.evaluate("""
        (element) => {
            const styles = window.getComputedStyle(element);
            return {
                // Check for custom properties that would only exist if CSS loaded
                hasCustomFont: styles.fontFamily !== 'Times New Roman',
                hasBoxSizing: styles.boxSizing === 'border-box',
                // Check if any CSS custom properties are defined
                hasCSSVariables: Array.from(styles).some(prop => prop.startsWith('--')),
                // Get background color to ensure it's not default
                backgroundColor: styles.backgroundColor,
                // Get some indication that layout CSS is applied
                display: styles.display,
                margin: styles.margin,
                padding: styles.padding
            };
        }
    """)

    # Verify that CSS is actually applied (not just browser defaults)
    assert computed_style["hasCustomFont"] or computed_style["hasBoxSizing"], (
        "CSS should be loaded and applied to elements"
    )

    # Check that Vite injected the CSS properly by looking for style/link tags
    style_tags = await page.evaluate("""
        () => {
            const styles = document.querySelectorAll('style, link[rel="stylesheet"]');
            return {
                count: styles.length,
                hasViteStyles: Array.from(styles).some(el => 
                    el.href?.includes('/src/') || 
                    el.textContent?.includes('--vite-') ||
                    el.getAttribute('data-vite-dev-id')
                )
            };
        }
    """)

    assert style_tags["count"] > 0, (
        "Should have at least one style tag or stylesheet link"
    )
    assert style_tags["hasViteStyles"], (
        "Should have Vite-injected styles in development mode"
    )
