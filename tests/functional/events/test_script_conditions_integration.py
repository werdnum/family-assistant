"""
Integration tests for event listener script conditions.
"""

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.event_listeners import create_event_listener_tool
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_zone_entry_detection_with_script(db_engine: AsyncEngine) -> None:
    """Test detecting zone entry using condition script."""
    # Create database context from engine
    async with DatabaseContext(engine=db_engine) as db_context:
        # Create mock event source
        mock_sources = {}

        # Create processor with db_context
        processor = EventProcessor(sources=mock_sources, db_context=db_context)
        # Create execution context
        exec_context = ToolExecutionContext(
            conversation_id="test_conv",
            interface_type="telegram",
            tools_provider=None,
            db_context=db_context,
            user_name="Test User",
            turn_id=None,
            chat_interface=None,
            event_sources=mock_sources,
        )

        # Create listener with zone entry script
        zone_entry_script = "event.get('old_state', {}).get('state') != 'home' and event.get('new_state', {}).get('state') == 'home'"

        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="zone_entry_detector",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "person.test"}},
            condition_script=zone_entry_script,
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is True
        listener_id = result_data["listener_id"]

        # Refresh processor cache to pick up new listener
        # TODO: This is a test-specific workaround that directly manipulates the
        # processor's internal cache. Consider adding a test-friendly method to
        # EventProcessor or using dependency injection for the cache.
        result = await db_context.fetch_all(
            text("SELECT * FROM event_listeners WHERE enabled = TRUE")
        )

        new_cache = {}
        for row in result:
            listener_dict = dict(row)
            # Parse JSON fields if they're strings
            match_conditions = listener_dict.get("match_conditions") or {}
            if isinstance(match_conditions, str):
                listener_dict["match_conditions"] = json.loads(match_conditions)
            else:
                listener_dict["match_conditions"] = match_conditions

            action_config = listener_dict.get("action_config") or {}
            if isinstance(action_config, str):
                listener_dict["action_config"] = json.loads(action_config)
            else:
                listener_dict["action_config"] = action_config

            source_id = listener_dict["source_id"]
            if source_id not in new_cache:
                new_cache[source_id] = []
            new_cache[source_id].append(listener_dict)

        processor._listener_cache = new_cache

        # Test that zone entry triggers the listener
        zone_entry_event = {
            "entity_id": "person.test",
            "old_state": {"state": "not_home"},
            "new_state": {"state": "home"},
        }

        # Track which listeners were triggered
        triggered_listeners = []

        async def mock_execute(
            db_ctx: DatabaseContext,
            listener: dict[str, Any],
            event_data: dict[str, Any],
        ) -> None:
            triggered_listeners.append(listener["id"])

        processor._execute_action_in_context = mock_execute

        # Ensure processor is running
        processor._running = True

        # Check that the listener has the condition_script in cache
        if processor._listener_cache.get("home_assistant"):
            for listener in processor._listener_cache["home_assistant"]:
                if listener["id"] == listener_id:
                    assert "condition_script" in listener
                    assert listener["condition_script"] == zone_entry_script

        # Process the event
        await processor.process_event("home_assistant", zone_entry_event)

        # Verify the listener was triggered
        assert listener_id in triggered_listeners

        # Test that attribute-only change doesn't trigger
        triggered_listeners.clear()
        attribute_change_event = {
            "entity_id": "person.test",
            "old_state": {"state": "home", "attributes": {"battery": 90}},
            "new_state": {"state": "home", "attributes": {"battery": 85}},
        }

        await processor.process_event("home_assistant", attribute_change_event)

        # Verify the listener was NOT triggered
        assert listener_id not in triggered_listeners


@pytest.mark.asyncio
async def test_temperature_threshold_with_script(db_engine: AsyncEngine) -> None:
    """Test temperature threshold detection using condition script."""
    # Create database context from engine
    async with DatabaseContext(engine=db_engine) as db_context:
        # Create mock event source
        mock_sources = {}

        # Create processor with db_context
        processor = EventProcessor(sources=mock_sources, db_context=db_context)
        # Create execution context
        exec_context = ToolExecutionContext(
            conversation_id="test_conv",
            interface_type="telegram",
            tools_provider=None,
            db_context=db_context,
            user_name="Test User",
            turn_id=None,
            chat_interface=None,
            event_sources=mock_sources,
        )

        # Create listener with temperature threshold script
        # For this test, we'll use a script that checks if temperature increased
        temp_script = "event.get('new_state', {}).get('state') == '26' and event.get('old_state', {}).get('state') == '20'"

        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="temp_spike_detector",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "sensor.temperature"}},
            condition_script=temp_script,
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is True
        listener_id = result_data["listener_id"]

        # Force processor to refresh its cache
        processor._last_cache_refresh = (
            0  # Set to 0 to force refresh on next process_event
        )
        processor._running = True

        # Track triggered listeners
        triggered_listeners = []

        async def mock_execute(
            db_ctx: DatabaseContext,
            listener: dict[str, Any],
            event_data: dict[str, Any],
        ) -> None:
            triggered_listeners.append(listener["id"])

        processor._execute_action_in_context = mock_execute

        # Test temperature spike
        temp_spike_event = {
            "entity_id": "sensor.temperature",
            "old_state": {"state": "20"},
            "new_state": {"state": "26"},
        }

        await processor.process_event("home_assistant", temp_spike_event)
        assert listener_id in triggered_listeners

        # Test small temperature change
        triggered_listeners.clear()
        small_change_event = {
            "entity_id": "sensor.temperature",
            "old_state": {"state": "20"},
            "new_state": {"state": "23"},
        }

        await processor.process_event("home_assistant", small_change_event)
        assert listener_id not in triggered_listeners


@pytest.mark.asyncio
async def test_script_validation_errors(db_engine: AsyncEngine) -> None:
    """Test that invalid scripts are rejected."""
    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = ToolExecutionContext(
            conversation_id="test_conv",
            interface_type="telegram",
            tools_provider=None,
            db_context=db_context,
            user_name="Test User",
            turn_id=None,
            chat_interface=None,
            event_sources={},
        )

        # Test syntax error
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="bad_syntax",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "test.entity"}},
            condition_script="True (",  # Invalid syntax
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is False
        assert "Syntax error" in result_data["message"]

        # Test non-boolean return
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="bad_return",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "test.entity"}},
            condition_script="'not a boolean'",
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is False
        assert "must return boolean" in result_data["message"]

        # Test script too large
        large_script = "# " + "x" * 10240 + "\nTrue"
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="too_large",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "test.entity"}},
            condition_script=large_script,
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is False
        assert "too large" in result_data["message"]


@pytest.mark.asyncio
async def test_script_with_no_match_conditions(db_engine: AsyncEngine) -> None:
    """Test that condition_script can be used without match_conditions."""
    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = ToolExecutionContext(
            conversation_id="test_conv",
            interface_type="telegram",
            tools_provider=None,
            db_context=db_context,
            user_name="Test User",
            turn_id=None,
            chat_interface=None,
            event_sources={},
        )

        # Create listener with only condition_script
        script = "event.get('entity_id', '').startswith('sensor.') and event.get('new_state', {}).get('state') == 'on'"

        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="sensor_on_detector",
            source="home_assistant",
            listener_config={},  # No match_conditions
            condition_script=script,
            one_time=False,
        )

        result_data = json.loads(result)
        assert result_data["success"] is True

        # Verify the listener was created
        listeners = await db_context.events.get_event_listeners("test_conv")
        assert len(listeners) == 1
        assert listeners[0]["name"] == "sensor_on_detector"
        assert listeners[0]["condition_script"] == script
        assert listeners[0]["match_conditions"] == {}
