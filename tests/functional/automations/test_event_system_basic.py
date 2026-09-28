"""Basic event handling tests for the event listener system."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.home_assistant_source import HomeAssistantSource
from family_assistant.events.processor import EventProcessor
from family_assistant.events.storage import EventStorage
from family_assistant.storage import Database
from family_assistant.storage.events import (
    EventSourceType,
    cleanup_old_events,
    event_listeners_table,
    recent_events_table,
)
from family_assistant.tools.events import query_recent_events_tool
from family_assistant.tools.events import (
    test_event_listener_tool as event_listener_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_condition


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


class FakeWebsocketClient:
    """Stands in for homeassistant_api's WebsocketClient, firing a fixed set of events."""

    def __init__(self, api_url: str, token: str, events: list[MockFiredEvent]) -> None:
        self.api_url = api_url
        self.token = token
        self.events = events

    def __enter__(self) -> "FakeWebsocketClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @contextmanager
    def listen_events(self) -> Iterator[Iterator[MockFiredEvent]]:
        yield iter(self.events)


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
        sample_interval_hours=1.0, get_db_context_func=lambda: Database(db_engine)
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
    db_ctx = Database(db_engine)
    result = await db_ctx.fetch_all(text("SELECT COUNT(*) as count FROM recent_events"))
    assert result[0]["count"] == 2  # Only 2 events should be stored


@pytest.mark.asyncio
async def test_home_assistant_event_processing(db_engine: AsyncEngine) -> None:
    """A state change fired over the HA websocket is stored and queryable."""
    mock_client = MagicMock()
    mock_client.api_url = "http://localhost:8123/api"
    mock_client.token = "test_token"
    mock_client.verify_ssl = True

    processor = EventProcessor(
        sources={"ha_test": HomeAssistantSource(client=mock_client)},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )
    fired_event = MockFiredEvent(
        entity_id="sensor.temperature", old_state="20.5", new_state="21.0"
    )

    async def event_stored() -> bool:
        rows = await Database(db_engine).fetch_all(
            select(recent_events_table.c.event_id)
        )
        return bool(rows)

    with patch(
        "family_assistant.events.home_assistant_source.WebsocketClient",
        partial(FakeWebsocketClient, events=[fired_event]),
    ):
        await processor.start()
        try:
            await wait_for_condition(
                event_stored,
                timeout=30.0,
                description="HA state change stored in recent_events",
            )
        finally:
            await processor.stop()

    db_ctx = Database(db_engine)
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
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )

    result = await query_recent_events_tool(
        exec_context=exec_context, source_id="home_assistant", hours=1
    )

    events = json.loads(result)["events"]
    assert len(events) == 1
    temp_event = events[0]
    assert temp_event["source_id"] == "home_assistant"
    assert temp_event["event_data"]["entity_id"] == "sensor.temperature"
    assert temp_event["event_data"]["event_type"] == "state_changed"
    assert temp_event["event_data"]["old_state"]["state"] == "20.5"
    assert temp_event["event_data"]["new_state"]["state"] == "21.0"


@pytest.mark.asyncio
async def test_event_listener_matching(db_engine: AsyncEngine) -> None:
    """Only an event satisfying the listener's match conditions fires its action."""
    db_ctx = Database(db_engine)
    await db_ctx.execute(
        text("""INSERT INTO event_listeners
                 (name, match_conditions, source_id, action_type, enabled,
                  conversation_id)
                 VALUES (:name, :conditions, :source_id, :action_type, :enabled,
                         :conversation_id)"""),
        {
            "name": "Temperature Monitor",
            "conditions": json.dumps({
                "entity_id": "sensor.temperature",
                "new_state.state": "26.0",
            }),
            "source_id": EventSourceType.home_assistant.value,
            "action_type": "wake_llm",
            "enabled": True,
            "conversation_id": "test_conversation",
        },
    )
    listener_row = await db_ctx.fetch_one(select(event_listeners_table.c.id))
    assert listener_row is not None

    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )
    await processor.start()

    match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "26.0"}}
    no_match_event = {"entity_id": "sensor.temperature", "new_state": {"state": "24.0"}}

    await processor.process_event("home_assistant", no_match_event)
    await processor.process_event("home_assistant", match_event)
    await processor.stop()

    callback_tasks = await Database(db_engine).tasks.get_all(task_type="llm_callback")
    assert len(callback_tasks) == 1
    payload = callback_tasks[0]["payload"]
    assert payload is not None
    assert payload["callback_context"]["listener_id"] == listener_row["id"]
    assert payload["callback_context"]["event_data"] == match_event


@pytest.mark.asyncio
async def test_test_event_listener_tool_matches_person_coming_home(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool correctly matches person coming home."""
    # Arrange
    db_ctx = Database(db_engine)
    await db_ctx.execute(text("DELETE FROM recent_events"))

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
        await db_ctx.execute(
            text("""INSERT INTO recent_events
                       (event_id, source_id, event_data, timestamp)
                       VALUES (:event_id, :source_id, :event_data, :timestamp)"""),
            event,
        )

    # Act
    db_ctx = Database(db_engine)
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
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
    assert data["total_tested"] == 3
    assert len(data["matched_events"]) == 1
    assert data["matched_events"][0]["event_data"]["entity_id"] == "person.alex"
    assert data["matched_events"][0]["event_data"]["new_state"]["state"] == "Home"


@pytest.mark.asyncio
async def test_test_event_listener_tool_no_match_wrong_state(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool provides analysis when no events match."""
    # Arrange
    db_ctx = Database(db_engine)
    await db_ctx.execute(text("DELETE FROM recent_events"))

    now = datetime.now(UTC)
    await db_ctx.execute(
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
    db_ctx = Database(db_engine)
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
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
    assert data["total_tested"] == 1
    assert "Field 'new_state.state' exists but has value: 'Home'" in data["analysis"]


@pytest.mark.asyncio
async def test_test_event_listener_tool_empty_conditions_error(
    db_engine: AsyncEngine,
) -> None:
    """Test that test_event_listener tool returns error for empty match conditions."""
    # Arrange - no events needed for this test

    # Act
    db_ctx = Database(db_engine)
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
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
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
    db_ctx = Database(db_engine)
    # Store events with different timestamps
    now = datetime.now(UTC)

    # Use EventStorage to store events
    storage = EventStorage(
        sample_interval_hours=0.01,  # Short interval for testing
        get_db_context_func=lambda: Database(db_engine),
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
        .where(recent_events_table.c.event_data["entity_id"].as_string() == "test.old")
        .values(created_at=now - timedelta(hours=72))
    )

    await db_ctx.execute(stmt)

    # Act - run cleanup with 48 hour retention
    db_ctx = Database(db_engine)
    deleted_count = await cleanup_old_events(db_ctx, retention_hours=48)

    # Assert
    assert deleted_count == 1
    remaining = await db_ctx.fetch_all(
        select(
            recent_events_table.c.event_data["entity_id"].as_string().label("entity_id")
        )
    )
    assert [row["entity_id"] for row in remaining] == ["test.recent"]
