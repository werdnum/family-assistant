"""Playwright-based functional tests for Events React UI - Detail view and CRUD operations."""

from datetime import UTC, datetime

import pytest
from playwright.async_api import expect

from family_assistant.storage.database import Database
from tests.functional.web.conftest import WebTestFixture


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
async def test_events_with_actual_data(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test events page with actual event data."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None

    # Create some test event data via the repository
    db_context = Database(engine=engine)
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

    await expect(page.get_by_text("Event not found")).to_be_visible()
    await expect(page.get_by_role("button", name="Back to Events")).to_be_visible()
    await expect(page.get_by_role("heading", name="Event Details")).to_have_count(0)


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
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None

    # Create a test event with complex JSON data
    db_context = Database(engine=engine)
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

    events, _ = await db_context.events.get_events_with_listeners(limit=1)
    assert len(events) == 1
    await page.goto(f"{server_url}/events/{events[0]['event_id']}")

    event_data = page.locator("[class*='eventDataSection'] pre")
    await expect(event_data).to_contain_text('"entity_id": "sensor.temperature"')
    await expect(event_data).to_contain_text(
        '"friendly_name": "Living Room Temperature"'
    )


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_triggered_listeners_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that triggered listeners section displays properly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None

    # Create a test event with triggered listeners
    db_context = Database(engine=engine)
    await db_context.events.store_event(
        source_id="indexing",
        event_data={"document": "test.pdf", "status": "processed"},
        triggered_listener_ids=[1, 2],
        timestamp=datetime.now(UTC),
    )

    events, _ = await db_context.events.get_events_with_listeners(limit=1)
    assert len(events) == 1
    await page.goto(f"{server_url}/events/{events[0]['event_id']}")

    await expect(page.get_by_text("2 listeners")).to_be_visible()
    await expect(page.locator("span[class*='listenerId']")).to_have_text(["#1", "#2"])


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_events_source_icons_display(
    web_test_fixture: WebTestFixture,
) -> None:
    """Test that event source icons display correctly."""
    page = web_test_fixture.page
    server_url = web_test_fixture.base_url
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None

    # Create events with different sources
    db_context = Database(engine=engine)
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
    engine = web_test_fixture.assistant.database_engine
    assert engine is not None

    # Create a test event
    db_context = Database(engine=engine)
    await db_context.events.store_event(
        source_id="home_assistant",
        event_data={"entity_id": "test.entity"},
        timestamp=datetime.now(UTC),
    )

    events, _ = await db_context.events.get_events_with_listeners(limit=1)
    assert len(events) == 1
    test_event_id = events[0]["event_id"]
    await page.goto(f"{server_url}/events/{test_event_id}")

    await expect(page.locator("[class*='eventIdCode']")).to_have_text(test_event_id)
    await expect(page.get_by_text("Timestamp:", exact=True)).to_be_visible()
