"""Playwright-based functional tests for chat history React UI - Filtering and interactions."""

import httpx
import pytest
from playwright.async_api import Page, expect

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.history_page import HistoryPage


async def wait_for_history_page_loaded(page: Page, timeout: int = 15000) -> bool:
    """Wait for the history page to load completely and return whether it succeeded."""
    try:
        await page.wait_for_function(
            "() => document.documentElement.getAttribute('data-app-ready') === 'true'",
            timeout=timeout,
        )
        await page.wait_for_selector(
            "h1:has-text('Conversation History'), h1, main, [data-testid='history-page']",
            timeout=timeout,
        )
        page_text = await page.text_content("body")
        if page_text and "Conversation History" in page_text:
            return True
        page_text = await page.text_content("body")
        return page_text is not None and "Conversation History" in page_text
    except Exception:
        try:
            page_text = await page.text_content("body")
            return page_text is not None and "Conversation History" in page_text
        except Exception:
            return False


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filters_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test filter form interactions on history page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
    # Clear any existing value first, then fill the new value
    await conv_input.clear()
    await conv_input.fill("web_conv_123", force=True)
    # Wait for the value to be set using expect
    await expect(conv_input).to_have_value("web_conv_123", timeout=5000)

    # Test date filters
    date_from_input = page.locator("input[name='date_from']")
    await date_from_input.wait_for(state="visible", timeout=5000)
    # Wait for input to be enabled
    await page.wait_for_function(
        "document.querySelector('input[name=\"date_from\"]').disabled === false",
        timeout=5000,
    )
    # For date inputs, use fill with force and wait for value to be set
    await date_from_input.fill("2024-01-01", force=True)
    await expect(date_from_input).to_have_value("2024-01-01", timeout=5000)

    date_to_input = page.locator("input[name='date_to']")
    await date_to_input.wait_for(state="visible", timeout=5000)
    # Wait for input to be enabled
    await page.wait_for_function(
        "document.querySelector('input[name=\"date_to\"]').disabled === false",
        timeout=5000,
    )
    # For date inputs, use fill with force and wait for value to be set
    await date_to_input.fill("2024-12-31", force=True)
    await expect(date_to_input).to_have_value("2024-12-31", timeout=5000)

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
    assert not conv_value
    assert not from_value_after
    assert not to_value_after


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filters_url_state_preservation(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that filter state is preserved in URL parameters."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
async def test_history_interface_filter_functionality(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test interface type filter functionality with real API integration."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to history page with URL parameters to auto-expand filters
    await page.goto(f"{server_url}/history?interface_type=all")
    await page.wait_for_selector("h1:has-text('Conversation History')", timeout=10000)

    # Wait for page content to load (not show "Loading...")
    await page.wait_for_selector("main:not(:has-text('Loading...'))", timeout=10000)

    # Test different interface filter options (should be visible due to URL parameters)
    history_page = HistoryPage(page, server_url)

    # Check that all interface options are available
    options = await history_page.get_interface_type_options()
    expected_options = ["All Interfaces", "Web", "Telegram", "API", "Email"]
    for expected in expected_options:
        assert any(expected in opt for opt in options), (
            f"{expected} option not found in interface filter"
        )

    # Test filtering by telegram (should show fewer/no results in test env)
    await history_page.set_interface_type_filter("telegram")

    # Wait for URL to update and content to reload
    await page.wait_for_url("**/history?*interface_type=telegram*", timeout=5000)

    # Wait for content to finish loading after filter change
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )

    # Check results summary updated
    results_summary = page.locator("text=/Found \\d+ conversation/")
    await results_summary.wait_for(timeout=5000)
    await results_summary.text_content()  # Verify results summary updated
    assert "telegram" in page.url.lower()

    # Switch back to web filter
    await history_page.set_interface_type_filter("web")

    # Wait for URL to update and content to reload
    await page.wait_for_url("**/history?*interface_type=web*", timeout=5000)

    # Wait for content to finish loading after filter change
    await page.wait_for_selector(
        "[class*='conversationsContainer'], .conversationsContainer, [class*='emptyState'], .emptyState",
        state="visible",
        timeout=10000,
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_date_range_filtering(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test date range filtering functionality."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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

    # Wait for URL to update with date filters
    await page.wait_for_url("**/history?*date_from=2024-01-01*", timeout=5000)

    # Check URL contains date filters
    current_url = page.url
    assert "date_from=2024-01-01" in current_url
    assert "date_to=2024-12-31" in current_url

    # Clear date filters and verify they're removed from URL
    clear_button = page.locator("details button:has-text('Clear Filters')")
    await clear_button.click()

    # Wait for URL to update without date filters
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            return !url.includes('date_from') && !url.includes('date_to');
        }""",
        timeout=5000,
    )

    # URL should no longer have date filters
    cleared_url = page.url
    assert "date_from" not in cleared_url
    assert "date_to" not in cleared_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_conversation_id_filter(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test conversation ID filtering functionality."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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

    # Wait for URL to update with conversation ID filter
    await page.wait_for_url(
        f"**/history?*conversation_id={test_conv_id}*", timeout=5000
    )

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
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test interaction of multiple filters applied together."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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

    # Wait for URL to update with conversation ID filter
    await page.wait_for_url("**/history?*conversation_id=web_conv*", timeout=5000)

    # Verify all filters are in URL
    current_url = page.url
    assert "interface_type=web" in current_url
    assert "date_from=2024-08-01" in current_url
    assert "conversation_id=web_conv" in current_url

    # Clear all filters
    clear_button = page.locator("details button:has-text('Clear Filters')")
    await clear_button.click()

    # Wait for URL to update without filters
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            return !url.includes('date_from') && !url.includes('conversation_id');
        }""",
        timeout=5000,
    )

    # Verify all filter values are cleared
    interface_value = await history_page.get_interface_type_filter_value()
    date_value = await date_from_input.input_value()
    conv_value = await conv_input.input_value()

    assert interface_value == "_all"
    assert not date_value
    assert not conv_value

    # URL should be clean
    cleared_url = page.url
    assert "interface_type" not in cleared_url
    assert "date_from" not in cleared_url
    assert "conversation_id" not in cleared_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filter_validation_and_error_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test filter validation and error handling."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_history_filter_state_management(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test comprehensive filter state management."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
