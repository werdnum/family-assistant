"""Basic event handling tests for the event listener system."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import janus
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.storage import EventStorage
from family_assistant.storage import get_db_context
from family_assistant.storage.events import (
    EventSourceType,
    cleanup_old_events,
    recent_events_table,
)
from family_assistant.tools.events import query_recent_events_tool
from family_assistant.tools.events import (
    test_event_listener_tool as event_listener_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext


class MockFiredEvent:
    """Mock for Home Assistant FiredEvent object."""

    def __init__(
        self,
        entity_id: str,
        old_state: str,
        new_state: str,
        event_type: str = "state_changed",
    ) -> None:
        self.event_type = event_type
        self.data = MockEventData(entity_id, old_state, new_state)


class MockEventData:
    """Mock for event data object."""

    def __init__(self, entity_id: str, old_state: str, new_state: str) -> None:
        self.entity_id = entity_id
        self.old_state = MockState(old_state) if old_state else None
        self.new_state = MockState(new_state) if new_state else None


class MockState:
    """Mock for Home Assistant state object."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.attributes = {"friendly_name": f"Test {state}"}
        self.last_changed = datetime.now(UTC).isoformat()


def safe_json_loads(data: str | dict | list) -> Any:  # noqa: ANN401  # JSON can be any type
    """
    Safely load JSON data that might already be parsed.

    SQLite returns JSON columns as strings, while PostgreSQL returns them as
    already-parsed dicts/lists. This function handles both cases.
    """
    if isinstance(data, dict | list):
        # Already parsed (PostgreSQL)
        return data
    # String that needs parsing (SQLite)
    return json.loads(data)


@pytest.mark.asyncio
async def test_event_storage_sampling(db_engine: AsyncEngine) -> None:
    """Test that event storage properly samples events (1 per entity per hour)."""
    storage = EventStorage(
        sample_interval_hours=1.0, get_db_context_func=lambda: get_db_context(db_engine)
    )

    # First event should be stored
    await storage.store_event(
        EventSourceType.home_assistant,
        {"entity_id": "light.kitchen", "state": "on"},
        None,
    )

    # Second event for same entity within the hour should not be stored
    await storage.store_event(
        EventSourceType.home_assistant,
        {"entity_id": "light.kitchen", "state": "off"},
        None,
    )

    # Event for different entity should be stored
    await storage.store_event(
        EventSourceType.home_assistant,
        {"entity_id": "light.bedroom", "state": "on"},
        None,
    )

    # Check stored events
    async with get_db_context(db_engine) as db_ctx:
        result = await db_ctx.fetch_all(
            text("SELECT COUNT(*) as count FROM recent_events")
        )
        assert result[0]["count"] == 2  # Only 2 events should be stored


@pytest.mark.asyncio
async def test_home_assistant_event_processing(db_engine: AsyncEngine) -> None:
    """Test processing Home Assistant state change events."""
    # Create mock HA client
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"
    mock_client.verify_ssl = True

    # Create event source
    ha_source = HomeAssistantSource(client=mock_client)

    # Create event processor
    processor = EventProcessor(
        sources={"ha_test": ha_source},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    # Set processor as running (normally done by start())
    processor._running = True

    # Initialize the janus queue (normally done by start())
    ha_source._event_queue = janus.Queue(maxsize=1000)

    # Process a state change event
    event = MockFiredEvent(
        entity_id="sensor.temperature", old_state="20.5", new_state="21.0"
    )

    # Simulate the sync handler adding event to queue
    ha_source._handle_event_sync("state_changed", event)

    # Process the event from the queue (normally done by _process_events task)
    # We'll manually process it here since we're not running the full async loop
    if ha_source._event_queue and not ha_source._event_queue.async_q.empty():
        queued_event = await ha_source._event_queue.async_q.get()
        await processor.process_event("home_assistant", queued_event)
        ha_source._event_queue.async_q.task_done()

    # Give a small delay to ensure async writes complete
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Allowing async write completion
    await asyncio.sleep(0.1)

    # Query recent events
    async with get_db_context(db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await query_recent_events_tool(
            exec_context=exec_context, source_id="home_assistant", hours=1
        )

        # Parse JSON result
        result_data = json.loads(result)

        assert result_data["count"] >= 1
        assert result_data["source_filter"] == "home_assistant"

        # Check event data
        events = result_data["events"]
        assert len(events) >= 1

        # Find our temperature event
        temp_event = None
        for event in events:
            if event["event_data"].get("entity_id") == "sensor.temperature":
                temp_event = event
                break

        assert temp_event is not None
        assert temp_event["source_id"] == "home_assistant"
        assert temp_event["event_data"]["event_type"] == "state_changed"
        assert temp_event["event_data"]["old_state"]["state"] == "20.5"
        assert temp_event["event_data"]["new_state"]["state"] == "21.0"

    # Clean up janus queue
    await ha_source._event_queue.aclose()


@pytest.mark.asyncio
async def test_event_listener_matching(db_engine: AsyncEngine) -> None:
    """Test event matching against listener conditions."""
    # Add a test listener
    async with get_db_context(db_engine) as db_ctx:
        await db_ctx.execute_with_retry(
            text("""INSERT INTO event_listeners
                 (name, match_conditions, source_id, enabled, conversation_id)
                 VALUES (:name, :conditions, :source_id, :enabled, :conversation_id)"""),
            {
                "name": "Temperature Monitor",
                "conditions": json.dumps({
                    "entity_id": "sensor.temperature",
                    "new_state.state": "26.0",  # Simple equality match
                }),
                "source_id": EventSourceType.home_assistant.value,
                "enabled": True,
                "conversation_id": "test_conversation",
            },
        )

    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    await processor._refresh_listener_cache()

    # Test matching
    match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "26.0"}}
    no_match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "24.0"}}

    listeners = processor._listener_cache.get("home_assistant", [])
    assert len(listeners) == 1

    assert processor._check_match_conditions(
        match_event,
        listeners[0]["match_conditions"],
        listeners[0].get("condition_script"),
    )
    assert not processor._check_match_conditions(
        no_match_event,
        listeners[0]["match_conditions"],
        listeners[0].get("condition_script"),
    )


@pytest.mark.asyncio
async def test_test_event_listener_tool_matches_person_coming_home(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool correctly matches person coming home."""
    # Arrange
    async with get_db_context(db_engine) as db_ctx:
        await db_ctx.execute_with_retry(text("DELETE FROM recent_events"))

        now = datetime.now(UTC)
        events_to_insert = [
            {
                "event_id": "test_1",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "entity_id": "person.alex",
                    "old_state": {"state": "Away"},
                    "new_state": {"state": "Home", "last_changed": now.isoformat()},
                }),
                "timestamp": now,
            },
            {
                "event_id": "test_2",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "entity_id": "person.alex",
                    "old_state": {"state": "Home"},
                    "new_state": {"state": "Away", "last_changed": now.isoformat()},
                }),
                "timestamp": now,
            },
            {
                "event_id": "test_3",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "entity_id": "sensor.temperature",
                    "old_state": {"state": "20"},
                    "new_state": {
                        "state": "22",
                        "attributes": {"unit_of_measurement": "°C"},
                    },
                }),
                "timestamp": now,
            },
        ]

        for event in events_to_insert:
            await db_ctx.execute_with_retry(
                text("""INSERT INTO recent_events
                       (event_id, source_id, event_data, timestamp)
                       VALUES (:event_id, :source_id, :event_data, :timestamp)"""),
                event,
            )

    # Act
    async with get_db_context(db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await event_listener_test_tool(
            exec_context,
            source=EventSourceType.home_assistant.value,
            match_conditions={
                "entity_id": "person.alex",
                "new_state.state": "Home",
            },
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert data["matched_count"] == 1
    assert data["total_tested"] >= 2
    assert len(data["matched_events"]) == 1
    assert data["matched_events"][0]["event_data"]["entity_id"] == "person.alex"
    assert data["matched_events"][0]["event_data"]["new_state"]["state"] == "Home"


@pytest.mark.asyncio
async def test_test_event_listener_tool_no_match_wrong_state(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool provides analysis when no events match."""
    # Arrange
    async with get_db_context(db_engine) as db_ctx:
        await db_ctx.execute_with_retry(text("DELETE FROM recent_events"))

        now = datetime.now(UTC)
        await db_ctx.execute_with_retry(
            text("""INSERT INTO recent_events
                   (event_id, source_id, event_data, timestamp)
                   VALUES (:event_id, :source_id, :event_data, :timestamp)"""),
            {
                "event_id": "test_1",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "entity_id": "person.alex",
                    "old_state": {"state": "Away"},
                    "new_state": {"state": "Home", "last_changed": now.isoformat()},
                }),
                "timestamp": now,
            },
        )

    # Act
    async with get_db_context(db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await event_listener_test_tool(
            exec_context,
            source=EventSourceType.home_assistant.value,
            match_conditions={
                "entity_id": "person.alex",
                "new_state.state": "Vacation",  # This state doesn't exist
            },
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert data["matched_count"] == 0
    assert data["total_tested"] >= 1
    assert data["analysis"] is not None
    assert len(data["analysis"]) > 0
    # Should mention the actual state values found
    analysis_text = " ".join(data["analysis"])
    assert "new_state.state" in analysis_text or "Field" in analysis_text


@pytest.mark.asyncio
async def test_test_event_listener_tool_empty_conditions_error(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool returns error for empty match conditions."""
    # Arrange - no events needed for this test

    # Act
    async with get_db_context(db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await event_listener_test_tool(
            exec_context,
            source=EventSourceType.home_assistant.value,
            match_conditions={},
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert "error" in data
    assert "condition" in data["message"].lower()


@pytest.mark.asyncio
async def test_cleanup_old_events(db_engine: AsyncEngine) -> None:
    """Test that old events are cleaned up correctly."""

    # Arrange - create events with different ages
    async with get_db_context(db_engine) as db_ctx:
        # Store events with different timestamps
        now = datetime.now(UTC)

        # Use EventStorage to store events
        storage = EventStorage(
            sample_interval_hours=0.01,  # Short interval for testing
            get_db_context_func=lambda: get_db_context(db_engine),
        )

        # Old event (should be cleaned up)
        old_event_data = {
            "entity_id": "test.old",
            "old_state": {"state": "old"},
            "new_state": {"state": "old"},
        }
        await storage.store_event(
            EventSourceType.home_assistant.value,
            old_event_data,
            None,
        )

        # Recent event (should NOT be cleaned up)
        recent_event_data = {
            "entity_id": "test.recent",
            "old_state": {"state": "recent"},
            "new_state": {"state": "recent"},
        }
        await storage.store_event(
            EventSourceType.home_assistant.value,
            recent_event_data,
            None,
        )

        # Update the created_at timestamp for the old event

        # Use SQLAlchemy's JSON operators for cross-database compatibility
        stmt = (
            update(recent_events_table)
            .where(
                recent_events_table.c.event_data["entity_id"].as_string() == "test.old"
            )
            .values(created_at=now - timedelta(hours=72))
        )

        await db_ctx.execute_with_retry(stmt)

    # Act - run cleanup with 48 hour retention
    async with get_db_context(db_engine) as db_ctx:
        deleted_count = await cleanup_old_events(db_ctx, retention_hours=48)

    # Assert - the cleanup function works correctly
    assert deleted_count == 1  # Exactly one old event was deleted
