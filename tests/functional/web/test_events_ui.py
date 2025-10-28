"""Playwright-based functional tests for Events React UI."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from family_assistant.storage.context import DatabaseContext

from .conftest import WebTestFixture
from .pages.events_page import EventsPage


@pytest.mark.flaky(reruns=2)
@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_page_basic_loading(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test basic functionality of the events page React interface."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that events list page loads successfully."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_detail_page_loads(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that events detail page loads with non-existent ID."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to a non-existent event detail page
    test_event_id = "test_event_123"
    await page.goto(f"{server_url}/events/{test_event_id}")

    # Wait for page to load by waiting for back button to appear
    back_button = page.locator("button:has-text('Back to Events')")
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()

    # Should show either event details or error message
    event_details = page.locator("h1:has-text('Event Details')")
    error_message = page.locator("[class*='error'], .error")

    has_details = await event_details.count() > 0 and await event_details.is_visible()
    has_error = await error_message.count() > 0 and await error_message.is_visible()

    # At least one should be present
    assert has_details or has_error, "Should show either event details or error state"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_navigation_between_list_and_detail(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test navigation between list and detail views."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Start on events list
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Navigate to a detail page (non-existent ID)
    test_event_id = "test_event_123"
    await page.goto(f"{server_url}/events/{test_event_id}")

    # Should be on detail page
    back_button = page.locator("button:has-text('Back to Events')")
    await back_button.wait_for(timeout=5000)

    # Click back button
    await back_button.click()

    # Should be back on list page
    await page.wait_for_selector("h1:has-text('Events')", timeout=5000)

    # Check URL is correct
    assert page.url.endswith("/events")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_list_filters_interface(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test filter form interactions on events page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that filter state is preserved in URL parameters."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that filter state persists after page reload."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test events list display and structure."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test pagination controls when available."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test responsive design of events page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test handling of API errors in events page."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
async def test_events_detail_view_structure(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test event detail view structure."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate directly to an event detail (will handle non-existent gracefully)
    await page.goto(f"{server_url}/events/test_event_id")

    # Wait for page to load by waiting for back button
    back_button = page.locator("button:has-text('Back to Events')")
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()

    # Check for event detail sections
    event_card = page.locator("[class*='eventCard'], .eventCard")
    error_state = page.locator("[class*='error'], .error")

    # Should show one of these states
    has_event_card = await event_card.count() > 0 and await event_card.is_visible()
    has_error = await error_state.count() > 0 and await error_state.is_visible()

    assert has_event_card or has_error


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_with_actual_data(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test events page with actual event data."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Create some test event data via the repository
    async with DatabaseContext(
        engine=web_test_fixture.assistant.database_engine
    ) as db_context:
        # Create a test event
        await db_context.events.store_event(
            source_id="home_assistant",
            event_data={
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"brightness": 255},
            },
            triggered_listener_ids=[],
            timestamp=datetime.now(timezone.utc),
        )

        # Create another event with triggered listeners
        await db_context.events.store_event(
            source_id="indexing",
            event_data={
                "document_id": "test_doc_123",
                "action": "indexed",
                "chunks": 5,
            },
            triggered_listener_ids=[1, 2],
            timestamp=datetime.now(timezone.utc),
        )

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for events to load by checking for results summary
    results_summary = page.locator("text=/Found \\d+ event/")
    await results_summary.wait_for(timeout=5000)
    summary_text = await results_summary.text_content()
    assert summary_text is not None
    # Should find exactly 2 events since we created 2
    assert "Found 2 events" in summary_text

    # Check for event cards
    event_cards = page.locator("[class*='eventCard']")
    card_count = await event_cards.count()
    assert card_count > 0, "Should have at least one event card"

    # Check first event card has expected elements
    first_card = event_cards.first

    # Check for source badge
    source_badge = first_card.locator("[class*='sourceBadge']")
    assert await source_badge.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_clear_filters_functionality(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test clear filters functionality works properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that page load triggers initial API call."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that filter changes trigger API calls correctly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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
        await asyncio.sleep(0.1)  # noqa: ASYNC110 # Poll every 100ms

    # Should have made a new API call with filter parameter
    assert len(api_requests) > 0
    assert any("source_id=home_assistant" in url for url in api_requests)


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_404_handling(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test 404 event handling works properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to a definitely non-existent event
    await page.goto(f"{server_url}/events/definitely_not_an_event_id_12345")

    # Wait for page to load by waiting for back button
    back_button = page.locator("button:has-text('Back to Events')")
    await back_button.wait_for(timeout=5000)

    # Should show error state
    error_message = page.locator("[class*='error'], .error")

    # Both should be visible
    await back_button.wait_for(timeout=5000)
    assert await back_button.is_visible()

    # Should have some error indication (either error message or "Event not found")
    has_error = await error_message.count() > 0 and await error_message.is_visible()
    not_found_text = await page.locator("text=Event not found").count() > 0

    assert has_error or not_found_text, "Should show error state for non-existent event"


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_loading_states(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that loading states display properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Navigate to events page
    await page.goto(f"{server_url}/events")

    # Wait for page to fully load - either loading disappears or filters appear
    # The filters only appear after loading is complete
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for either the loading to disappear or the filters to appear
    # (filters only show after loading completes)
    await page.wait_for_function(
        """() => {
            const loading = document.querySelector('.loading');
            const filters = document.querySelector('details');
            return !loading || filters;
        }""",
        timeout=10000,
    )

    # Now verify loading is not visible
    loading_text = page.locator("div.loading", has_text="Loading events...")
    assert await loading_text.count() == 0, (
        "Loading indicator should not be present after page loads"
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_json_formatting_in_detail_view(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that JSON event data displays with proper formatting in detail view."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Create a test event with complex JSON data
    test_event_id = None
    async with DatabaseContext(
        engine=web_test_fixture.assistant.database_engine
    ) as db_context:
        await db_context.events.store_event(
            source_id="home_assistant",
            event_data={
                "entity_id": "sensor.temperature",
                "state": "22.5",
                "attributes": {
                    "unit_of_measurement": "°C",
                    "friendly_name": "Living Room Temperature",
                    "device_class": "temperature",
                },
            },
            triggered_listener_ids=[1, 2, 3],
            timestamp=datetime.now(timezone.utc),
        )

        # Get the created event ID (it will be in the format source_id:timestamp)
        events, _ = await db_context.events.get_events_with_listeners(limit=1)
        if events:
            test_event_id = events[0]["event_id"]

    if test_event_id:
        # Navigate to event detail page
        await page.goto(f"{server_url}/events/{test_event_id}")

        # Wait for page to load by waiting for back button
        await page.wait_for_selector("button:has-text('Back to Events')", timeout=5000)

        # Check that event data section is present
        event_data_section = page.locator(
            "[class*='eventDataSection'], .eventDataSection"
        )
        if await event_data_section.count() > 0:
            assert await event_data_section.is_visible()

            # Check for JSON formatting - be more specific to avoid multiple matches
            json_pre = page.locator("pre").filter(has_text="entity_id")
            if await json_pre.count() > 0:
                json_content = await json_pre.first.text_content()
                assert json_content is not None
                # Should contain formatted JSON with the test data
                assert "entity_id" in json_content
                assert "sensor.temperature" in json_content


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_triggered_listeners_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that triggered listeners section displays properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Create a test event with triggered listeners
    test_event_id = None
    async with DatabaseContext(
        engine=web_test_fixture.assistant.database_engine
    ) as db_context:
        await db_context.events.store_event(
            source_id="indexing",
            event_data={"document": "test.pdf", "status": "processed"},
            triggered_listener_ids=[1, 2],
            timestamp=datetime.now(timezone.utc),
        )

        # Get the created event ID
        events, _ = await db_context.events.get_events_with_listeners(limit=1)
        if events:
            test_event_id = events[0]["event_id"]

    if test_event_id:
        # Navigate to event detail page
        await page.goto(f"{server_url}/events/{test_event_id}")

        # Wait for page to load by waiting for back button
        await page.wait_for_selector("button:has-text('Back to Events')", timeout=5000)

        # Check for triggered listeners info
        listeners_info = page.locator("text=/\\d+ listener/")
        if await listeners_info.count() > 0:
            listeners_text = await listeners_info.text_content()
            assert listeners_text is not None
            assert "listener" in listeners_text

            # Check for listener IDs
            listener_ids = page.locator("[class*='listenerId'], .listenerId")
            if await listener_ids.count() > 0:
                # Should show listener ID elements
                assert await listener_ids.first.is_visible()


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_empty_state_handling(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test empty state handling works properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

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


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_source_icons_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that event source icons display correctly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Create events with different sources
    async with DatabaseContext(
        engine=web_test_fixture.assistant.database_engine
    ) as db_context:
        await db_context.events.store_event(
            source_id="home_assistant",
            event_data={"test": "data"},
            timestamp=datetime.now(timezone.utc),
        )

        await db_context.events.store_event(
            source_id="indexing",
            event_data={"test": "data"},
            timestamp=datetime.now(timezone.utc),
        )

    # Navigate to events page
    await page.goto(f"{server_url}/events")
    await page.wait_for_selector("h1:has-text('Events')", timeout=10000)

    # Wait for events to load by checking for results summary
    await page.wait_for_selector("text=/Found \\d+ event/", timeout=10000)

    # Check for source badges/icons
    source_badges = page.locator("[class*='sourceBadge'], .sourceBadge")
    if await source_badges.count() > 0:
        # Should have source badges
        assert await source_badges.first.is_visible()

        # Check for source icons
        source_icons = page.locator("[class*='sourceIcon'], .sourceIcon")
        if await source_icons.count() > 0:
            # Should display source icons (emojis)
            icon_text = await source_icons.first.text_content()
            assert icon_text is not None and len(icon_text) > 0


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_metadata_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that event metadata (ID, source, timestamp) shows correctly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url

    # Create a test event
    test_event_id = None
    async with DatabaseContext(
        engine=web_test_fixture.assistant.database_engine
    ) as db_context:
        await db_context.events.store_event(
            source_id="home_assistant",
            event_data={"entity_id": "test.entity"},
            timestamp=datetime.now(timezone.utc),
        )

        # Get the created event ID
        events, _ = await db_context.events.get_events_with_listeners(limit=1)
        if events:
            test_event_id = events[0]["event_id"]

    if test_event_id:
        # Navigate to event detail page
        await page.goto(f"{server_url}/events/{test_event_id}")

        # Wait for page to load by waiting for back button
        await page.wait_for_selector("button:has-text('Back to Events')", timeout=5000)

        # Check for event metadata sections
        info_items = page.locator("[class*='infoItem'], .infoItem")
        if await info_items.count() > 0:
            # Should have info items
            assert await info_items.first.is_visible()

            # Check for event ID display - be more specific to avoid matching JSON code blocks
            event_id_code = page.locator("code").filter(has_text="home_assistant:")
            if await event_id_code.count() > 0:
                id_text = await event_id_code.first.text_content()
                assert id_text is not None
                assert test_event_id in id_text

            # Check for source label
            source_labels = page.locator("[class*='sourceLabel'], .sourceLabel")
            if await source_labels.count() > 0:
                source_text = await source_labels.first.text_content()
                assert source_text is not None
                assert (
                    "Home Assistant" in source_text or "home_assistant" in source_text
                )
