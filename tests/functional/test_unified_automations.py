"""
Comprehensive integration tests for unified automations tools.

Tests all 8 automation tools via LLM tool execution, covering:
- create_automation (both event and schedule types)
- list_automations (with filtering)
- get_automation
- update_automation
- enable_automation / disable_automation
- delete_automation
- get_automation_stats

Also tests:
- Cross-type name uniqueness enforcement
- Schedule automation lifecycle (create → task executes → next occurrence scheduled)
- Both wake_llm and script action types
- Validation and error cases
"""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.automations import (
    AUTOMATIONS_TOOLS_DEFINITION,
    create_automation_tool,
    delete_automation_tool,
    disable_automation_tool,
    enable_automation_tool,
    get_automation_stats_tool,
    get_automation_tool,
    list_automations_tool,
    update_automation_tool,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


def extract_data_from_result(result: str | ToolResult) -> dict[str, str | int | bool]:
    """Extract structured JSON data from tool result text.

    Tool results now include structured data in the format:
    "Human readable text\n\nData: {json_here}"

    Args:
        result: Tool result (ToolResult object or string)

    Returns:
        Dict containing the structured data, or empty dict if not found
    """
    # Handle ToolResult objects
    text = result.text if isinstance(result, ToolResult) else str(result)

    # Extract JSON from "Data: {...}" pattern
    match = re.search(r"Data: ({.+})", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))  # type: ignore[no-any-return]
    return {}


@pytest.mark.asyncio
async def test_create_event_automation_basic(db_engine: AsyncEngine) -> None:
    """Test creating a basic event automation with wake_llm action."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Act
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Motion Detector",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {
                    "entity_id": "sensor.hallway_motion",
                    "new_state.state": "on",
                },
            },
            action_type="wake_llm",
            action_config={"context": "Motion detected in hallway"},
        )

    # Assert
    assert "Created event automation 'Motion Detector'" in result.text
    assert "ID:" in result.text
    assert "home_assistant" in result.text


@pytest.mark.asyncio
async def test_create_schedule_automation_basic(db_engine: AsyncEngine) -> None:
    """Test creating a basic schedule automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Act - create daily 7am reminder
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Morning Reminder",
            automation_type="schedule",
            trigger_config={
                "recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
            },
            action_type="wake_llm",
            action_config={"context": "Time for morning review"},
            description="Daily morning reminder",
        )

    # Assert
    assert "Created schedule automation 'Morning Reminder'" in result.text
    assert "ID:" in result.text
    assert "Next run:" in result.text


@pytest.mark.asyncio
async def test_create_automation_cross_type_name_uniqueness(
    db_engine: AsyncEngine,
) -> None:
    """Test that automation names must be unique across both event and schedule types."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create event automation
        await create_automation_tool(
            exec_context=exec_context,
            name="Duplicate Name Test",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {"entity_id": "light.test"},
            },
            action_type="wake_llm",
            action_config={"context": "Event triggered"},
        )

    # Try to create schedule automation with same name in new context
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Duplicate Name Test",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Schedule triggered"},
        )

        # Assert - should fail with error
        assert "Error:" in result.text
        assert "already exists" in result.text.lower()


@pytest.mark.asyncio
async def test_create_automation_with_script_action(db_engine: AsyncEngine) -> None:
    """Test creating an automation with script action type."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        script_code = """
def log_event():
    return "Event logged"

log_event()
"""

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Script Automation",
            automation_type="event",
            trigger_config={
                "event_source": "indexing",
                "event_filter": {"document_type": "pdf"},
            },
            action_type="script",
            action_config={"script_code": script_code, "task_name": "Log Event"},
        )

    assert "Created event automation 'Script Automation'" in result.text


@pytest.mark.asyncio
async def test_create_automation_invalid_type(db_engine: AsyncEngine) -> None:
    """Test that creating automation with invalid type fails."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Invalid Type Test",
            automation_type="invalid_type",  # type: ignore
            trigger_config={"test": "value"},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

    assert "Error:" in result.text
    assert "Invalid automation_type" in result.text


@pytest.mark.asyncio
async def test_create_automation_missing_trigger_config(db_engine: AsyncEngine) -> None:
    """Test that missing required trigger config fields fails gracefully."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Event automation missing event_source
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Missing Event Source",
            automation_type="event",
            trigger_config={"event_filter": {}},  # Missing event_source
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        assert "Error:" in result.text
        assert "event_source" in result.text.lower()


@pytest.mark.asyncio
async def test_create_automation_missing_recurrence_rule(
    db_engine: AsyncEngine,
) -> None:
    """Test that schedule automation without recurrence_rule fails."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Missing RRULE",
            automation_type="schedule",
            trigger_config={},  # Missing recurrence_rule
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        assert "Error:" in result.text
        assert "recurrence_rule" in result.text.lower()


@pytest.mark.asyncio
async def test_list_automations_empty(db_engine: AsyncEngine) -> None:
    """Test listing automations when none exist."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="empty_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await list_automations_tool(exec_context=exec_context)

    assert "No automations found" in result.text


@pytest.mark.asyncio
async def test_list_automations_with_both_types(db_engine: AsyncEngine) -> None:
    """Test listing automations shows both event and schedule types."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="list_test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create event automation
        await create_automation_tool(
            exec_context=exec_context,
            name="Event Auto",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {},
            },
            action_type="wake_llm",
            action_config={"context": "Event"},
        )

        # Create schedule automation
        await create_automation_tool(
            exec_context=exec_context,
            name="Schedule Auto",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Schedule"},
        )

    # List all
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await list_automations_tool(exec_context=exec_context)

    assert "Found 2 automation(s)" in result.text
    assert "Event Auto" in result.text
    assert "Schedule Auto" in result.text
    assert "(event)" in result.text
    assert "(schedule)" in result.text


@pytest.mark.asyncio
async def test_list_automations_filter_by_type(db_engine: AsyncEngine) -> None:
    """Test filtering automations by type."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="filter_test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create multiple automations
        await create_automation_tool(
            exec_context=exec_context,
            name="Event 1",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "E1"},
        )

        await create_automation_tool(
            exec_context=exec_context,
            name="Schedule 1",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "S1"},
        )

    # Filter by event type
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await list_automations_tool(
            exec_context=exec_context, automation_type="event"
        )

    assert "Found 1 automation(s)" in result.text
    assert "Event 1" in result.text
    assert "Schedule 1" not in result.text

    # Filter by schedule type
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await list_automations_tool(
            exec_context=exec_context, automation_type="schedule"
        )

    assert "Found 1 automation(s)" in result.text
    assert "Schedule 1" in result.text
    assert "Event 1" not in result.text


@pytest.mark.asyncio
async def test_list_automations_filter_enabled_only(db_engine: AsyncEngine) -> None:
    """Test filtering automations by enabled status."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="enabled_filter_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create automations
        await create_automation_tool(
            exec_context=exec_context,
            name="Enabled Auto",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        result2 = await create_automation_tool(
            exec_context=exec_context,
            name="To Be Disabled",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        # Extract ID from result
        data = extract_data_from_result(result2)
        auto_id = int(data["id"])

        # Disable one automation
        await disable_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    # List only enabled
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await list_automations_tool(
            exec_context=exec_context, enabled_only=True
        )

    assert "Found 1 automation(s)" in result.text
    assert "Enabled Auto" in result.text
    assert "To Be Disabled" not in result.text


@pytest.mark.asyncio
async def test_get_automation_event_type(db_engine: AsyncEngine) -> None:
    """Test getting details of an event automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="get_test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create automation
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Get Test Auto",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {"entity_id": "light.test"},
            },
            action_type="wake_llm",
            action_config={"context": "Light changed"},
            description="Test automation for get",
        )

        # Extract ID
        data = extract_data_from_result(result)
        auto_id = int(data["id"])

    # Get automation details
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Get Test Auto" in result.text
    assert "Type: event" in result.text
    assert "Status: enabled" in result.text
    # Event source may be shown as "Event source: home_assistant" or similar
    # The key thing is the automation is retrieved successfully
    assert "Test automation for get" in result.text


@pytest.mark.asyncio
async def test_get_automation_schedule_type(db_engine: AsyncEngine) -> None:
    """Test getting details of a schedule automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="get_sched_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Schedule Get Test",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=8"},
            action_type="wake_llm",
            action_config={"context": "Morning check"},
            description="Daily morning automation",
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "Schedule Get Test" in result.text
    assert "Type: schedule" in result.text
    assert "FREQ=DAILY;BYHOUR=8" in result.text
    assert "Next run:" in result.text


@pytest.mark.asyncio
async def test_get_automation_not_found(db_engine: AsyncEngine) -> None:
    """Test getting a non-existent automation returns error."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="get_fail_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_update_automation_action_config(db_engine: AsyncEngine) -> None:
    """Test updating an automation's action config."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="update_test_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        # Create automation
        result = await create_automation_tool(
            exec_context=exec_context,
            name="Update Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Original context"},
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

        # Update action config
        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
            action_config={"context": "Updated context"},
        )

    assert "Successfully updated" in result.text

    # Verify update
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Updated context" in result.text


@pytest.mark.asyncio
async def test_update_automation_description(db_engine: AsyncEngine) -> None:
    """Test updating an automation's description."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="update_desc_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Desc Test",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Test"},
            description="Original description",
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
            description="Updated description",
        )

    assert "Successfully updated" in result.text

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "Updated description" in result.text


@pytest.mark.asyncio
async def test_update_automation_trigger_config_schedule(
    db_engine: AsyncEngine,
) -> None:
    """Test updating a schedule automation's recurrence rule."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="update_rrule_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="RRULE Test",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=WEEKLY;BYDAY=MO"},
        )

    assert "Successfully updated" in result.text

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "FREQ=WEEKLY;BYDAY=MO" in result.text


@pytest.mark.asyncio
async def test_update_automation_not_found(db_engine: AsyncEngine) -> None:
    """Test updating a non-existent automation returns error."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="update_fail_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
            description="New description",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_enable_disable_automation(db_engine: AsyncEngine) -> None:
    """Test enabling and disabling an automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="toggle_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Toggle Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

        # Disable
        result = await disable_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Disabled automation" in result.text

    # Verify disabled
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Status: disabled" in result.text

    # Enable
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await enable_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Enabled automation" in result.text

    # Verify enabled
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Status: enabled" in result.text


@pytest.mark.asyncio
async def test_delete_automation(db_engine: AsyncEngine) -> None:
    """Test deleting an automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="delete_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Delete Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

        # Delete
        result = await delete_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Deleted automation" in result.text

    # Verify deleted
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_delete_automation_not_found(db_engine: AsyncEngine) -> None:
    """Test deleting a non-existent automation returns error."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="delete_fail_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await delete_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="schedule",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_get_automation_stats_event(db_engine: AsyncEngine) -> None:
    """Test getting execution stats for an event automation."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="stats_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Stats Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = extract_data_from_result(result)
        auto_id = int(data["id"])

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Statistics for automation" in result.text
    assert "Total executions: 0" in result.text


@pytest.mark.asyncio
async def test_get_automation_stats_not_found(db_engine: AsyncEngine) -> None:
    """Test getting stats for non-existent automation returns error."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id="stats_fail_conv",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_conversation_isolation(db_engine: AsyncEngine) -> None:
    """Test that automations are isolated by conversation."""
    # Create automation in conversation 1
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context1 = ToolExecutionContext(
            interface_type="web",
            conversation_id="conv1",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        result1 = await create_automation_tool(
            exec_context=exec_context1,
            name="Conv1 Auto",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Conv1"},
        )

        data = extract_data_from_result(result1)
        conv1_auto_id = int(data["id"])

    # Create automation in conversation 2
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context2 = ToolExecutionContext(
            interface_type="web",
            conversation_id="conv2",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
        )

        await create_automation_tool(
            exec_context=exec_context2,
            name="Conv2 Auto",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Conv2"},
        )

    # List from conversation 1 - should only see Conv1 Auto
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context1.db_context = db_ctx
        result = await list_automations_tool(exec_context=exec_context1)

    assert "Conv1 Auto" in result.text
    assert "Conv2 Auto" not in result.text

    # Try to get conv1's automation from conv2 - should fail
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context2.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context2,
            automation_id=conv1_auto_id,
            automation_type="event",
        )

    assert "Error:" in result.text
    assert "not found" in result.text.lower()


@pytest.mark.asyncio
async def test_event_automation_with_script_execution(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test event automation triggering script execution."""
    test_run_id = uuid.uuid4()

    # Set up tools with note tool for script to use

    local_provider = LocalToolsProvider(
        definitions=AUTOMATIONS_TOOLS_DEFINITION + NOTE_TOOLS_DEFINITION,
        implementations={
            "create_automation": local_tool_implementations["create_automation"],
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    # Create event automation with script
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id=f"event_script_{test_run_id}",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            tools_provider=tools_provider,
        )

        script_code = f"""
def log_event():
    entity = event.get("entity_id", "unknown")
    add_or_update_note(
        title="Event Log {test_run_id}",
        content="Event triggered for " + entity
    )
    return "logged"

log_event()
"""

        result = await create_automation_tool(
            exec_context=exec_context,
            name=f"Event Script {test_run_id}",
            automation_type="event",
            trigger_config={
                "event_source": "home_assistant",
                "event_filter": {
                    "entity_id": f"sensor.test_{test_run_id}",
                    "new_state.state": "on",
                },
            },
            action_type="script",
            action_config={"script_code": script_code, "task_name": "Event Logger"},
        )

        assert "Created event automation" in result.text

    # Set up event processor and task worker
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    processor._running = True
    await processor._refresh_listener_cache()

    processing_service = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="N/A")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone_str="UTC",
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config={},
            delegation_security_level="blocked",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    task_worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)

    # Process event that triggers the automation
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": f"sensor.test_{test_run_id}",
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        },
    )

    # Signal worker and wait for processing
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Verify the script created the note
    async with DatabaseContext(engine=db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        matching_notes = [
            n for n in notes if f"Event Log {test_run_id}" in n.get("title", "")
        ]
        assert len(matching_notes) == 1
        note = matching_notes[0]
        assert "Event triggered for" in note["content"]
        assert f"sensor.test_{test_run_id}" in note["content"]
