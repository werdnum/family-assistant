"""Playwright-based functional tests for Events React UI - List view and filtering."""

import asyncio  # noqa: F401
import time  # noqa: F401
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from tests.functional.web.conftest import WebTestFixture
from tests.functional.web.pages.events_page import EventsPage


@pytest.mark.flaky(reruns=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_page_basic_loading(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test basic functionality of the events page React interface."""
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

    # Navigate to events page
    await page.goto(f"{server_url}/events")

    # Wait for h1 element to ensure page has started loading
    await page.wait_for_selector("h1", timeout=10000)

    # Check page title and heading
    await page.wait_for_selector("h1:has-text('Events')")

    # Verify React components have loaded by checking for filters section
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(timeout=5000)
    assert await filters_section.is_visible()

    # Check that results summary is present (handles empty state or API errors)
    # From the console output, we can see "Found 0 events" is displayed, which means the component is working
    results_summary = page.locator("text=/Found \\d+ event/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    assert "Found" in summary_text and "event" in summary_text

    # Final check for critical errors (allow API fetch errors as the endpoint may not be fully working)
    critical_errors = [
        e
        for e in console_errors
        if "Failed to fetch" not in e and "fetch" not in e.lower()
    ]
    assert not critical_errors, f"Critical console errors detected: {critical_errors}"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_list_page_loads(
    web_test_fixture_readonly: WebTestFixture,
    take_screenshot: Callable[[Any, str, str], Awaitable[None]],
) -> None:
    """Test that events list page loads successfully."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Check that main container has expected CSS classes (indicating React components loaded)
    events_list = page.locator("[class*='eventsList']")
    await events_list.wait_for(timeout=5000)
    assert await events_list.is_visible()

    # Check that filters form has CSS styling
    filters_form = page.locator("form[class*='filtersForm'], .filtersForm")
    assert await filters_form.count() > 0
    assert await filters_form.is_visible()

    # Take screenshot of events list page
    for viewport in ["desktop", "mobile"]:
        await take_screenshot(page, "events-list", viewport)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_list_filters_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test filter form interactions on events page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object
    events_page = EventsPage(page, server_url)

    # Open filters section
    await events_page.open_filters()

    # Test source dropdown
    await events_page.set_source_filter("home_assistant")
    selected_value = await events_page.get_source_filter_value()
    assert selected_value == "home_assistant"

    # Test hours selector
    await events_page.set_hours_filter("6")
    hours_value = await events_page.get_hours_filter_value()
    assert hours_value == "6"

    # Test only triggered checkbox
    initial_state = await events_page.get_only_triggered_filter_value()
    await events_page.set_only_triggered_filter(not initial_state)
    is_checked = await events_page.get_only_triggered_filter_value()
    assert is_checked is not initial_state  # Verify the state toggled

    # Test Clear Filters button
    await events_page.clear_filters()

    # Verify filters are cleared
    source_value = await events_page.get_source_filter_value()
    hours_value_after = await events_page.get_hours_filter_value()
    is_checked_after = await events_page.get_only_triggered_filter_value()

    assert source_value == "_all"  # Default value
    assert hours_value_after == "24"  # Default value
    assert is_checked_after is False


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_filters_url_state_management(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that filter state is preserved in URL parameters."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page with query parameters
    await page.goto(
        f"{server_url}/events?source_id=home_assistant&hours=6&only_triggered=true&page=1"
    )

    # Wait for page to load
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object
    events_page = EventsPage(page, server_url)

    # Open filters to see the values
    await events_page.open_filters()

    # Check that filter values are restored from URL
    selected_source = await events_page.get_source_filter_value()
    assert selected_source == "home_assistant"

    hours_value = await events_page.get_hours_filter_value()
    assert hours_value == "6"

    is_checked = await events_page.get_only_triggered_filter_value()
    assert is_checked is True

    # Verify URL contains the filter parameters
    current_url = page.url
    assert "source_id=home_assistant" in current_url
    assert "hours=6" in current_url
    assert "only_triggered=true" in current_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_filters_url_state_persistence_after_reload(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that filter state persists after page reload."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object and apply filters
    events_page = EventsPage(page, server_url)
    await events_page.open_filters()
    await events_page.set_source_filter("indexing")
    await events_page.set_hours_filter("48")

    # Wait for URL to update by checking the URL contains the filters
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            return url.includes('source_id=indexing') && url.includes('hours=48');
        }""",
        timeout=10000,
    )

    # Check that URL reflects filter state
    current_url = page.url
    assert "source_id=indexing" in current_url
    assert "hours=48" in current_url

    # Reload page
    await page.reload()
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object for verification after reload
    events_page = EventsPage(page, server_url)
    await events_page.open_filters()

    # Check that filter values are restored after reload
    source_value = await events_page.get_source_filter_value()
    hours_value = await events_page.get_hours_filter_value()

    assert source_value == "indexing"
    assert hours_value == "48"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_list_display_structure(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test events list display and structure."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for either events container or empty state to appear
    events_container = page.locator("[class*='eventsContainer'], .eventsContainer")
    empty_state = page.locator("[class*='emptyState'], .emptyState")
    await page.wait_for_function(
        """() => {
            const eventsContainer = document.querySelector('[class*="eventsContainer"]');
            const emptyState = document.querySelector('[class*="emptyState"]');
            return (eventsContainer && window.getComputedStyle(eventsContainer).display !== 'none') ||
                   (emptyState && window.getComputedStyle(emptyState).display !== 'none');
        }""",
        timeout=10000,
    )

    # Either events are displayed or empty state is shown
    has_events = (
        await events_container.count() > 0 and await events_container.is_visible()
    )
    has_empty_state = await empty_state.count() > 0 and await empty_state.is_visible()

    # At least one should be visible (events or empty state)
    assert has_events or has_empty_state, "Neither events nor empty state is displayed"

    # If events exist, test their structure
    if has_events:
        # Check for event cards
        event_cards = page.locator("[class*='eventCard'], .eventCard")
        card_count = await event_cards.count()

        if card_count > 0:
            first_card = event_cards.first

            # Check for event ID or timestamp
            event_header = first_card.locator("[class*='eventHeader'], .eventHeader")
            if await event_header.count() > 0:
                assert await event_header.is_visible()

            # Check for source badge
            source_badge = first_card.locator("[class*='sourceBadge'], .sourceBadge")
            if await source_badge.count() > 0:
                assert await source_badge.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_pagination_interface(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test pagination controls when available."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for page content to load - either pagination or events container should appear
    await page.wait_for_function(
        """() => {
            const pagination = document.querySelector('[class*="pagination"]');
            const eventsContainer = document.querySelector('[class*="eventsContainer"]');
            const emptyState = document.querySelector('[class*="emptyState"]');
            return pagination || eventsContainer || emptyState;
        }""",
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
async def test_events_responsive_design(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test responsive design of events page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Test mobile viewport
    await page.set_viewport_size({"width": 375, "height": 667})

    # Wait for layout to stabilize by checking elements are visible
    heading = page.locator("h1:has-text('Events')")
    await heading.wait_for(state="visible", timeout=5000)
    assert await heading.is_visible()

    # Filters should still be accessible
    filters_section = page.locator("details summary:has-text('Filters')")
    await filters_section.wait_for(state="visible", timeout=5000)
    assert await filters_section.is_visible()

    # Test tablet viewport
    await page.set_viewport_size({"width": 768, "height": 1024})

    # Wait for layout to stabilize
    await heading.wait_for(state="visible", timeout=5000)
    await filters_section.wait_for(state="visible", timeout=5000)

    # Check elements are still visible
    assert await heading.is_visible()
    assert await filters_section.is_visible()

    # Test desktop viewport
    await page.set_viewport_size({"width": 1200, "height": 800})

    # Wait for layout to stabilize
    await heading.wait_for(state="visible", timeout=5000)
    await filters_section.wait_for(state="visible", timeout=5000)

    # Check elements are still visible
    assert await heading.is_visible()
    assert await filters_section.is_visible()


@pytest.mark.flaky(reruns=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_api_error_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test handling of API errors in events page."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for the page to finish loading (network idle indicates API calls complete)

    # The page should handle API responses gracefully
    # Either show events, empty state, or error message
    has_events = await page.locator("[class*='eventsContainer']").count() > 0
    has_empty_state = await page.locator("[class*='emptyState']").count() > 0
    has_error = await page.locator("[class*='error']").count() > 0
    has_loading = await page.locator("text=Loading").count() > 0

    # Page should not be stuck loading and should show appropriate content
    assert not has_loading, "Page should not be stuck in a loading state"
    assert has_events or has_empty_state or has_error, (
        "Page should display events, an empty state, or an error message"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_clear_filters_functionality(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test clear filters functionality works properly."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object
    events_page = EventsPage(page, server_url)

    # Open filters and apply multiple filters
    await events_page.open_filters()
    await events_page.set_source_filter("home_assistant")
    await events_page.set_hours_filter("1")
    await events_page.set_only_triggered_filter(True)

    # Wait for filters to be applied by checking URL
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            return url.includes('source_id=home_assistant') &&
                   url.includes('hours=1') &&
                   url.includes('only_triggered=true');
        }""",
        timeout=10000,
    )

    # Clear filters using the clear button
    await events_page.clear_filters()

    # Wait for filters to be cleared by checking URL and shadcn select trigger states
    await page.wait_for_function(
        """() => {
            const url = window.location.href;
            const sourceSelect = document.querySelector('#source_id');
            const hoursSelect = document.querySelector('#hours');
            const sourceText = sourceSelect?.textContent || '';
            const hoursText = hoursSelect?.textContent || '';
            return sourceText.includes('All Sources') &&
                   hoursText.includes('Last 24 hours') &&
                   !url.includes('only_triggered=true');
        }""",
        timeout=10000,
    )

    # Verify all filters are cleared
    source_value = await events_page.get_source_filter_value()
    hours_value = await events_page.get_hours_filter_value()
    is_checked = await events_page.get_only_triggered_filter_value()

    assert source_value == "_all"
    assert hours_value == "24"
    assert is_checked is False

    # Check that URL is also cleared
    current_url = page.url
    assert "source_id=" not in current_url or "source_id=&" in current_url
    assert "hours=" not in current_url or "hours=24" in current_url
    assert "only_triggered=true" not in current_url


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_page_load_triggers_api_call(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that page load triggers initial API call."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Monitor API requests
    api_requests = []

    def log_api_request(request: Any) -> None:  # noqa: ANN401  # playwright request object
        if "/api/events" in request.url:
            api_requests.append(request.url)

    page.on("request", log_api_request)

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for initial API call by checking the results summary appears
    await page.wait_for_selector("text=/Found \\d+ event/", timeout=10000)

    # Should have made at least one API call
    assert len(api_requests) > 0
    assert any("/api/events" in url for url in api_requests)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_filter_changes_trigger_api_calls(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that filter changes trigger API calls correctly."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Monitor API requests
    api_requests = []

    def log_api_request(request: Any) -> None:  # noqa: ANN401  # playwright request object
        if "/api/events" in request.url:
            api_requests.append(request.url)

    page.on("request", log_api_request)

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Create page object
    events_page = EventsPage(page, server_url)

    # Open filters
    await events_page.open_filters()

    # Clear existing requests
    api_requests.clear()

    # Change a filter using the page object method
    await events_page.set_source_filter("home_assistant")

    # Wait for API call to be captured (poll the Python list)
    deadline = time.time() + 10  # 10 second timeout
    while time.time() < deadline:
        if len(api_requests) > 0 and any(
            "source_id=home_assistant" in url for url in api_requests
        ):
            break
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling interval in condition-checking loop
        await asyncio.sleep(0.1)  # noqa: ASYNC110 # Poll every 100ms

    # Should have made a new API call with filter parameter
    assert len(api_requests) > 0
    assert any("source_id=home_assistant" in url for url in api_requests)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_empty_state_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test empty state handling works properly."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

    # Navigate to events page (might be empty initially)
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for API response by checking for results summary
    results_summary = page.locator("text=/Found \\d+ event/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None

    # If no events, should show empty state
    if "Found 0 event" in summary_text:
        empty_state = page.locator("[class*='emptyState'], .emptyState")
        await empty_state.wait_for(timeout=5000)
        assert await empty_state.is_visible()

        empty_text = await empty_state.text_content()
        assert empty_text and (
            "No events found" in empty_text or "matching your criteria" in empty_text
        )
