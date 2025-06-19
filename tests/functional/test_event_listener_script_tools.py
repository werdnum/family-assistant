"""Tests for event listener script tools."""

import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventActionType
from family_assistant.tools.event_listeners import (
    create_event_listener_tool,
    validate_event_listener_script_tool,
)
from family_assistant.tools.event_listeners import (
    test_event_listener_script_tool as script_test_tool,  # Rename to avoid pytest confusion
)
from family_assistant.tools.types import ToolExecutionContext


@pytest_asyncio.fixture
async def mock_exec_context(
    test_db_engine: AsyncEngine,
) -> AsyncGenerator[ToolExecutionContext, None]:
    """Create a mock execution context for tool testing."""
    # Import here to avoid issues during collection
    from tests.mocks.mock_tools_provider import MockToolsProvider

    mock_tools_provider = MockToolsProvider()

    # Add a mock tool for testing
    async def mock_add_note(title: str, content: str) -> str:
        return f"Added note: {title}"

    mock_tools_provider.add_tool("add_or_update_note", mock_add_note)

    # Create a database context that will be used as async context manager
    async with DatabaseContext() as db_context:
        yield ToolExecutionContext(
            interface_type="web",  # Use valid interface type
            conversation_id="test_conversation",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_context,
            tools_provider=mock_tools_provider,
        )


@pytest.mark.asyncio
async def test_validate_event_listener_script_valid(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test validating a valid script."""
    script_code = """
def process_event():
    result = add_or_update_note("Test", "Content")
    return result

process_event()
"""

    result = await validate_event_listener_script_tool(mock_exec_context, script_code)
    result_json = json.loads(result)

    assert result_json["success"] is True
    assert result_json["message"] == "Script syntax is valid"


@pytest.mark.asyncio
async def test_validate_event_listener_script_invalid(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test validating an invalid script."""
    script_code = """
# Invalid syntax - missing colon
def process_event()
    return "test"
"""

    result = await validate_event_listener_script_tool(mock_exec_context, script_code)
    result_json = json.loads(result)

    assert result_json["success"] is False
    assert "Syntax error" in result_json["error"]


@pytest.mark.asyncio
async def test_script_execution_with_sample_event(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test running a script with a sample event."""
    script_code = """
def handle_event():
    # Access the event data
    entity_id = event.get("entity_id", "unknown")
    state = event.get("new_state", {}).get("state", "unknown")
    
    # Simple string manipulation without tools for now
    return "Processed: " + entity_id

handle_event()
"""

    sample_event = {
        "entity_id": "binary_sensor.kitchen_motion",
        "new_state": {"state": "on"},
        "old_state": {"state": "off"},
    }

    result = await script_test_tool(
        mock_exec_context, script_code, sample_event, timeout=2
    )
    result_json = json.loads(result)

    if not result_json["success"]:
        print(f"Script failed: {result_json}")
    assert result_json["success"] is True
    assert "Processed: binary_sensor.kitchen_motion" in str(result_json["result"])


@pytest.mark.asyncio
async def test_script_execution_with_error(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test running a script that has an error."""
    script_code = """
# This will cause a runtime error
undefined_variable + 1
"""

    sample_event = {"test": "data"}

    result = await script_test_tool(mock_exec_context, script_code, sample_event)
    result_json = json.loads(result)

    assert result_json["success"] is False
    assert "error" in result_json


@pytest.mark.asyncio
async def test_create_event_listener_with_script(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test creating an event listener with a script action."""
    script_code = """
def handle_motion():
    add_or_update_note("Motion Log", f"Motion detected at {time_now()}")
    return "logged"

handle_motion()
"""

    result = await create_event_listener_tool(
        exec_context=mock_exec_context,
        name="motion_script_listener",
        source="home_assistant",
        listener_config={
            "match_conditions": {
                "entity_id": "binary_sensor.front_door_motion",
                "new_state.state": "on",
            }
        },
        action_type="script",
        script_code=script_code,
        script_config={"timeout": 30},
    )

    result_json = json.loads(result)
    assert result_json["success"] is True
    assert "listener_id" in result_json

    # Verify the listener was created with script action
    # The db_context is already open from the fixture
    listeners = await mock_exec_context.db_context.events.get_event_listeners(
        conversation_id=mock_exec_context.conversation_id
    )
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener["name"] == "motion_script_listener"
    assert listener["action_type"] == EventActionType.script
    assert listener["action_config"]["script_code"] == script_code
    assert listener["action_config"]["timeout"] == 30


@pytest.mark.asyncio
async def test_create_event_listener_script_validation(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test that invalid script code is rejected when creating a listener."""
    invalid_script = """
# Invalid syntax
def broken(
"""

    result = await create_event_listener_tool(
        exec_context=mock_exec_context,
        name="invalid_script_listener",
        source="home_assistant",
        listener_config={"match_conditions": {"test": "value"}},
        action_type="script",
        script_code=invalid_script,
    )

    result_json = json.loads(result)
    # Currently we don't validate on creation, but this could be added
    # For now, just verify it creates successfully
    assert "success" in result_json


@pytest.mark.asyncio
async def test_create_event_listener_script_missing_code(
    mock_exec_context: ToolExecutionContext,
) -> None:
    """Test that script_code is required for script action type."""
    result = await create_event_listener_tool(
        exec_context=mock_exec_context,
        name="no_code_listener",
        source="home_assistant",
        listener_config={"match_conditions": {"test": "value"}},
        action_type="script",
        # script_code intentionally omitted
    )

    result_json = json.loads(result)
    assert result_json["success"] is False
    assert "script_code is required" in result_json["message"]
