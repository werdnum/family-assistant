"""
Functional tests for script execution via event listeners.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.events import EventActionType, EventSourceType
from family_assistant.storage.repositories.notes import NoteWritePolicy
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.stored_scripts import save_script_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_script_execution_creates_note(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test end-to-end flow: event triggers script that creates a note."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Execution Test ({test_run_id}) ---")

    # Step 1: Create event listener with script action
    db_ctx = Database(engine=db_engine)
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
    # Event processor with database access
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

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
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    # Start task worker using the fixture
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start and register handler
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
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 4: Verify user-visible outcome - note was created
    db_ctx = Database(engine=db_engine)
    note = await db_ctx.notes.get_by_title("Temperature Log", visibility_grants=None)
    assert note is not None
    assert "Temperature: 22.5°C" in note.content

    logger.info("Script executed successfully and created note")

    # Cleanup is handled by the task_worker_manager fixture
    logger.info(f"--- Script Execution Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_execution_by_stored_name(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that handle_script_execution resolves and runs a stored script by name."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Stored Script Execution Test ({test_run_id}) ---")

    # Set up tools provider for save_script_tool validation
    save_local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    save_tools_provider = CompositeToolsProvider(providers=[save_local_provider])
    await save_tools_provider.get_tool_definitions()

    # Step 1: Save a script via save_script_tool (declares 'event' in schema so
    # validation accepts the runtime global) and create automation referencing it by name
    db_ctx = Database(engine=db_engine)
    tool_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="Test User",
        turn_id="turn-1",
        db_context=db_ctx,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        tools_provider=save_tools_provider,
        credential_resolvers=None,
        api_backend=None,
    )
    save_result = await save_script_tool(
        tool_context,
        name=f"log_temp_{test_run_id}",
        description="Log temperature to a note",
        code=(
            'temp = float(event["new_state"]["state"])\n'
            "add_or_update_note(\n"
            '    title="Stored Temp Log",\n'
            '    content="Stored: " + str(temp) + "°C"\n'
            ")\n"
        ),
        parameters_schema={
            "type": "object",
            "properties": {"event": {"type": "object"}},
        },
    )
    assert isinstance(save_result.data, dict)
    assert "error" not in save_result.data, f"save_script failed: {save_result.data}"

    await db_ctx.events.create_event_listener(
        name=f"Stored Temperature Logger {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={"entity_id": "sensor.test_temperature"},
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={"script_name": f"log_temp_{test_run_id}"},
        enabled=True,
    )

    # Step 2: Minimal infrastructure
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )
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
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    worker, new_task_event, _shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start and register handler
    await asyncio.sleep(0.1)

    # Step 3: Trigger the event
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.test_temperature",
            "old_state": {"state": "18.0"},
            "new_state": {"state": "24.5"},
        },
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 4: Verify the stored script was resolved and executed
    db_ctx = Database(engine=db_engine)
    note = await db_ctx.notes.get_by_title("Stored Temp Log", visibility_grants=None)
    assert note is not None
    assert "Stored: 24.5°C" in note.content

    logger.info(f"--- Stored Script Execution Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_with_syntax_error_creates_no_note(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that script with syntax error doesn't create any notes."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Syntax Error Test ({test_run_id}) ---")

    # Step 1: Create event listener with invalid script
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name=f"Bad Script {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={
            "entity_id": "sensor.bad_script",
        },
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={"script_code": "this is not valid syntax!"},
        enabled=True,
    )

    # Step 2: Create infrastructure
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

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
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    # Start task worker using the fixture
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start and register handler
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
        await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 4: Verify no notes were created
    db_ctx = Database(engine=db_engine)
    notes = await db_ctx.notes.get_all(visibility_grants=None)
    assert len(notes) == 0, "No notes should be created when script has errors"

    logger.info("Confirmed no notes created for script with syntax error")

    # Cleanup is handled by the task_worker_manager fixture
    logger.info(f"--- Script Syntax Error Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_creates_multiple_notes(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that script can create multiple notes using different tools."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Multi-Note Test ({test_run_id}) ---")

    # Step 1: Create event listener with script that creates multiple notes
    db_ctx = Database(engine=db_engine)
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
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

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
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    # Start task worker using the fixture
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start and register handler
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
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 4: Verify both notes were created
    db_ctx = Database(engine=db_engine)
    all_notes = await db_ctx.notes.get_all(visibility_grants=None)
    note_titles = {n.title for n in all_notes}

    assert "Event Log" in note_titles
    assert "Event Details" in note_titles

    # Verify Event Details content
    details_notes = [n for n in all_notes if n.title == "Event Details"]
    assert len(details_notes) == 1
    assert "Entity: sensor.multi_test" in details_notes[0].content
    assert "New State: active" in details_notes[0].content

    logger.info("Script successfully created multiple notes")

    # Cleanup is handled by the task_worker_manager fixture
    logger.info(f"--- Script Multi-Note Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_can_retrieve_notes(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that scripts can retrieve notes via list_notes and get_note tools.

    This verifies that the database context is properly passed through the
    MontyEngine to tool calls, enabling read operations (not just writes).
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Note Retrieval Test ({test_run_id}) ---")

    # Step 1: Pre-create a note that the script will try to retrieve
    db_ctx = Database(engine=db_engine)
    await db_ctx.notes.add_or_update(
        title="Test Note Alpha",
        content="This is a test note for retrieval",
        include_in_prompt=True,
        write_policy=NoteWritePolicy.UNCONSTRAINED,
    )

    # Step 2: Create event listener with script that reads notes
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name=f"Note Reader {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={
            "entity_id": "sensor.note_reader_test",
        },
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={
            "script_code": """
# Retrieve notes via list_notes tool
notes_list = json_decode(list_notes())

# Find our test note
found = False
for note in notes_list:
    if note["title"] == "Test Note Alpha":
        found = True

# Also test get_note for a specific note
detail = json_decode(get_note(title="Test Note Alpha"))

# Create a summary note with results
add_or_update_note(
    title="Retrieval Results",
    content="Found via list: " + str(found) + ", Exists: " + str(detail["exists"]) + ", Content: " + str(detail["content"])
)
"""
        },
        enabled=True,
    )

    # Step 3: Create infrastructure with all note tools
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    processor._running = True
    await processor._refresh_listener_cache()

    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
            "list_notes": local_tool_implementations["list_notes"],
            "get_note": local_tool_implementations["get_note"],
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
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start and register handler
    await asyncio.sleep(0.1)

    # Step 4: Process event that triggers the script
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.note_reader_test",
            "old_state": {"state": "idle"},
            "new_state": {"state": "active"},
        },
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 5: Verify the script successfully retrieved and processed notes
    db_ctx = Database(engine=db_engine)
    result_note = await db_ctx.notes.get_by_title(
        "Retrieval Results", visibility_grants=None
    )
    assert result_note is not None, "Script should have created a result note"
    assert "Found via list: True" in result_note.content
    assert "Exists: True" in result_note.content
    assert "This is a test note for retrieval" in result_note.content

    logger.info("Script successfully retrieved notes via tools")
    logger.info(f"--- Script Note Retrieval Test ({test_run_id}) Passed ---")
