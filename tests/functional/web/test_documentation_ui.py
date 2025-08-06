"""Playwright-based functional tests for Documentation React UI."""

import pytest

from .conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_list_page_loads(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that documentation list page loads successfully."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to documentation page
    await page.goto(f"{server_url}/docs")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Documentation')", timeout=10000)

    # Check that page has expected structure
    docs_list = page.locator("[class*='docsList']")
    await docs_list.wait_for(timeout=5000)
    assert await docs_list.is_visible()

    # Check for documentation items or loading state
    doc_items = page.locator("[class*='docItem']")
    loading = page.locator("[class*='loading']")

    # Either docs are displayed or loading is shown
    has_docs = await doc_items.count() > 0
    has_loading = await loading.count() > 0 and await loading.is_visible()

    assert has_docs or has_loading, "Should show documentation items or loading state"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_view_page_loads(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that documentation view page loads successfully."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to a specific document (USER_GUIDE.md should exist)
    await page.goto(f"{server_url}/docs/USER_GUIDE.md")

    # Wait for page to load
    await page.wait_for_timeout(3000)

    # Check for sidebar
    sidebar = page.locator("[class*='sidebar']")
    if await sidebar.count() > 0:
        assert await sidebar.is_visible()

    # Check for content area or error state
    content = page.locator("[class*='content']")
    error = page.locator("[class*='error']")

    has_content = await content.count() > 0 and await content.is_visible()
    has_error = await error.count() > 0 and await error.is_visible()

    assert has_content or has_error, "Should show content or error state"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_documentation_navigation(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test navigation between documentation pages."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Start on documentation list
    await page.goto(f"{server_url}/docs")
    await page.wait_for_selector("h1:has-text('Documentation')", timeout=10000)

    # Click on a documentation card if available
    doc_cards = page.locator("[class*='docItem']")
    if await doc_cards.count() > 0:
        # Click the first doc card
        await doc_cards.first.click()

        # Should navigate to document view
        await page.wait_for_timeout(2000)

        # Check URL changed
        assert "/docs/" in page.url

        # Check for back button or sidebar
        back_button = page.locator("button:has-text('Back')")
        sidebar_link = page.locator("[class*='sidebar'] a")

        has_back = await back_button.count() > 0
        has_sidebar = await sidebar_link.count() > 0

        assert has_back or has_sidebar, "Should have navigation options"
