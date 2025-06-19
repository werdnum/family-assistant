"""
Functional tests for script execution via event listeners.
"""

import asyncio
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventActionType, EventSourceType
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_script_execution_creates_note(test_db_engine: AsyncEngine) -> None:
    """Test end-to-end flow: event triggers script that creates a note."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Execution Test ({test_run_id}) ---")

    # Step 1: Create event listener with script action
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        await db_ctx.events.create_event_listener(
            name=f"Temperature Logger {test_run_id}",
            source_id=EventSourceType.home_assistant,
            match_conditions={
                "entity_id": "sensor.test_temperature",
            },
            conversation_id="test_conv",
            interface_type="telegram",
            action_type=EventActionType.script,
            action_config={
                "script_code": """
temp = float(event["new_state"]["state"])
add_or_update_note(
    title="Temperature Log",
    content="Temperature: " + str(temp) + "°C"
)
"""
            },
            enabled=True,
        )

    # Step 2: Create minimal infrastructure
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    # Event processor
    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    # Real tools provider with note tool
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

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

    # Step 3: Process event that triggers the script
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.test_temperature",
            "old_state": {"state": "20.0"},
            "new_state": {"state": "22.5"},
        },
    )

    # Signal worker and wait for processing
    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    # Step 4: Verify user-visible outcome - note was created
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        note = await db_ctx.notes.get_by_title("Temperature Log")
        assert note is not None
        assert "Temperature: 22.5°C" in note["content"]

    logger.info("Script executed successfully and created note")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Execution Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_with_syntax_error_creates_no_note(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that script with syntax error doesn't create any notes."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Syntax Error Test ({test_run_id}) ---")

    # Step 1: Create event listener with invalid script
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        await db_ctx.events.create_event_listener(
            name=f"Bad Script {test_run_id}",
            source_id=EventSourceType.home_assistant,
            match_conditions={
                "entity_id": "sensor.bad_script",
            },
            conversation_id="test_conv",
            interface_type="telegram",
            action_type=EventActionType.script,
            action_config={"script_code": "this is not valid starlark syntax!"},
            enabled=True,
        )

    # Step 2: Create infrastructure
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

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
        task_worker.run(new_task_event), name=f"ErrorWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 3: Process event
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.bad_script",
            "old_state": {"state": "0"},
            "new_state": {"state": "1"},
        },
    )

    new_task_event.set()

    # Expect the task to fail due to syntax error
    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            test_db_engine, task_types={"script_execution"}
        )

    # Step 4: Verify no notes were created
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        assert len(notes) == 0, "No notes should be created when script has errors"

    logger.info("Confirmed no notes created for script with syntax error")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Syntax Error Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_creates_multiple_notes(test_db_engine: AsyncEngine) -> None:
    """Test that script can create multiple notes using different tools."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Multi-Note Test ({test_run_id}) ---")

    # Step 1: Create event listener with script that creates multiple notes
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        await db_ctx.events.create_event_listener(
            name=f"Multi Note Logger {test_run_id}",
            source_id=EventSourceType.home_assistant,
            match_conditions={
                "entity_id": "sensor.multi_test",
            },
            conversation_id="test_conv",
            interface_type="telegram",
            action_type=EventActionType.script,
            action_config={
                "script_code": """
# Create first note
add_or_update_note(
    title="Event Log",
    content="Event received at " + time_format(time_now(), "%H:%M:%S")
)

# Create second note with event details
entity = event["entity_id"]
add_or_update_note(
    title="Event Details",
    content="Entity: " + entity + ", New State: " + event["new_state"]["state"]
)
"""
            },
            enabled=True,
        )

    # Step 2: Create infrastructure
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    processor = EventProcessor(sources={}, sample_interval_hours=1.0)
    processor._running = True
    await processor._refresh_listener_cache()

    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

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
        task_worker.run(new_task_event), name=f"MultiWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 3: Process event
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.multi_test",
            "old_state": {"state": "idle"},
            "new_state": {"state": "active"},
        },
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(test_db_engine, task_types={"script_execution"})

    # Step 4: Verify both notes were created
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        all_notes = await db_ctx.notes.get_all()
        note_titles = {n["title"] for n in all_notes}

        assert "Event Log" in note_titles
        assert "Event Details" in note_titles

        # Verify Event Details content
        details_notes = [n for n in all_notes if n["title"] == "Event Details"]
        assert len(details_notes) == 1
        assert "Entity: sensor.multi_test" in details_notes[0]["content"]
        assert "New State: active" in details_notes[0]["content"]

    logger.info("Script successfully created multiple notes")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Multi-Note Test ({test_run_id}) Passed ---")
