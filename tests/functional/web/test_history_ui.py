"""Playwright-based functional tests for chat history React UI."""

from typing import Any

import pytest

from .conftest import WebTestFixture


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
    print(f"\n=== Navigating to {server_url}/history ===")
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
    await page.wait_for_timeout(2000)

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
        print("\n=== CONSOLE ERRORS DETECTED ===")
        for err in console_errors:
            print(f"  - {err}")
        # Don't fail immediately, let's see what loaded

    if network_errors:
        print("\n=== NETWORK ERRORS DETECTED ===")
        for err in network_errors:
            print(f"  - {err}")

    # Try to wait for h1 with more debugging
    try:
        await page.wait_for_selector("h1", timeout=10000)
    except Exception as e:
        print(f"Failed to find h1 element: {e}")
        # Take another screenshot
        await page.screenshot(path="/tmp/history_page_after_wait.png")
        print("Screenshot saved to /tmp/history_page_after_wait.png")

        # Get final page state
        final_content = await page.content()
        print(f"\nFinal page HTML (first 2000 chars):\n{final_content[:2000]}")

        # Check if there were critical errors
        assert not console_errors, f"Console errors detected: {console_errors}"
        raise

    # Check page title and heading
    await page.wait_for_selector("h1:has-text('Conversation History')")

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
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for filters section to be visible
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(state="visible", timeout=5000)

    # Check if details is already open by checking if select is visible
    interface_select = page.locator("select[name='interface_type']")
    is_visible = await interface_select.is_visible()

    if not is_visible:
        # Click the summary to open the filters
        await filters_section.click()
        await page.wait_for_timeout(500)

    # Wait for the interface select to be visible
    await interface_select.wait_for(state="visible", timeout=5000)
    await interface_select.select_option("web", force=True)
    selected_value = await interface_select.input_value()
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
    # First check if any button exists in the filters actions area
    all_buttons = await page.query_selector_all(
        ".filtersActions button, details button"
    )
    assert len(all_buttons) > 0, "No buttons found in filters section"

    # Find Clear Filters button
    clear_button = None
    for btn in all_buttons:
        text = await btn.text_content()
        if text and "Clear" in text and "Filters" in text:
            clear_button = btn
            break

    assert clear_button is not None, "Clear Filters button not found"
    await clear_button.click()

    # Verify filters are cleared
    interface_value = await interface_select.input_value()
    conv_value = await conv_input.input_value()
    from_value_after = await date_from_input.input_value()
    to_value_after = await date_to_input.input_value()

    assert interface_value == ""
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

    # Check that filter values are restored from URL
    interface_select = page.locator("select[name='interface_type']")
    await interface_select.wait_for(timeout=5000)
    selected_value = await interface_select.input_value()
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
    await page.wait_for_timeout(2000)

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
    await page.wait_for_timeout(2000)

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
        await page.wait_for_timeout(2000)

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
            await page.wait_for_timeout(1000)

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

    # Wait for page to load
    await page.wait_for_timeout(3000)

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
    await page.wait_for_timeout(2000)

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
    await page.wait_for_timeout(500)

    # Check that main elements are still visible
    heading = page.locator("h1:has-text('Conversation History')")
    assert await heading.is_visible()

    # Filters should still be accessible
    filters_section = page.locator("details summary:has-text('Filters')")
    assert await filters_section.is_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})
    await page.wait_for_timeout(500)

    # Check elements are still visible
    assert await heading.is_visible()
    assert await filters_section.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})
    await page.wait_for_timeout(500)

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

    # Wait for API calls to complete (successful or failed)
    await page.wait_for_timeout(3000)

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

    # Wait for page response
    await page.wait_for_timeout(3000)

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

    # Navigate to history page
    await page.goto(f"{server_url}/history")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)
    await page.wait_for_timeout(1000)

    # Apply multiple filters
    interface_select = page.locator("select[name='interface_type']")
    await interface_select.wait_for(timeout=5000)
    await interface_select.select_option("telegram", force=True)

    date_from_input = page.locator("input[name='date_from']")
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
    interface_value = await interface_select.input_value()
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
