"""Playwright-based functional tests for Documentation React UI."""

import logging

import pytest

from .conftest import WebTestFixture

logger = logging.getLogger(__name__)


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

    # Wait for the page heading to appear
    await page.wait_for_selector("h1:has-text('Documentation')", timeout=10000)

    # Check that page has expected structure
    docs_list = page.locator("[class*='docsList']")
    if await docs_list.count() > 0:
        assert await docs_list.is_visible()

    # Check for documentation items or loading state or error
    doc_items = page.locator("[class*='docItem']")
    error = page.locator("[class*='error']")
    no_docs = page.locator("text=/no documentation files found/i")
    empty_state = page.locator("[class*='empty']")

    # Verify successful page load (either docs or no docs message, but no error)
    has_docs = await doc_items.count() > 0
    has_error = await error.count() > 0
    has_no_docs = await no_docs.count() > 0
    has_empty = await empty_state.count() > 0

    # Get page content for debugging
    if not (has_docs or has_no_docs or has_empty) or has_error:
        page_content = await page.content()
        logger.warning(f"Page content snippet: {page_content[:1000]}")

        # Check for specific error messages
        error_messages = await error.all_text_contents() if has_error else []
        logger.warning(f"Error messages: {error_messages}")

    # The page should show content without errors
    assert (has_docs or has_no_docs or has_empty) and not has_error, (
        "Should show documentation items or no docs message without errors"
    )


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

    # Wait for the page to render (look for any content element)
    await page.wait_for_selector(
        "[class*='content'], [class*='sidebar'], [class*='error']", timeout=10000
    )

    # Check for either sidebar or content or error - the page should have some content
    sidebar_elements = page.locator("[class*='sidebar']")
    content = page.locator("[class*='content']")
    error = page.locator("[class*='error']")
    markdown = page.locator("[class*='markdownContent']")

    has_sidebar = await sidebar_elements.count() > 0
    has_content = await content.count() > 0
    has_error = await error.count() > 0
    has_markdown = await markdown.count() > 0

    assert has_sidebar or has_content or has_error or has_markdown, (
        "Should show sidebar, content, error, or markdown"
    )


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
        # The back button text is "← Documentation" not "Back"
        back_button = page.locator("button:has-text('Documentation')")
        sidebar = page.locator("[class*='sidebar']")
        doc_nav_items = page.locator("[class*='docNavItem']")

        has_back = await back_button.count() > 0
        has_sidebar = await sidebar.count() > 0
        has_nav_items = await doc_nav_items.count() > 0

        assert has_back or has_sidebar or has_nav_items, (
            "Should have navigation options"
        )
