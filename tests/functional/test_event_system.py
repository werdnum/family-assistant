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
        EventSourceType.HOME_ASSISTANT,
        {"entity_id": "light.kitchen", "state": "on"},
        None,
    )

    # Second event for same entity within the hour should not be stored
    await storage.store_event(
        EventSourceType.HOME_ASSISTANT,
        {"entity_id": "light.kitchen", "state": "off"},
        None,
    )

    # Event for different entity should be stored
    await storage.store_event(
        EventSourceType.HOME_ASSISTANT,
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

        assert "sensor.temperature" in result
        assert "20.5 → 21.0" in result


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
                "source_id": EventSourceType.HOME_ASSISTANT.value,
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
