"""
Simplified functional test for script execution via event listeners.
"""

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import EventActionType, EventSourceType

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_script_execution_creates_note(test_db_engine: AsyncEngine) -> None:
    """Test that a script triggered by an event can create a note."""

    # Step 1: Create event listener with script action using raw SQL
    # (since this is how it's done in the event system tests)
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        await db_ctx.execute_with_retry(
            text("""INSERT INTO event_listeners 
                 (name, match_conditions, source_id, action_type, action_config, enabled, 
                  conversation_id, interface_type)
                 VALUES (:name, :conditions, :source_id, :action_type, :action_config, 
                         :enabled, :conversation_id, :interface_type)"""),
            {
                "name": "Test Script Logger",
                "conditions": json.dumps({
                    "entity_id": "sensor.test_temperature",
                }),
                "source_id": EventSourceType.home_assistant.value,
                "action_type": EventActionType.script.value,
                "action_config": json.dumps({
                    "script_code": """
# Simple script that creates a note
temp = float(event["new_state"]["state"])
add_or_update_note(
    title="Temperature Log",
    content="Temperature: " + str(temp) + "°C"
)
"""
                }),
                "enabled": True,
                "conversation_id": "test_conv_123",
                "interface_type": "telegram",
            },
        )

    # Step 2: Simulate the complete flow by directly enqueuing a script task
    # (This avoids the complexity of EventProcessor and focuses on the script execution)
    test_event = {
        "entity_id": "sensor.test_temperature",
        "old_state": {"state": "20.0"},
        "new_state": {"state": "22.5"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        # Enqueue a script execution task as the event processor would
        await db_ctx.tasks.enqueue(
            task_id="test_script_task_001",
            task_type="script_execution",
            payload={
                "script_code": """
temp = float(event["new_state"]["state"])
add_or_update_note(
    title="Temperature Log",
    content="Temperature: " + str(temp) + "°C"
)
""",
                "event_data": test_event,
                "config": {},
                "listener_id": 1,
                "conversation_id": "test_conv_123",
            },
        )

    # Step 3: Start a minimal task worker to process the script
    from unittest.mock import AsyncMock, MagicMock

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService, ProcessingServiceConfig
    from family_assistant.task_worker import TaskWorker, handle_script_execution
    from family_assistant.tools import (
        AVAILABLE_FUNCTIONS as local_tool_implementations,
    )
    from family_assistant.tools import (
        NOTE_TOOLS_DEFINITION,
        CompositeToolsProvider,
        LocalToolsProvider,
    )
    from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

    # Minimal tools for script
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    # Minimal processing service
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

    # Create worker
    shutdown_event = asyncio.Event()
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

    # Process one task cycle by starting worker briefly
    worker_task = asyncio.create_task(task_worker.run(asyncio.Event()))
    await asyncio.sleep(0.5)  # Give worker time to process the task
    task_worker.should_stop = True
    await asyncio.sleep(0.1)
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # Step 4: Verify the note was created
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        note = await db_ctx.notes.get_by_title("Temperature Log")
        assert note is not None
        assert "Temperature: 22.5°C" in note["content"]

    logger.info("Script successfully created note")


@pytest.mark.asyncio
async def test_script_with_error_creates_no_note(test_db_engine: AsyncEngine) -> None:
    """Test that a script with errors doesn't create notes."""

    # Enqueue a script task with invalid syntax
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        await db_ctx.tasks.enqueue(
            task_id="test_bad_script_001",
            task_type="script_execution",
            payload={
                "script_code": "this is not valid starlark!",
                "event_data": {"test": "data"},
                "config": {},
                "listener_id": 2,
                "conversation_id": "test_conv_456",
            },
        )

    # Setup minimal infrastructure
    from unittest.mock import AsyncMock, MagicMock

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService, ProcessingServiceConfig
    from family_assistant.task_worker import TaskWorker, handle_script_execution
    from family_assistant.tools import CompositeToolsProvider
    from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

    tools_provider = CompositeToolsProvider(providers=[])
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

    shutdown_event = asyncio.Event()
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

    # Try to process the bad script task
    worker_task = asyncio.create_task(task_worker.run(asyncio.Event()))
    await asyncio.sleep(0.5)  # Give worker time to process the task
    task_worker.should_stop = True
    await asyncio.sleep(0.1)
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # Verify no notes were created
    async with DatabaseContext(engine=test_db_engine) as db_ctx:
        notes = await db_ctx.notes.get_all()
        assert len(notes) == 0

    logger.info("Bad script correctly failed without creating notes")
