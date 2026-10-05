"""Tests for scheduled script execution functionality."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import LLMInterface, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import ToolMessage
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker, handle_script_execution
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from family_assistant.utils.clock import MockClock
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

# Test configuration
TEST_CHAT_ID = 12345
TEST_USER_NAME = "ScriptTester"
SCRIPT_DELAY_SECONDS = 2


@pytest.mark.asyncio
async def test_schedule_script_execution(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Test that schedule_action tool can schedule a script for future execution."""
    # Arrange
    initial_time = mock_clock.now()
    script_dt = initial_time + timedelta(seconds=SCRIPT_DELAY_SECONDS)
    script_time_iso = script_dt.isoformat()

    # Unique note title to verify script execution
    test_note_title = f"Test Script Note {TEST_CHAT_ID}_{script_time_iso}"

    test_script = f"""
# Test script that creates a note
result = add_or_update_note(
    title="{test_note_title}",
    content="This note was created by a scheduled script at " + str(time_now_utc()["unix"])
)
print("Script executed - note created: " + str(result))
"""

    # Define LLM rule to schedule script
    def schedule_script_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "schedule a script" in last_text and kwargs.get("tools") is not None

    schedule_response = MockLLMOutput(
        content=f"I'll schedule the script to run at {script_time_iso}.",
        tool_calls=[
            ToolCallItem(
                id="call_schedule_script",
                type="function",
                function=ToolCallFunction(
                    name="schedule_action",
                    arguments=json.dumps({
                        "schedule_time": script_time_iso,
                        "action_type": "script",
                        "action_config": {
                            "script_code": test_script,
                        },
                    }),
                ),
            )
        ],
    )

    # Add a rule to handle the tool response
    def tool_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if messages and len(messages) >= 2:
            last_msg = messages[-1]
            if last_msg.role == "tool":
                return True
        return False

    tool_response_output = MockLLMOutput(
        content="The script has been scheduled successfully."
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (schedule_script_matcher, schedule_response),
            (tool_response_matcher, tool_response_output),
        ],
        default_response=MockLLMOutput(content="I can help with that."),
    )

    # Setup dependencies
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "Test assistant"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="test_profile",
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        clock=mock_clock,
        credential_resolvers=None,
        api_backend=None,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_message_id"

    # Create task worker using the fixture
    task_worker, test_new_task_event, test_shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
    )

    # Register the script execution handler
    task_worker.register_task_handler("script_execution", handle_script_execution)

    db_context = Database(engine=db_engine)
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        chat_interface=mock_chat_interface,
        interface_type="test",
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[
            {"type": "text", "text": "Please schedule a script to run later"}
        ],
        trigger_interface_message_id="501",
        user_name=TEST_USER_NAME,
    )

    assert result.error_traceback is None
    task_rows = await db_context.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "script_execution")
    )
    assert len(task_rows) == 1
    assert task_rows[0]["payload"]["script_code"] == test_script
    scheduled_at = task_rows[0]["scheduled_at"]
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    assert scheduled_at == script_dt.astimezone(UTC)

    mock_clock.advance(timedelta(seconds=SCRIPT_DELAY_SECONDS + 1))
    test_new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=db_engine, timeout_seconds=15.0, task_types={"script_execution"}
    )

    note = await db_context.notes.get_by_title(
        test_note_title, read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None, f"Expected the script to create note '{test_note_title}'"
    assert "scheduled script" in note.content


@pytest.mark.asyncio
async def test_schedule_action_rejects_script_with_syntax_error(
    db_engine: AsyncEngine,
    mock_clock: MockClock,
) -> None:
    """schedule_action validates the script when called, reports the syntax
    error back to the LLM, and enqueues nothing."""
    # Arrange
    initial_time = mock_clock.now()
    script_dt = initial_time + timedelta(seconds=1)
    script_time_iso = script_dt.isoformat()

    # Script with syntax error
    invalid_script = """
# Invalid script
if True  # Missing colon
    add_or_update_note(title="Test", content="This won't work")
"""

    # Define LLM rule
    def schedule_invalid_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "invalid script" in last_text and kwargs.get("tools") is not None

    schedule_response = MockLLMOutput(
        content="Scheduling the script as requested.",
        tool_calls=[
            ToolCallItem(
                id="call_invalid_script",
                type="function",
                function=ToolCallFunction(
                    name="schedule_action",
                    arguments=json.dumps({
                        "schedule_time": script_time_iso,
                        "action_type": "script",
                        "action_config": {
                            "script_code": invalid_script,
                        },
                    }),
                ),
            )
        ],
    )

    # Add a rule to handle the tool response
    def tool_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if messages and len(messages) >= 2:
            last_msg = messages[-1]
            if last_msg.role == "tool":
                return True
        return False

    tool_response_output = MockLLMOutput(content="The script could not be scheduled.")

    llm_client = RuleBasedMockLLMClient(
        rules=[
            (schedule_invalid_matcher, schedule_response),
            (tool_response_matcher, tool_response_output),
        ],
        default_response=MockLLMOutput(content="I can help with that."),
    )

    # Setup dependencies (simplified)
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, MCPToolsProvider(mcp_server_configs={})]
    )
    await composite_provider.get_tool_definitions()

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "Test"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id="test_profile",
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        clock=mock_clock,
        credential_resolvers=None,
        api_backend=None,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_message_id"

    db_context = Database(engine=db_engine)
    result = await processing_service.handle_chat_interaction(
        db_context=db_context,
        chat_interface=mock_chat_interface,
        interface_type="test",
        conversation_id=str(TEST_CHAT_ID),
        trigger_content_parts=[{"type": "text", "text": "Schedule an invalid script"}],
        trigger_interface_message_id="701",
        user_name=TEST_USER_NAME,
    )

    assert result.error_traceback is None
    history = await db_context.message_history.get_recent(
        interface_type="test",
        conversation_id=str(TEST_CHAT_ID),
        current_time=mock_clock.now(),
    )
    tool_messages = [
        message
        for message in history
        if isinstance(message, ToolMessage) and message.name == "schedule_action"
    ]
    assert len(tool_messages) == 1
    assert "Script validation failed" in tool_messages[0].content
    assert "Syntax error" in tool_messages[0].content
    task_rows = await db_context.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "script_execution")
    )
    assert task_rows == []
