"""Automation execution logic and scheduling tests."""

import asyncio
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
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
    get_automation_stats_tool,
)
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient


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
            camera_backend=None,
        )

        result = await create_automation_tool(
            exec_context=exec_context,
            name="Stats Test",
            automation_type="event",
            trigger_config={"event_source": "home_assistant", "event_filter": {}},
            action_type="wake_llm",
            action_config={"context": "Test"},
        )

        data = result.get_data()
        assert isinstance(data, dict), "Expected structured data"
        assert "id" in data, "Missing id in result data"
        auto_id = int(data["id"])

    async with DatabaseContext(engine=db_engine) as db_ctx:
        exec_context.db_context = db_ctx
        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=auto_id,
            automation_type="event",
        )

    assert "Statistics for automation" in result.get_text()
    assert "Total executions: 0" in result.get_text()


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
            camera_backend=None,
        )

        result = await get_automation_stats_tool(
            exec_context=exec_context,
            automation_id=99999,
            automation_type="event",
        )

    assert "Error:" in result.get_text()
    assert "not found" in result.get_text().lower()


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
            camera_backend=None,
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

        assert "Created event automation" in result.get_text()

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
        app_config=AppConfig(),
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
