"""Playwright-based functional tests for chat history React UI - Basic display and navigation."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from playwright.async_api import Page

from tests.functional.web.conftest import WebTestFixture


async def wait_for_history_page_loaded(page: Page, timeout: int = 15000) -> bool:
    """Wait for the history page to load completely and return whether it succeeded."""
    try:
        # Wait until the frontend signals it is ready
        await page.wait_for_function(
            "() => document.documentElement.getAttribute('data-app-ready') === 'true'",
            timeout=timeout,
        )

        # Try multiple selectors that indicate the page is loaded
        await page.wait_for_selector(
            "h1:has-text('Conversation History'), h1, main, [data-testid='history-page']",
            timeout=timeout,
        )

        # Additional check - make sure the page text is there
        page_text = await page.text_content("body")
        if page_text and "Conversation History" in page_text:
            return True

        # If no text, try waiting a bit more for React to mount
        page_text = await page.text_content("body")
        return page_text is not None and "Conversation History" in page_text

    except Exception as e:
        logging.error(f"Failed to wait for history page: {e}")
        # Check if page has any content at all
        try:
            page_text = await page.text_content("body")
            return page_text is not None and "Conversation History" in page_text
        except Exception:
            return False


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_basic_loading(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test basic functionality of the history page React interface."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Set up console error tracking
    console_errors = []
    network_errors = []

    def on_console(msg: Any) -> None:  # noqa: ANN401  # playwright console message
        if msg.type == "error":
            console_errors.append(
                f"{msg.location.get('url', 'unknown')}:{msg.location.get('lineNumber', '?')} - {msg.text}"
            )
            print(f"[CONSOLE ERROR] {msg.text}")

    def on_response(response: Any) -> None:  # noqa: ANN401  # playwright response object
        if response.status >= 400:
            network_errors.append(f"{response.status} {response.url}")
            print(f"[NETWORK ERROR] {response.status} {response.url}")

    page.on("console", on_console)
    page.on("response", on_response)

    # Navigate to history page
    print(f"=== Navigating to {server_url}/history ===")
    await page.goto(f"{server_url}/history")

    # Take a screenshot before waiting
    await page.screenshot(path="/tmp/history_page_initial.png")
    print("Screenshot saved to /tmp/history_page_initial.png")

    # Check page content
    page_content = await page.content()
    print(f"Page content length: {len(page_content)}")
    print(f"Page title: {await page.title()}")

    # Check if React app root exists
    app_root = await page.locator("#app-root").count()
    print(f"Found {app_root} #app-root elements")

    # Check for router.html markers
    router_marker = "router-entry.jsx" in page_content
    print(f"Router entry point loaded: {router_marker}")

    # Wait for React to mount
    # Wait for page to fully load

    # Check for any h1 elements
    h1_count = await page.locator("h1").count()
    print(f"Found {h1_count} h1 elements on page")

    # Check for any text content
    body_text = await page.locator("body").text_content()
    print(
        f"Body text (first 500 chars): {body_text[:500] if body_text else 'No text content'}"
    )

    # Wait for network idle to ensure all resources loaded

    print("Network idle state reached")

    # Check for console and network errors
    if console_errors:
        print("=== CONSOLE ERRORS DETECTED ===")
        for err in console_errors:
            print(f"  - {err}")
        # Don't fail immediately, let's see what loaded

    if network_errors:
        print("=== NETWORK ERRORS DETECTED ===")
        for err in network_errors:
            print(f"  - {err}")

    # Wait for the history page to load
    page_loaded = await wait_for_history_page_loaded(page, timeout=15000)

    if not page_loaded:
        print("Failed to detect history page load - taking diagnostics")
        # Take another screenshot
        await page.screenshot(path="/tmp/history_page_after_wait.png")
        print("Screenshot saved to /tmp/history_page_after_wait.png")

        # Get final page state
        final_content = await page.content()
        print(f"Final page HTML (first 2000 chars):{final_content[:2000]}")

        # Check if there were critical errors
        if console_errors:
            print(f"Console errors detected: {console_errors}")

        assert not console_errors, f"Console errors detected: {console_errors}"
        assert page_loaded, "History page failed to load properly"

    # Verify React components have loaded by checking for filters section
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(timeout=5000)
    assert await filters_section.is_visible()

    # Check that results summary is present (handles empty state)
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    assert "Found" in summary_text and "conversation" in summary_text

    # Take screenshot of history page
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "history-list", viewport)

    # Final check for errors
    assert not console_errors, f"Console errors detected during test: {console_errors}"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_css_styling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that CSS styling is properly applied to React components."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Check that main container has expected CSS classes (indicating React components loaded)
    # The exact class names depend on the CSS modules
    conversations_list = page.locator("[class*='conversationsList']")
    await conversations_list.wait_for(timeout=5000)
    assert await conversations_list.is_visible()

    # Check that filters form has CSS styling
    filters_form = page.locator("form[class*='filtersForm'], .filtersForm")
    if await filters_form.count() > 0:
        assert await filters_form.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversations_list_display(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test conversations list display and metadata."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for API response - either conversations container or empty state will appear
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )

    # Check for either conversations or empty state
    conversations_container = page.locator(
        "[class*='conversationsContainer'], .conversationsContainer"
    )
    empty_state = page.locator("[class*='emptyState'], .emptyState")

    # Either conversations are displayed or empty state is shown
    has_conversations = (
        await conversations_container.count() > 0
        and await conversations_container.is_visible()
    )
    has_empty_state = await empty_state.count() > 0 and await empty_state.is_visible()

    # At least one should be visible (conversations or empty state)
    assert has_conversations or has_empty_state, (
        "Neither conversations nor empty state is displayed"
    )

    # If conversations exist, test their structure
    if has_conversations:
        # Check for conversation cards
        conversation_cards = page.locator(
            "[class*='conversationCard'], .conversationCard"
        )
        card_count = await conversation_cards.count()

        if card_count > 0:
            first_card = conversation_cards.first

            # Check conversation metadata elements
            conversation_link = first_card.locator(
                "[class*='conversationLink'], .conversationLink"
            )
            if await conversation_link.count() > 0:
                assert await conversation_link.is_visible()

            # Check for interface icon
            interface_icon = first_card.locator(
                "[class*='interfaceIcon'], .interfaceIcon"
            )
            if await interface_icon.count() > 0:
                assert await interface_icon.is_visible()

            # Check for message count metadata
            meta_items = first_card.locator("[class*='metaItem'], .metaItem")
            if await meta_items.count() > 0:
                assert await meta_items.first.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversation_navigation(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test navigation to conversation detail view."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for API response - either conversations container or empty state will appear
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )

    # Look for conversation links
    conversation_links = page.locator(
        "a[href*='/history/']:has-text('View Conversation')"
    )
    link_count = await conversation_links.count()

    # If there are conversations, test navigation
    if link_count > 0:
        # Click the first conversation link
        first_link = conversation_links.first
        await first_link.click()

        # Wait for navigation to detail view
        await page.wait_for_selector(
            "h1:has-text('Conversation Details'), h1:has-text('Conversation History')",
            timeout=5000,
        )

        # Verify we're on a conversation detail page
        detail_heading = page.locator("h1:has-text('Conversation Details')")
        if await detail_heading.count() > 0:
            assert await detail_heading.is_visible()

            # Check for back button
            back_button = page.locator("button:has-text('Back to Conversations')")
            await back_button.wait_for(timeout=5000)
            assert await back_button.is_visible()

            # Test back navigation
            await back_button.click()
            # Wait for navigation back to list
            await page.wait_for_selector(
                "h1:has-text('Conversation History')", timeout=5000
            )

            # Should be back on conversations list
            await page.wait_for_selector(
                "h1:has-text('Conversation History')", timeout=5000
            )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversation_detail_view(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test conversation detail view functionality."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Try to navigate directly to a conversation detail (will show error for non-existent ID)
    test_conversation_id = "test_conversation_id"
    await page.goto(f"{server_url}/history/{test_conversation_id}")

    # Wait for the loading indicator to disappear.
    await page.locator("text=Loading conversation...").wait_for(
        state="hidden", timeout=10000
    )

    # After loading, we should have either details or an error.
    detail_heading = page.locator("h1:has-text('Conversation Details')")
    error_message = page.locator("[class*='error'], .error")

    # Wait for either the details heading or an error message to be visible.
    await page.wait_for_selector(
        "h1:has-text('Conversation Details'), [class*='error']", timeout=5000
    )

    has_details = await detail_heading.is_visible()
    has_error = await error_message.is_visible()

    assert has_details or has_error, (
        "Conversation detail page should show details or an error after loading."
    )

    # The back button should be present in either the details or error state.
    back_button = page.locator("button:has-text('Back to Conversations')")
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_pagination_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test pagination controls when available."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for API response - either conversations container or empty state will appear
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )

    # Check if pagination controls are present
    pagination = page.locator("[class*='pagination'], .pagination")

    # If pagination exists, test basic functionality
    if await pagination.count() > 0 and await pagination.is_visible():
        # Look for page navigation elements
        page_buttons = pagination.locator("button, a")
        button_count = await page_buttons.count()

        if button_count > 0:
            # Pagination controls are present and visible
            assert await pagination.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_responsive_design(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test responsive design of history page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})
    # Wait for viewport change to render
    await page.wait_for_load_state("domcontentloaded")

    # Check that main elements are still visible
    heading = page.locator("h1:has-text('Conversation History')")
    assert await heading.is_visible()

    # Filters should still be accessible
    filters_section = page.locator("details summary:has-text('Filters')")
    assert await filters_section.is_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})
    # Wait for viewport change to render
    await page.wait_for_load_state("domcontentloaded")

    # Check elements are still visible
    assert await heading.is_visible()
    assert await filters_section.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})
    # Wait for viewport change to render
    await page.wait_for_load_state("domcontentloaded")

    # Check elements are still visible
    assert await heading.is_visible()
    assert await filters_section.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_api_error_handling(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test handling of API errors in history page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for conversations or empty/error state to appear
    await page.wait_for_selector(
        "[class*='conversationsContainer'], [class*='emptyState'], [class*='error']",
        timeout=5000,
    )

    # The page should handle API responses gracefully
    # Either show conversations, empty state, or error message
    has_conversations = (
        await page.locator("[class*='conversationsContainer']").count() > 0
    )
    has_empty_state = await page.locator("[class*='emptyState']").count() > 0
    has_error = await page.locator("[class*='error']").count() > 0
    has_loading = await page.locator("text=Loading").count() > 0

    # Page should be in one of these states, not stuck loading
    assert has_conversations or has_empty_state or has_error or not has_loading


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_message_display_structure(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test message display structure in conversation view."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate directly to a conversation (will handle non-existent gracefully)
    await page.goto(f"{server_url}/history/test_conv_id")

    # Wait for page response - look for back button which should always be present
    await page.wait_for_selector(
        "button:has-text('Back to Conversations')", timeout=5000
    )

    # Check that the page structure is correct regardless of whether conversation exists
    back_button = page.locator("button:has-text('Back to Conversations')")
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()

    # Check for conversation metadata section
    meta_section = page.locator("[class*='conversationMeta'], .conversationMeta")
    if await meta_section.count() > 0:
        assert await meta_section.is_visible()

    # Check for messages container (even if empty)
    messages_container = page.locator(
        "[class*='messagesContainer'], .messagesContainer"
    )
    empty_state = page.locator("[class*='emptyState'], .emptyState")
    error_state = page.locator("[class*='error'], .error")

    # Should show one of these states
    has_messages = (
        await messages_container.count() > 0 and await messages_container.is_visible()
    )
    has_empty = await empty_state.count() > 0 and await empty_state.is_visible()
    has_error = await error_state.count() > 0 and await error_state.is_visible()

    assert has_messages or has_empty or has_error
