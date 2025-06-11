"""
Functional tests for the event listener system.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.storage import EventStorage
from family_assistant.storage import get_db_context
from family_assistant.storage.events import EventSourceType
from family_assistant.tools.events import query_recent_events_tool
from family_assistant.tools.events import (
    test_event_listener_tool as event_listener_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext


class MockFiredEvent:
    """Mock for Home Assistant FiredEvent object."""

    def __init__(self, entity_id: str, old_state: str, new_state: str) -> None:
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
        self.last_changed = datetime.now(timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_event_storage_sampling(test_db_engine: AsyncEngine) -> None:
    """Test that event storage properly samples events (1 per entity per hour)."""
    storage = EventStorage(sample_interval_hours=1.0)

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
    async with get_db_context() as db_ctx:
        from sqlalchemy import text

        result = await db_ctx.fetch_all(
            text("SELECT COUNT(*) as count FROM recent_events")
        )
        assert result[0]["count"] == 2  # Only 2 events should be stored


@pytest.mark.asyncio
async def test_home_assistant_event_processing(test_db_engine: AsyncEngine) -> None:
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
        sources={"ha_test": ha_source}, sample_interval_hours=1.0
    )
    # Set processor as running (normally done by start())
    processor._running = True

    # Process a state change event
    event = MockFiredEvent(
        entity_id="sensor.temperature", old_state="20.5", new_state="21.0"
    )

    await ha_source._handle_state_change(event)

    # Manually call process_event since we're not running the full WebSocket loop
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.temperature",
            "old_state": {
                "state": "20.5",
                "attributes": {"friendly_name": "Test 20.5"},
            },
            "new_state": {
                "state": "21.0",
                "attributes": {"friendly_name": "Test 21.0"},
            },
        },
    )

    # Give a small delay to ensure async writes complete
    await asyncio.sleep(0.1)

    # Query recent events
    async with get_db_context() as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
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
        assert temp_event["event_data"]["old_state"]["state"] == "20.5"
        assert temp_event["event_data"]["new_state"]["state"] == "21.0"


@pytest.mark.asyncio
async def test_event_listener_matching(test_db_engine: AsyncEngine) -> None:
    """Test event matching against listener conditions."""
    # Add a test listener
    async with get_db_context() as db_ctx:
        from sqlalchemy import text

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

    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    await processor._refresh_listener_cache()

    # Test matching
    match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "26.0"}}
    no_match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "24.0"}}

    listeners = processor._listener_cache.get("home_assistant", [])
    assert len(listeners) == 1

    assert processor._check_match_conditions(
        match_event, listeners[0]["match_conditions"]
    )
    assert not processor._check_match_conditions(
        no_match_event, listeners[0]["match_conditions"]
    )


@pytest.mark.asyncio
async def test_websocket_connection_with_mock(test_db_engine: AsyncEngine) -> None:
    """Test WebSocket connection handling with mocked homeassistant_api."""
    with patch(
        "family_assistant.events.home_assistant_source.WebsocketClient"
    ) as MockWSClient:
        # Set up mock WebSocket client
        mock_ws_instance = MagicMock()
        mock_ws_instance.__enter__ = MagicMock(return_value=mock_ws_instance)
        mock_ws_instance.__exit__ = MagicMock(return_value=None)

        # Mock listen_events context manager
        mock_events = [
            MockFiredEvent("light.kitchen", "off", "on"),
            MockFiredEvent("sensor.temperature", "22", "23"),
        ]
        mock_listen_ctx = MagicMock()
        mock_listen_ctx.__enter__ = MagicMock(return_value=iter(mock_events))
        mock_listen_ctx.__exit__ = MagicMock(return_value=None)
        mock_ws_instance.listen_events.return_value = mock_listen_ctx

        MockWSClient.return_value = mock_ws_instance

        # Create HA source
        mock_client = MagicMock()
        mock_client.api_url = "http://localhost:8123/api"
        mock_client.token = "test_token"
        ha_source = HomeAssistantSource(client=mock_client)

        # Track processed events
        processed_events = []

        async def mock_process_event(source_id: str, event_data: Any) -> None:
            processed_events.append(event_data)

        # Create processor with mocked process_event
        processor = EventProcessor(sources={"ha": ha_source}, sample_interval_hours=1.0)
        processor.process_event = mock_process_event
        ha_source.processor = processor

        # Simulate running for a short time
        ha_source._running = True

        # Run in thread (will process the mocked events)
        await asyncio.to_thread(ha_source._connect_and_listen)

        # Verify WebSocket was created with correct URL
        MockWSClient.assert_called_once_with(
            api_url="ws://localhost:8123/api/websocket", token="test_token"
        )

        # Verify events were processed
        assert len(processed_events) == 2
        assert processed_events[0]["entity_id"] == "light.kitchen"
        assert processed_events[1]["entity_id"] == "sensor.temperature"


@pytest.mark.asyncio
async def test_test_event_listener_tool_matches_person_coming_home(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool correctly matches person coming home."""
    # Arrange
    async with get_db_context() as db_ctx:
        from sqlalchemy import text

        await db_ctx.execute_with_retry(text("DELETE FROM recent_events"))

        now = datetime.now(timezone.utc)
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
    async with get_db_context() as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        result = await event_listener_test_tool(
            exec_context,
            source_id=EventSourceType.home_assistant.value,
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
    test_db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool provides analysis when no events match."""
    # Arrange
    async with get_db_context() as db_ctx:
        from sqlalchemy import text

        await db_ctx.execute_with_retry(text("DELETE FROM recent_events"))

        now = datetime.now(timezone.utc)
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
    async with get_db_context() as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        result = await event_listener_test_tool(
            exec_context,
            source_id=EventSourceType.home_assistant.value,
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
    test_db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool returns error for empty match conditions."""
    # Arrange - no events needed for this test

    # Act
    async with get_db_context() as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        result = await event_listener_test_tool(
            exec_context,
            source_id=EventSourceType.home_assistant.value,
            match_conditions={},
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert "error" in data
    assert "condition" in data["message"].lower()


@pytest.mark.asyncio
async def test_cleanup_old_events(test_db_engine: AsyncEngine) -> None:
    """Test that old events are cleaned up correctly."""
    from datetime import timedelta

    from family_assistant.storage.events import cleanup_old_events

    # Arrange - create events with different ages
    async with get_db_context() as db_ctx:
        # Store events with different timestamps
        now = datetime.now(timezone.utc)

        # Use EventStorage to store events
        storage = EventStorage(sample_interval_hours=0.01)  # Short interval for testing

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
        from sqlalchemy import text

        await db_ctx.execute_with_retry(
            text(
                "UPDATE recent_events SET created_at = :old_time "
                "WHERE json_extract(event_data, '$.entity_id') = 'test.old'"
            ),
            {"old_time": now - timedelta(hours=72)},
        )

    # Act - run cleanup with 48 hour retention
    async with get_db_context() as db_ctx:
        deleted_count = await cleanup_old_events(db_ctx, retention_hours=48)

    # Assert - the cleanup function works correctly
    assert deleted_count == 1  # Exactly one old event was deleted


@pytest.mark.asyncio
async def test_end_to_end_event_listener_wakes_llm(test_db_engine: AsyncEngine) -> None:
    """Test end-to-end flow: event triggers listener which enqueues LLM callback task."""
    from sqlalchemy import text

    from family_assistant.storage.tasks import dequeue_task

    # Step 1: Create an event listener that watches for motion detection
    async with get_db_context() as db_ctx:
        await db_ctx.execute_with_retry(
            text("""INSERT INTO event_listeners 
                 (name, match_conditions, source_id, action_type, action_config, enabled, 
                  conversation_id, interface_type)
                 VALUES (:name, :conditions, :source_id, :action_type, :action_config, 
                         :enabled, :conversation_id, :interface_type)"""),
            {
                "name": "Motion Light Automation",
                "conditions": json.dumps({
                    "entity_id": "binary_sensor.hallway_motion",
                    "new_state.state": "on",
                }),
                "source_id": EventSourceType.home_assistant.value,
                "action_type": "wake_llm",
                "action_config": json.dumps({
                    "include_event_data": True,
                }),
                "enabled": True,
                "conversation_id": "test_chat_123",
                "interface_type": "telegram",
            },
        )

    # Step 2: Create event processor and refresh cache
    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    # Verify listener is in cache
    listeners = processor._listener_cache.get("home_assistant", [])
    assert len(listeners) == 1
    assert listeners[0]["name"] == "Motion Light Automation"

    # Step 3: Process a motion detection event
    motion_event = {
        "entity_id": "binary_sensor.hallway_motion",
        "old_state": {
            "state": "off",
            "attributes": {"friendly_name": "Hallway Motion Sensor"},
        },
        "new_state": {
            "state": "on",
            "attributes": {"friendly_name": "Hallway Motion Sensor"},
            "last_changed": datetime.now(timezone.utc).isoformat(),
        },
    }

    await processor.process_event("home_assistant", motion_event)

    # Step 4: Verify the event was stored
    async with get_db_context() as db_ctx:
        events_result = await db_ctx.fetch_all(
            text("SELECT * FROM recent_events WHERE source_id = 'home_assistant'")
        )
        assert len(events_result) >= 1

        # Find our motion event
        motion_event_stored = None
        for event in events_result:
            event_data = json.loads(event["event_data"])
            if event_data.get("entity_id") == "binary_sensor.hallway_motion":
                motion_event_stored = event
                break

        assert motion_event_stored is not None
        triggered_listeners = json.loads(
            motion_event_stored["triggered_listener_ids"] or "[]"
        )
        assert len(triggered_listeners) == 1

    # Step 5: Verify an LLM callback task was created
    async with get_db_context() as db_ctx:
        # Check tasks table for our callback
        tasks_result = await db_ctx.fetch_all(
            text(
                "SELECT * FROM tasks WHERE task_type = 'llm_callback' AND status = 'pending'"
            )
        )

        # Find the task created by our listener
        callback_task = None
        for task in tasks_result:
            if task["task_id"].startswith("event_listener_"):
                callback_task = task
                break

        assert callback_task is not None

        # Verify task payload
        payload = json.loads(callback_task["payload"])
        assert payload["interface_type"] == "telegram"
        assert payload["conversation_id"] == "test_chat_123"
        assert "callback_context" in payload

        callback_context = payload["callback_context"]
        assert (
            callback_context["trigger"]
            == "Event listener 'Motion Light Automation' matched"
        )
        assert callback_context["source"] == "home_assistant"
        assert "event_data" in callback_context
        assert (
            callback_context["event_data"]["entity_id"]
            == "binary_sensor.hallway_motion"
        )

    # Step 6: Verify the task can be dequeued by a worker
    async with get_db_context() as db_ctx:
        dequeued_task = await dequeue_task(
            db_ctx,
            worker_id="test_worker",
            task_types=["llm_callback"],
            current_time=datetime.now(timezone.utc),
        )

        assert dequeued_task is not None
        assert dequeued_task["task_type"] == "llm_callback"
        # The task was just dequeued and is ready for processing
        assert dequeued_task["task_id"].startswith("event_listener_")


@pytest.mark.asyncio
async def test_one_time_listener_disables_after_trigger(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that one-time listeners are disabled after they trigger."""
    from sqlalchemy import text

    # Create a one-time listener
    async with get_db_context() as db_ctx:
        result = await db_ctx.execute_with_retry(
            text("""INSERT INTO event_listeners 
                 (name, match_conditions, source_id, action_type, enabled, 
                  conversation_id, interface_type, one_time)
                 VALUES (:name, :conditions, :source_id, :action_type, :enabled, 
                         :conversation_id, :interface_type, :one_time)
                 RETURNING id"""),
            {
                "name": "One-time door alert",
                "conditions": json.dumps({
                    "entity_id": "binary_sensor.front_door",
                    "new_state.state": "open",
                }),
                "source_id": EventSourceType.home_assistant.value,
                "action_type": "wake_llm",
                "enabled": True,
                "conversation_id": "test_chat_456",
                "interface_type": "telegram",
                "one_time": True,
            },
        )
        listener_id = result.scalar_one()

    # Create processor and process matching event
    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    door_event = {
        "entity_id": "binary_sensor.front_door",
        "old_state": {"state": "closed"},
        "new_state": {"state": "open"},
    }

    await processor.process_event("home_assistant", door_event)

    # Verify listener is now disabled
    async with get_db_context() as db_ctx:
        result = await db_ctx.fetch_one(
            text("SELECT enabled FROM event_listeners WHERE id = :id"),
            {"id": listener_id},
        )
        assert result is not None
        assert result["enabled"] == 0  # SQLite stores False as 0
