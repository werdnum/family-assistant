"""
Functional tests for event listener CRUD tools.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.event_listeners import (
    create_event_listener_tool,
    delete_event_listener_tool,
    list_event_listeners_tool,
    toggle_event_listener_tool,
)
from family_assistant.tools.types import ToolExecutionContext


@pytest.mark.asyncio
async def test_create_event_listener_basic(test_db_engine: AsyncEngine) -> None:
    """Test creating a basic event listener."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Motion Detector",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "sensor.hallway_motion",
                    "new_state.state": "on",
                }
            },
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is True
    assert "listener_id" in data
    assert data["listener_id"] > 0
    assert "Motion Detector" in data["message"]


@pytest.mark.asyncio
async def test_create_event_listener_with_action_config(
    test_db_engine: AsyncEngine,
) -> None:
    """Test creating an event listener with action configuration."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Temperature Alert",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "sensor.server_room_temp",
                    "new_state.state": "high",
                },
                "action_config": {
                    "include_event_data": True,
                    "prompt": "Alert! Server room temperature is high!",
                },
            },
            one_time=True,
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is True
    assert "listener_id" in data


@pytest.mark.asyncio
async def test_create_event_listener_duplicate_name_error(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that creating a listener with duplicate name fails."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Create first listener
        await create_event_listener_tool(
            exec_context=exec_context,
            name="Duplicate Test",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "light.test"}},
        )

        # Act - try to create with same name
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Duplicate Test",
            source="home_assistant",
            listener_config={"match_conditions": {"entity_id": "light.other"}},
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is False
    assert "already exists" in data["message"]


@pytest.mark.asyncio
async def test_create_event_listener_invalid_source(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that creating a listener with invalid source fails."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Invalid Source",
            source="invalid_source",
            listener_config={"match_conditions": {"test": "value"}},
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is False
    assert "Invalid source" in data["message"]


@pytest.mark.asyncio
async def test_list_event_listeners_empty(test_db_engine: AsyncEngine) -> None:
    """Test listing listeners when none exist."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="999999",  # New conversation with no listeners
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await list_event_listeners_tool(exec_context=exec_context)

    # Assert
    data = json.loads(result)
    assert data["success"] is True
    assert data["count"] == 0
    assert data["listeners"] == []


@pytest.mark.asyncio
async def test_list_event_listeners_with_filters(test_db_engine: AsyncEngine) -> None:
    """Test listing listeners with source and enabled filters."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="filter_test",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Create several listeners
        await create_event_listener_tool(
            exec_context=exec_context,
            name="HA Enabled",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "1"}},
        )

        await create_event_listener_tool(
            exec_context=exec_context,
            name="HA Disabled",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "2"}},
        )

        await create_event_listener_tool(
            exec_context=exec_context,
            name="Indexing Enabled",
            source="indexing",
            listener_config={"match_conditions": {"test": "3"}},
        )

        # Disable one listener
        result = await list_event_listeners_tool(exec_context=exec_context)
        data = json.loads(result)
        ha_disabled_id = next(
            listener["id"]
            for listener in data["listeners"]
            if listener["name"] == "HA Disabled"
        )
        await toggle_event_listener_tool(
            exec_context=exec_context, listener_id=ha_disabled_id, enabled=False
        )

        # Act - filter by source
        result = await list_event_listeners_tool(
            exec_context=exec_context, source="home_assistant"
        )

        # Assert
        data = json.loads(result)
        assert data["success"] is True
        assert data["count"] == 2
        assert all(
            listener["source"] == "home_assistant" for listener in data["listeners"]
        )

        # Act - filter by enabled
        result = await list_event_listeners_tool(
            exec_context=exec_context, enabled=True
        )
        data = json.loads(result)
        assert data["count"] == 2
        assert all(listener["enabled"] is True for listener in data["listeners"])

        # Act - filter by both
        result = await list_event_listeners_tool(
            exec_context=exec_context, source="home_assistant", enabled=True
        )
        data = json.loads(result)
        assert data["count"] == 1
        assert data["listeners"][0]["name"] == "HA Enabled"


@pytest.mark.asyncio
async def test_toggle_event_listener(test_db_engine: AsyncEngine) -> None:
    """Test toggling a listener's enabled status."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="toggle_test",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Create a listener
        create_result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Toggle Test",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "value"}},
        )
        listener_id = json.loads(create_result)["listener_id"]

        # Act - disable it
        result = await toggle_event_listener_tool(
            exec_context=exec_context, listener_id=listener_id, enabled=False
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is True
    assert "disabled" in data["message"]

    # Verify it's disabled
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        list_result = await list_event_listeners_tool(exec_context=exec_context)
        list_data = json.loads(list_result)
        assert list_data["listeners"][0]["enabled"] is False

        # Act - enable it again
        result = await toggle_event_listener_tool(
            exec_context=exec_context, listener_id=listener_id, enabled=True
        )
        data = json.loads(result)
        assert data["success"] is True
        assert "enabled" in data["message"]


@pytest.mark.asyncio
async def test_toggle_event_listener_not_found(test_db_engine: AsyncEngine) -> None:
    """Test toggling a non-existent listener."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await toggle_event_listener_tool(
            exec_context=exec_context, listener_id=99999, enabled=True
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is False
    assert "not found" in data["message"]


@pytest.mark.asyncio
async def test_delete_event_listener(test_db_engine: AsyncEngine) -> None:
    """Test deleting an event listener."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="delete_test",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Create a listener
        create_result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Delete Test",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "value"}},
        )
        listener_id = json.loads(create_result)["listener_id"]

        # Act
        result = await delete_event_listener_tool(
            exec_context=exec_context, listener_id=listener_id
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is True
    assert "Deleted listener 'Delete Test'" in data["message"]

    # Verify it's gone
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        list_result = await list_event_listeners_tool(exec_context=exec_context)
        list_data = json.loads(list_result)
        assert list_data["count"] == 0


@pytest.mark.asyncio
async def test_delete_event_listener_not_found(test_db_engine: AsyncEngine) -> None:
    """Test deleting a non-existent listener."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="123456",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Act
        result = await delete_event_listener_tool(
            exec_context=exec_context, listener_id=99999
        )

    # Assert
    data = json.loads(result)
    assert data["success"] is False
    assert "not found" in data["message"]


@pytest.mark.asyncio
async def test_conversation_isolation(test_db_engine: AsyncEngine) -> None:
    """Test that listeners are isolated by conversation."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        # Create listeners in conversation 1
        exec_context1 = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="conv1",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        result1 = await create_event_listener_tool(
            exec_context=exec_context1,
            name="Conv1 Listener",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "1"}},
        )
        listener1_id = json.loads(result1)["listener_id"]

        # Create listeners in conversation 2
        exec_context2 = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="conv2",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        await create_event_listener_tool(
            exec_context=exec_context2,
            name="Conv2 Listener",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "2"}},
        )

    # Act - list from conversation 1
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context1.db_context = db_ctx
        list_result1 = await list_event_listeners_tool(exec_context=exec_context1)
        list_data1 = json.loads(list_result1)

        # Assert - should only see conversation 1's listener
        assert list_data1["count"] == 1
        assert list_data1["listeners"][0]["name"] == "Conv1 Listener"

    # Act - try to delete conversation 1's listener from conversation 2
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context2.db_context = db_ctx
        delete_result = await delete_event_listener_tool(
            exec_context=exec_context2, listener_id=listener1_id
        )
        delete_data = json.loads(delete_result)

        # Assert - should fail
        assert delete_data["success"] is False
        assert "not found" in delete_data["message"]

    # Verify conversation 1's listener still exists
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context1.db_context = db_ctx
        list_result1_again = await list_event_listeners_tool(exec_context=exec_context1)
        list_data1_again = json.loads(list_result1_again)
        assert list_data1_again["count"] == 1


@pytest.mark.asyncio
async def test_listener_execution_tracking(test_db_engine: AsyncEngine) -> None:
    """Test that listener execution info is returned in list."""
    # Arrange
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        from sqlalchemy import text

        exec_context = ToolExecutionContext(
            interface_type="telegram",
            conversation_id="exec_test",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
        )

        # Create a listener
        create_result = await create_event_listener_tool(
            exec_context=exec_context,
            name="Execution Test",
            source="home_assistant",
            listener_config={"match_conditions": {"test": "value"}},
        )
        listener_id = json.loads(create_result)["listener_id"]

        # Simulate executions by updating the database
        now = datetime.now(timezone.utc)
        await db_ctx.execute_with_retry(
            text("""UPDATE event_listeners 
                    SET daily_executions = 3, 
                        last_execution_at = :now
                    WHERE id = :id"""),
            {"id": listener_id, "now": now},
        )

    # Act
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        list_result = await list_event_listeners_tool(exec_context=exec_context)

        # Assert
        list_data = json.loads(list_result)
        listener = list_data["listeners"][0]
        assert listener["daily_executions"] == 3
        assert listener["last_execution_at"] is not None
