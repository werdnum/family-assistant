"""Advanced event system features: type matching, end-to-end flows, one-time listeners."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import get_db_context
from family_assistant.storage.events import EventSourceType
from family_assistant.task_worker import TaskWorker, handle_llm_callback
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from family_assistant.tools.events import (
    test_event_listener_tool as event_listener_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


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
async def test_event_type_matching(db_engine: AsyncEngine) -> None:
    """Test that event type matching works correctly."""
    # Arrange - store different event types
    async with get_db_context(db_engine) as db_ctx:
        await db_ctx.execute_with_retry(text("DELETE FROM recent_events"))

        now = datetime.now(UTC)

        # Store a state_changed event
        await db_ctx.execute_with_retry(
            text("""INSERT INTO recent_events
                   (event_id, source_id, event_data, timestamp)
                   VALUES (:event_id, :source_id, :event_data, :timestamp)"""),
            {
                "event_id": "test_state_1",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "event_type": "state_changed",
                    "entity_id": "light.living_room",
                    "old_state": {"state": "off"},
                    "new_state": {"state": "on"},
                }),
                "timestamp": now,
            },
        )

        # Store a call_service event
        await db_ctx.execute_with_retry(
            text("""INSERT INTO recent_events
                   (event_id, source_id, event_data, timestamp)
                   VALUES (:event_id, :source_id, :event_data, :timestamp)"""),
            {
                "event_id": "test_service_1",
                "source_id": EventSourceType.home_assistant.value,
                "event_data": json.dumps({
                    "event_type": "call_service",
                    "domain": "light",
                    "service": "turn_on",
                    "service_data": {"entity_id": "light.living_room"},
                }),
                "timestamp": now,
            },
        )

    # Act - test matching state_changed events only
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
            camera_backend=None,
        )

        result = await event_listener_test_tool(
            exec_context,
            source=EventSourceType.home_assistant.value,
            match_conditions={
                "event_type": "state_changed",
                "entity_id": "light.living_room",
            },
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert data["matched_count"] == 1
    assert data["total_tested"] == 2
    assert len(data["matched_events"]) == 1
    assert data["matched_events"][0]["event_data"]["event_type"] == "state_changed"

    # Act - test matching call_service events only
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
            camera_backend=None,
        )

        result = await event_listener_test_tool(
            exec_context,
            source=EventSourceType.home_assistant.value,
            match_conditions={
                "event_type": "call_service",
                "domain": "light",
            },
            hours=1,
        )

    # Assert
    data = json.loads(result)
    assert data["matched_count"] == 1
    assert data["total_tested"] == 2
    assert len(data["matched_events"]) == 1
    assert data["matched_events"][0]["event_data"]["event_type"] == "call_service"


@pytest.mark.asyncio
async def test_end_to_end_event_listener_wakes_llm(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test end-to-end flow: event triggers listener which enqueues LLM callback task."""

    # Step 1: Create an event listener that watches for motion detection
    async with get_db_context(db_engine) as db_ctx:
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
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
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
            "last_changed": datetime.now(UTC).isoformat(),
        },
    }

    await processor.process_event("home_assistant", motion_event)

    # Step 4: Verify the event was stored
    async with get_db_context(db_engine) as db_ctx:
        events_result = await db_ctx.fetch_all(
            text("SELECT * FROM recent_events WHERE source_id = 'home_assistant'")
        )
        assert len(events_result) >= 1

        # Find our motion event
        motion_event_stored = None
        for event in events_result:
            # Handle both string (SQLite) and dict (PostgreSQL) formats
            event_data = safe_json_loads(event["event_data"])
            if event_data.get("entity_id") == "binary_sensor.hallway_motion":
                motion_event_stored = event
                break

        assert motion_event_stored is not None
        # Handle both string (SQLite) and dict/list (PostgreSQL) formats
        triggered_listeners = motion_event_stored["triggered_listener_ids"]
        if triggered_listeners is None:
            triggered_listeners = []
        else:
            triggered_listeners = safe_json_loads(triggered_listeners)
        assert len(triggered_listeners) == 1

    # Step 5: Verify an LLM callback task was created
    async with get_db_context(db_engine) as db_ctx:
        # Check tasks table for our callback
        tasks_result = await db_ctx.fetch_all(
            text(
                "SELECT * FROM tasks WHERE task_type = 'llm_callback' AND status = 'pending'"
            )
        )

        # There should be at least one llm_callback task
        assert len(tasks_result) > 0
        callback_task = tasks_result[0]  # Get the first one

        # Verify task payload
        # Handle both string (SQLite) and dict (PostgreSQL) formats
        payload = safe_json_loads(callback_task["payload"])
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

    # Step 6: Start a task worker that will process the callback task

    # Setup mock LLM that will handle the callback
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def callback_matcher(kwargs: dict[str, Any]) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        if last_message.role == "user":
            content = last_message.content or ""
            return (
                "System Callback Trigger:" in content
                and "Motion Light Automation" in content
            )
        return False

    callback_response = MockLLMOutput(
        content="Motion detected in hallway. I would turn on the lights now.",
        tool_calls=None,
    )

    llm_client = RuleBasedMockLLMClient(
        rules=[(callback_matcher, callback_response)],
        default_response=MockLLMOutput(content="Default response"),
    )

    # Setup tools provider
    local_provider = LocalToolsProvider(definitions=[], implementations={})
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    # Setup processing service
    test_service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt"},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="test_event_listener_profile",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config,
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        clock=None,
    )

    # Setup mock chat interface
    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_message_id"

    # Use task_worker_manager fixture to create and start task worker
    task_worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        mock_chat_interface,
    )
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    # Give worker time to start
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Allow worker startup
    await asyncio.sleep(0.1)

    # Signal that there's a new task to process
    new_task_event.set()

    # Give worker time to process the task (this will fail if timestamp is wrong)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Allow task processing
    await asyncio.sleep(0.5)

    # Verify the message was sent (proves the task was processed successfully)
    mock_chat_interface.send_message.assert_called_once()
    call_kwargs = mock_chat_interface.send_message.call_args[1]
    assert call_kwargs["conversation_id"] == "test_chat_123"
    assert "Motion detected" in call_kwargs["text"]

    # Cleanup is handled by the task_worker_manager fixture


@pytest.mark.asyncio
async def test_one_time_listener_disables_after_trigger(
    db_engine: AsyncEngine,
) -> None:
    """Test that one-time listeners are disabled after they trigger."""

    # Create a one-time listener
    async with get_db_context(db_engine) as db_ctx:
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
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    processor._running = True
    await processor._refresh_listener_cache()

    door_event = {
        "entity_id": "binary_sensor.front_door",
        "old_state": {"state": "closed"},
        "new_state": {"state": "open"},
    }

    await processor.process_event("home_assistant", door_event)

    # Verify listener is now disabled
    async with get_db_context(db_engine) as db_ctx:
        result = await db_ctx.fetch_one(
            text("SELECT enabled FROM event_listeners WHERE id = :id"),
            {"id": listener_id},
        )
        assert result is not None
        assert result["enabled"] == 0  # SQLite stores False as 0
