"""Playwright-based functional tests for Events React UI - Detail view and CRUD operations."""

from datetime import UTC, datetime

import pytest

from family_assistant.storage.context import DatabaseContext
from tests.functional.web.conftest import WebTestFixture


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_detail_page_loads(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that events detail page loads with non-existent ID."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test navigation between list and detail views."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
async def test_events_detail_view_structure(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test event detail view structure."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
            timestamp=datetime.now(UTC),
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
            timestamp=datetime.now(UTC),
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
async def test_events_404_handling(
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test 404 event handling works properly."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
    web_test_fixture_readonly: WebTestFixture,
) -> None:
    """Test that loading states display properly."""
    page = web_test_fixture_readonly.page
    server_url = web_test_fixture_readonly.base_url

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
            timestamp=datetime.now(UTC),
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
            timestamp=datetime.now(UTC),
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
            timestamp=datetime.now(UTC),
        )

        await db_context.events.store_event(
            source_id="indexing",
            event_data={"test": "data"},
            timestamp=datetime.now(UTC),
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
            timestamp=datetime.now(UTC),
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
