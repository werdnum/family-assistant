"""CRUD operations for automations (create, list, get, update, delete)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.automations import (
    create_automation_tool,
    delete_automation_tool,
    disable_automation_tool,
    enable_automation_tool,
    get_automation_tool,
    list_automations_tool,
    update_automation_tool,
)
from family_assistant.tools.types import ToolExecutionContext


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
            camera_backend=None,
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
    assert "Created event automation 'Motion Detector'" in result.get_text()
    assert "ID:" in result.get_text()
    assert "home_assistant" in result.get_text()


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
            camera_backend=None,
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
    assert "Created schedule automation 'Morning Reminder'" in result.get_text()
    assert "ID:" in result.get_text()
    assert "Next run:" in result.get_text()


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
            camera_backend=None,
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
            camera_backend=None,
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
        assert "Error:" in result.get_text()
        assert "already exists" in result.get_text().lower()


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
            camera_backend=None,
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

    assert "Created event automation 'Script Automation'" in result.get_text()


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Invalid Type Test",
            automation_type="invalid_type",  # type: ignore
            trigger_config={"test": "value"},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

    assert "Error:" in result.get_text()
    assert "Invalid automation_type" in result.get_text()


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
            camera_backend=None,
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

        assert "Error:" in result.get_text()
        assert "event_source" in result.get_text().lower()


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Missing RRULE",
            automation_type="schedule",
            trigger_config={},  # Missing recurrence_rule
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        assert "Error:" in result.get_text()
        assert "recurrence_rule" in result.get_text().lower()


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
            camera_backend=None,
        )

        result = await list_automations_tool(exec_context=exec_context)

    assert "No automations found" in result.get_text()


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
            camera_backend=None,
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

    assert "Found 2 automation(s)" in result.get_text()
    assert "Event Auto" in result.get_text()
    assert "Schedule Auto" in result.get_text()
    assert "(event)" in result.get_text()
    assert "(schedule)" in result.get_text()


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
            camera_backend=None,
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

    assert "Found 1 automation(s)" in result.get_text()
    assert "Event 1" in result.get_text()
    assert "Schedule 1" not in result.get_text()

    # Filter by schedule type
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await list_automations_tool(
            exec_context=exec_context, automation_type="schedule"
        )

    assert "Found 1 automation(s)" in result.get_text()
    assert "Schedule 1" in result.get_text()
    assert "Event 1" not in result.get_text()


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
            camera_backend=None,
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
        data = result2.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
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

    assert "Found 1 automation(s)" in result.get_text()
    assert "Enabled Auto" in result.get_text()
    assert "To Be Disabled" not in result.get_text()


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
            camera_backend=None,
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
        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

    # Get automation details
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Get Test Auto" in result.get_text()
    assert "Type: event" in result.get_text()
    assert "Status: enabled" in result.get_text()
    # Event source may be shown as "Event source: home_assistant" or similar
    # The key thing is the automation is retrieved successfully
    assert "Test automation for get" in result.get_text()


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
            camera_backend=None,
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

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "Schedule Get Test" in result.get_text()
    assert "Type: schedule" in result.get_text()
    assert "FREQ=DAILY;BYHOUR=8" in result.get_text()
    assert "Next run:" in result.get_text()


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
            camera_backend=None,
        )

        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()


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
            camera_backend=None,
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

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

        # Update action config
        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
            action_config={"context": "Updated context"},
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("success") is True, "Update should succeed"

    # Verify update
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Updated context" in result.get_text()


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
            camera_backend=None,
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

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
            description="Updated description",
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("success") is True, "Update should succeed"

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "Updated description" in result.get_text()


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="RRULE Test",
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=DAILY"},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
            trigger_config={"recurrence_rule": "FREQ=WEEKLY;BYDAY=MO"},
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("success") is True, "Update should succeed"

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="schedule",
        )

    assert "FREQ=WEEKLY;BYDAY=MO" in result.get_text()


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
            camera_backend=None,
        )

        result = await update_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
            description="New description",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Toggle Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

        # Disable
        result = await disable_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("enabled") is False, "Automation should be disabled"

    # Verify disabled
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Status: disabled" in result.get_text()

    # Enable
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await enable_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("enabled") is True, "Automation should be enabled"

    # Verify enabled
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Status: enabled" in result.get_text()


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Delete Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

        # Delete
        result = await delete_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    data = result.get_data()
    assert isinstance(data, dict), "Expected structured data"
    assert data.get("deleted") is True, "Automation should be deleted"

    # Verify deleted
    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()


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
            camera_backend=None,
        )

        result = await delete_automation_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="schedule",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()
