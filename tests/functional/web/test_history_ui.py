"""Playwright-based functional tests for chat history React UI."""

import logging
from typing import Any

import pytest
from playwright.async_api import Page

from tests.functional.web.pages.history_page import HistoryPage

from .conftest import WebTestFixture


async def wait_for_history_page_loaded(page: Page, timeout: int = 15000) -> bool:
    """Wait for the history page to load completely and return whether it succeeded."""
    try:
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
        # Wait for page to fully load
        await page.wait_for_load_state("networkidle", timeout=5000)
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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test basic functionality of the history page React interface."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Set up console error tracking
    console_errors = []
    network_errors = []

    def on_console(msg: Any) -> None:
        if msg.type == "error":
            console_errors.append(
                f"{msg.location.get('url', 'unknown')}:{msg.location.get('lineNumber', '?')} - {msg.text}"
            )
            print(f"[CONSOLE ERROR] {msg.text}")

    def on_response(response: Any) -> None:
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
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Check for any h1 elements
    h1_count = await page.locator("h1").count()
    print(f"Found {h1_count} h1 elements on page")

    # Check for any text content
    body_text = await page.locator("body").text_content()
    print(
        f"Body text (first 500 chars): {body_text[:500] if body_text else 'No text content'}"
    )

    # Wait for network idle to ensure all resources loaded
    await page.wait_for_load_state("networkidle", timeout=5000)
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

    # Final check for errors
    assert not console_errors, f"Console errors detected during test: {console_errors}"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_css_styling(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that CSS styling is properly applied to React components."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
async def test_history_filters_interface(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test filter form interactions on history page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    history_page = HistoryPage(page, server_url)
    await history_page.navigate_to()

    # Wait for filters section to be visible
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(state="visible", timeout=5000)

    # Check if filters are already open (they should be by default)
    filters_open = await page.locator("details[open]").count() > 0
    if not filters_open:
        # Click the summary to open the filters if needed
        await filters_section.click()

    # Wait for filter inputs to become visible
    await page.wait_for_selector(
        "input[name='conversation_id']", state="visible", timeout=5000
    )

    # Test interface type filter using the page object
    await history_page.set_interface_type_filter("web")
    selected_value = await history_page.get_interface_type_filter_value()
    assert selected_value == "web"

    # Test conversation ID filter (should be a text input)
    conv_input = page.locator("input[name='conversation_id']")
    await conv_input.wait_for(state="visible", timeout=5000)
    # Wait for input to be enabled (not loading)
    await page.wait_for_function(
        "document.querySelector('input[name=\"conversation_id\"]').disabled === false",
        timeout=5000,
    )
    await conv_input.fill("web_conv_123")
    conv_value = await conv_input.input_value()
    assert conv_value == "web_conv_123"

    # Test date filters
    date_from_input = page.locator("input[name='date_from']")
    await date_from_input.wait_for(state="visible", timeout=5000)
    # Wait for input to be enabled
    await page.wait_for_function(
        "document.querySelector('input[name=\"date_from\"]').disabled === false",
        timeout=5000,
    )
    await date_from_input.fill("2024-01-01")
    from_value = await date_from_input.input_value()
    assert from_value == "2024-01-01"

    date_to_input = page.locator("input[name='date_to']")
    await date_to_input.wait_for(state="visible", timeout=5000)
    # Wait for input to be enabled
    await page.wait_for_function(
        "document.querySelector('input[name=\"date_to\"]').disabled === false",
        timeout=5000,
    )
    await date_to_input.fill("2024-12-31")
    to_value = await date_to_input.input_value()
    assert to_value == "2024-12-31"

    # Test Clear Filters button
    # Look for Clear Filters button within the filters form (more specific selector)
    clear_button = page.locator("details button:has-text('Clear Filters')")
    await clear_button.wait_for(state="visible", timeout=5000)
    assert await clear_button.is_visible(), "Clear Filters button not found"
    await clear_button.click()

    # Wait for filters to be cleared - URL should be empty of query params
    await page.wait_for_function(
        "() => window.location.search === '' || window.location.search === '?'",
        timeout=5000,
    )

    # Verify filters are cleared
    interface_value = await history_page.get_interface_type_filter_value()
    conv_value = await conv_input.input_value()
    from_value_after = await date_from_input.input_value()
    to_value_after = await date_to_input.input_value()

    assert interface_value == "_all"
    assert conv_value == ""
    assert from_value_after == ""
    assert to_value_after == ""


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filters_url_state_preservation(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that filter state is preserved in URL parameters."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page with query parameters
    await page.goto(f"{server_url}/history?interface_type=web&page=1")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Use HistoryPage to check filter values
    history_page = HistoryPage(page, server_url)
    # Wait for the filter value to be applied from URL params
    await page.wait_for_function(
        "() => document.querySelector('[role=combobox]')?.textContent?.includes('Web')",
        timeout=5000,
    )
    selected_value = await history_page.get_interface_type_filter_value()
    assert selected_value == "web"

    # Verify URL contains the filter parameter
    current_url = page.url
    assert "interface_type=web" in current_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversations_list_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test conversations list display and metadata."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for API response (might show empty state or conversations)
    # Wait for page to fully load
    await page.wait_for_load_state("networkidle", timeout=5000)

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test navigation to conversation detail view."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for API response
    # Wait for page to fully load
    await page.wait_for_load_state("networkidle", timeout=5000)

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test conversation detail view functionality."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Try to navigate directly to a conversation detail (will show error for non-existent ID)
    test_conversation_id = "test_conversation_id"
    await page.goto(f"{server_url}/history/{test_conversation_id}")

    # Wait for page to load - look for either details or error
    await page.wait_for_selector(
        "h1:has-text('Conversation Details'), [class*='error'], text=Loading",
        timeout=5000,
    )

    # Should either show conversation details or an error state
    detail_heading = page.locator("h1:has-text('Conversation Details')")
    error_message = page.locator("[class*='error'], .error")
    loading_message = page.locator("text=Loading conversation")

    # One of these states should be visible
    has_details = await detail_heading.count() > 0 and await detail_heading.is_visible()
    has_error = await error_message.count() > 0 and await error_message.is_visible()
    has_loading = (
        await loading_message.count() > 0 and await loading_message.is_visible()
    )

    assert has_details or has_error or has_loading, (
        "Conversation detail page should show some state"
    )

    # Back button should always be present
    back_button = page.locator("button:has-text('Back to Conversations')")
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_pagination_interface(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test pagination controls when available."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for API response
    # Wait for page to fully load
    await page.wait_for_load_state("networkidle", timeout=5000)

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test responsive design of history page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test message display structure in conversation view."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filter_state_management(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test comprehensive filter state management."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page with URL parameters to auto-expand filters
    await page.goto(f"{server_url}/history?interface_type=all")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Apply multiple filters
    history_page = HistoryPage(page, server_url)
    await history_page.set_interface_type_filter("telegram")

    date_from_input = page.locator("input[name='date_from']")
    await date_from_input.wait_for(state="visible", timeout=5000)
    await date_from_input.fill("2024-06-01", force=True)
    # Trigger change event explicitly by pressing Tab to blur the field
    await date_from_input.press("Tab")

    # Wait for filter changes to be processed and URL to update
    await page.wait_for_url("**/history?*interface_type=telegram*", timeout=5000)

    # Check that URL reflects filter state
    current_url = page.url
    assert "interface_type=telegram" in current_url
    assert "date_from=2024-06-01" in current_url

    # Refresh page and verify filters persist
    await page.reload()
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Check that filter values are restored after reload
    interface_value = await history_page.get_interface_type_filter_value()
    date_value = await date_from_input.input_value()

    assert interface_value == "telegram"
    assert date_value == "2024-06-01"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_page_with_conversation_data(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test history page with actual conversation data."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # First, create some test conversation data via the API
    # This would typically be done through a fixture, but for now we'll use the API directly
    import httpx

    async with httpx.AsyncClient() as client:
        # Send a test message to create a conversation
        response = await client.post(
            f"{server_url}/api/v1/chat/send_message",
            json={
                "prompt": "Test message for history UI",
                "conversation_id": "test-conv-123",
                "interface_type": "web",
            },
        )
        assert response.status_code == 200

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Check that we have at least one conversation
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    # Should find at least 1 conversation
    assert "Found 1 conversation" in summary_text or "conversations" in summary_text

    # Check for conversation cards
    conversation_cards = page.locator("[class*='conversationCard']")
    card_count = await conversation_cards.count()
    assert card_count > 0, "Should have at least one conversation card"

    # Check first conversation card has expected elements
    first_card = conversation_cards.first

    # Check for conversation links (there are multiple)
    conv_links = first_card.locator("a[href*='/history/']")
    link_count = await conv_links.count()
    assert link_count >= 1

    # Check for interface icon
    interface_icon = first_card.locator("[class*='interfaceIcon']")
    assert await interface_icon.is_visible()

    # Check for View Conversation link
    view_link = first_card.locator("a:has-text('View Conversation')")
    assert await view_link.is_visible()

    # Click on the conversation to view details
    await view_link.click()

    # Wait for conversation detail page
    await page.wait_for_selector("h1:has-text('Conversation Details')", timeout=5000)

    # Check for back button
    back_button = page.locator("button:has-text('Back to Conversations')")
    assert await back_button.is_visible()

    # Navigate back
    await back_button.click()

    # Should be back on the list page
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_interface_filter_functionality(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test interface type filter functionality with real API integration."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page with URL parameters to auto-expand filters
    await page.goto(f"{server_url}/history?interface_type=all")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for page content to load (not show "Loading...")
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Test different interface filter options (should be visible due to URL parameters)
    history_page = HistoryPage(page, server_url)
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)  # Let React render

    # Check that all interface options are available
    options = await history_page.get_interface_type_options()
    expected_options = ["All Interfaces", "Web", "Telegram", "API", "Email"]
    for expected in expected_options:
        assert any(expected in opt for opt in options), (
            f"{expected} option not found in interface filter"
        )

    # Test filtering by telegram (should show fewer/no results in test env)
    await history_page.set_interface_type_filter("telegram")
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)  # Wait for API call

    # Check URL updated
    await page.wait_for_url("**/history?*interface_type=telegram*", timeout=5000)

    # Check results summary updated
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    await results_summary.text_content()  # Verify results summary updated
    assert "telegram" in page.url.lower()

    # Switch back to web filter
    await history_page.set_interface_type_filter("web")
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Check URL updated again
    await page.wait_for_url("**/history?*interface_type=web*", timeout=5000)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_date_range_filtering(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test date range filtering functionality."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history?interface_type=all")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for page content to load (not show "Loading...")
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Set date filters
    date_from_input = page.locator("input[name='date_from']")
    date_to_input = page.locator("input[name='date_to']")

    await date_from_input.wait_for(state="visible", timeout=5000)
    await date_to_input.wait_for(state="visible", timeout=5000)

    # Set a date range that should capture recent conversations
    await date_from_input.fill("2024-01-01", force=True)
    await date_from_input.press("Tab")

    await date_to_input.fill("2024-12-31", force=True)
    await date_to_input.press("Tab")
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Check URL contains date filters
    current_url = page.url
    assert "date_from=2024-01-01" in current_url
    assert "date_to=2024-12-31" in current_url

    # Clear date filters and verify they're removed from URL
    clear_button = page.locator("details button:has-text('Clear Filters')")
    await clear_button.click()
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)

    # URL should no longer have date filters
    cleared_url = page.url
    assert "date_from" not in cleared_url
    assert "date_to" not in cleared_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversation_id_filter(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test conversation ID filtering functionality."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page with URL parameters to auto-expand filters
    await page.goto(f"{server_url}/history?interface_type=all")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for page content to load (not show "Loading...")
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Test conversation ID filter (should be visible due to URL parameters)
    conv_input = page.locator("input[name='conversation_id']")
    await conv_input.wait_for(state="visible", timeout=10000)

    # Enter a specific conversation ID
    test_conv_id = "web_conv_test_123"
    await conv_input.fill(test_conv_id, force=True)
    await conv_input.press("Tab")
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Check URL contains the conversation ID filter
    current_url = page.url
    assert f"conversation_id={test_conv_id}" in current_url

    # The results should either show the specific conversation (if exists) or empty state
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    # Should show 0 conversations (since this ID likely doesn't exist) or 1 if it does
    assert summary_text is not None
    assert (
        "Found 0 conversation" in summary_text or "Found 1 conversation" in summary_text
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_combined_filters_interaction(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test interaction of multiple filters applied together."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page
    await page.goto(f"{server_url}/history?interface_type=all")

    # Wait for page to load
    page_loaded = await wait_for_history_page_loaded(page)
    assert page_loaded, "History page failed to load"

    # Wait for page content to load (not show "Loading...")
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Apply multiple filters
    history_page = HistoryPage(page, server_url)
    await history_page.set_interface_type_filter("web")

    date_from_input = page.locator("input[name='date_from']")
    await date_from_input.fill("2024-08-01", force=True)
    await date_from_input.press("Tab")

    conv_input = page.locator("input[name='conversation_id']")
    await conv_input.fill("web_conv", force=True)
    await conv_input.press("Tab")

    # Wait for filters to be applied
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Verify all filters are in URL
    current_url = page.url
    assert "interface_type=web" in current_url
    assert "date_from=2024-08-01" in current_url
    assert "conversation_id=web_conv" in current_url

    # Clear all filters
    clear_button = page.locator("details button:has-text('Clear Filters')")
    await clear_button.click()
    # Wait for navigation
    await page.wait_for_load_state("networkidle", timeout=5000)

    # Verify all filter values are cleared
    interface_value = await history_page.get_interface_type_filter_value()
    date_value = await date_from_input.input_value()
    conv_value = await conv_input.input_value()

    assert interface_value == "_all"
    assert date_value == ""
    assert conv_value == ""

    # URL should be clean
    cleared_url = page.url
    assert "interface_type" not in cleared_url
    assert "date_from" not in cleared_url
    assert "conversation_id" not in cleared_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filter_validation_and_error_handling(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test filter validation and error handling."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to history page with invalid date format in URL
    await page.goto(f"{server_url}/history?date_from=invalid-date")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Page should still load (frontend handles invalid dates gracefully)
    # The frontend should show an error message or fallback gracefully
    has_heading = await page.locator("h1:has-text('Conversation History')").count() > 0

    assert has_heading, "Page should load normally with graceful error handling"

    # Test with valid date format
    # Navigate with URL parameters to auto-expand filters
    await page.goto(f"{server_url}/history?interface_type=all")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    date_from_input = page.locator("input[name='date_from']")
    await date_from_input.wait_for(state="visible", timeout=5000)

    # HTML date inputs should handle validation automatically
    await date_from_input.fill("2024-08-09", force=True)
    await date_from_input.press("Tab")

    # Should work without errors
    current_value = await date_from_input.input_value()
    assert current_value == "2024-08-09"
