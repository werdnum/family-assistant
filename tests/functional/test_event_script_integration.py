"""
Integration tests for event listener script tools.

Tests the full flow from creating script listeners via tools to having them execute.
"""

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventActionType
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.event_listeners import (
    create_event_listener_tool,
    validate_event_listener_script_tool,
)
from family_assistant.tools.event_listeners import (
    test_event_listener_script_tool as script_test_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_create_script_listener_via_tool_and_execute(
    test_db_engine: AsyncEngine,
) -> None:
    """Test end-to-end: create script listener via tool, trigger event, verify execution."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Listener Tool Integration Test ({test_run_id}) ---"
    )

    # Step 1: Set up tools provider with event listener tools
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
            "create_event_listener": local_tool_implementations[
                "create_event_listener"
            ],
            "validate_event_listener_script": local_tool_implementations[
                "validate_event_listener_script"
            ],
            "test_event_listener_script": local_tool_implementations[
                "test_event_listener_script"
            ],
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    # Create execution context for tool calls
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id=f"test_conv_{test_run_id}",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            tools_provider=tools_provider,
        )

        # Step 2: First validate the script
        script_code = """
def log_motion():
    entity = event["entity_id"]
    state = event["new_state"]["state"]
    
    # Use string concatenation instead of f-strings (Starlark doesn't support f-strings)
    add_or_update_note(
        title="Motion Events",
        content="Motion " + state + " on " + entity + " at " + time_format(time_now(), '%H:%M:%S')
    )
    return "logged"

log_motion()
"""

        # Step 2: First validate the script
        validate_result = await validate_event_listener_script_tool(
            exec_context, script_code
        )
        validate_json = json.loads(validate_result)
        if not validate_json["success"]:
            logger.error(f"Validation failed: {validate_json}")
        assert validate_json["success"] is True, f"Validation failed: {validate_json}"

        # Step 3: Test the script with sample event
        sample_event = {
            "entity_id": "binary_sensor.front_door",
            "new_state": {"state": "on"},
            "old_state": {"state": "off"},
        }

        test_result = await script_test_tool(
            exec_context, script_code, sample_event, timeout=2
        )
        test_json = json.loads(test_result)
        if not test_json["success"]:
            logger.error(f"Test script failed: {test_json}")
        assert test_json["success"] is True, f"Test script failed: {test_json}"

        # Step 4: Create event listener via tool
        create_result = await create_event_listener_tool(
            exec_context=exec_context,
            name=f"motion_detector_{test_run_id}",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "binary_sensor.front_door",
                    "new_state.state": "on",
                }
            },
            action_type="script",
            script_code=script_code,
            script_config={"timeout": 30},
        )

        create_json = json.loads(create_result)
        assert create_json["success"] is True
        listener_id = create_json["listener_id"]

        # Verify listener was created correctly
        listeners = await db_ctx.events.get_event_listeners(
            conversation_id=exec_context.conversation_id
        )
        assert len(listeners) == 1
        listener = listeners[0]
        assert listener["id"] == listener_id
        assert listener["name"] == f"motion_detector_{test_run_id}"
        assert listener["action_type"] == EventActionType.script

    # Step 5: Set up infrastructure for event processing
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    # Event processor
    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    # Processing service for event_handler profile
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

    # Start task worker
    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=AsyncMock(spec=ChatInterface),
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        calendar_config={},
        shutdown_event_instance=shutdown_event,
        engine=test_db_engine,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)

    worker_task = asyncio.create_task(
        task_worker.run(new_task_event), name=f"ScriptWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 6: Process event that should trigger the script
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "binary_sensor.front_door",
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        },
    )

    # Signal worker and wait for processing
    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    # Step 7: Verify the script executed and created the note
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        assert len(notes) == 1
        note = notes[0]
        assert note["title"] == "Motion Events"
        assert "Motion on on binary_sensor.front_door" in note["content"]

    logger.info("Script listener created via tool and executed successfully")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Listener Tool Integration Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_listener_with_complex_conditions(
    test_db_engine: AsyncEngine,
) -> None:
    """Test script listener with more complex match conditions and script logic."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Complex Script Listener Test ({test_run_id}) ---")

    # Set up tools
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
            "create_event_listener": local_tool_implementations[
                "create_event_listener"
            ],
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="web",
            conversation_id=f"test_conv_{test_run_id}",
            user_name="test_user",
            turn_id="test_turn",
            db_context=db_ctx,
            tools_provider=tools_provider,
        )

        # Complex script that processes temperature changes
        script_code = """
def process_temperature():
    temp = float(event["new_state"]["state"])
    prev_temp = float(event["old_state"]["state"])
    entity = event["entity_id"]
    
    # Only log significant changes
    # Calculate difference manually since Starlark's abs() only accepts int
    diff = temp - prev_temp
    if diff < 0:
        diff = -diff
    if diff >= 2.0:
        direction = "increased" if temp > prev_temp else "decreased"
        add_or_update_note(
            title="Significant Temperature Changes",
            content=entity + ": Temperature " + direction + " from " + str(prev_temp) + "°C to " + str(temp) + "°C"
        )
        
        # Alert on high temps
        if temp > 30:
            add_or_update_note(
                title="Temperature Alerts",
                content="HIGH TEMP WARNING: " + entity + " is at " + str(temp) + "°C!"
            )
    
    return "Processed: " + str(temp) + "°C"

process_temperature()
"""

        # Create listener with multiple conditions
        create_result = await create_event_listener_tool(
            exec_context=exec_context,
            name=f"temp_monitor_{test_run_id}",
            source="home_assistant",
            listener_config={
                "match_conditions": {
                    "entity_id": "sensor.server_room_temp",
                    # Could add more conditions here if needed
                }
            },
            action_type="script",
            script_code=script_code,
            script_config={"timeout": 60},
        )

        assert json.loads(create_result)["success"] is True

    # Set up infrastructure
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
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

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=AsyncMock(spec=ChatInterface),
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        calendar_config={},
        shutdown_event_instance=shutdown_event,
        engine=test_db_engine,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)

    worker_task = asyncio.create_task(
        task_worker.run(new_task_event), name=f"ComplexWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Test 1: Small temperature change (should not create note)
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.server_room_temp",
            "old_state": {"state": "22.0"},
            "new_state": {"state": "23.0"},  # Only 1°C change
        },
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        assert len(notes) == 0  # No notes for small change

    # Test 2: Large temperature change (should create note)
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.server_room_temp",
            "old_state": {"state": "23.0"},
            "new_state": {"state": "28.0"},  # 5°C change
        },
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        assert len(notes) == 1
        assert notes[0]["title"] == "Significant Temperature Changes"
        assert "increased from 23.0°C to 28.0°C" in notes[0]["content"]

    # Test 3: High temperature (should create alert)
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.server_room_temp",
            "old_state": {"state": "28.0"},
            "new_state": {"state": "32.0"},  # Above 30°C threshold
        },
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        note_titles = {n["title"] for n in notes}
        assert "Temperature Alerts" in note_titles

        alert_notes = [n for n in notes if n["title"] == "Temperature Alerts"]
        assert len(alert_notes) == 1
        assert "HIGH TEMP WARNING" in alert_notes[0]["content"]
        assert "32.0°C" in alert_notes[0]["content"]

    logger.info("Complex script listener executed conditional logic correctly")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Complex Script Listener Test ({test_run_id}) Passed ---")
