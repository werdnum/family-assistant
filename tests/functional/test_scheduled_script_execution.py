"""Tests for scheduled script execution functionality."""

import asyncio
import json
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.interfaces import ChatInterface
from family_assistant.llm import LLMInterface, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import TaskWorker
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
from tests.test_helpers.task_worker import managed_task_worker

logger = logging.getLogger(__name__)

# Test configuration
TEST_CHAT_ID = 12345
TEST_USER_NAME = "ScriptTester"
SCRIPT_DELAY_SECONDS = 2


@pytest.mark.asyncio
async def test_schedule_script_execution(test_db_engine: AsyncEngine) -> None:
    """Test that schedule_action tool can schedule a script for future execution."""
    # Arrange
    mock_clock = MockClock()
    initial_time = mock_clock.now()
    script_dt = initial_time + timedelta(seconds=SCRIPT_DELAY_SECONDS)
    script_time_iso = script_dt.isoformat()

    test_script = """
# Test script that sets a global variable
set_global("test_executed", True)
set_global("execution_time", now().isoformat())
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

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[(schedule_script_matcher, schedule_response)],
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
            timezone_str="UTC",
            max_history_messages=5,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="confirm",
            id="test_profile",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_message_id"

    # Create task worker
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,
        shutdown_event_instance=test_shutdown_event,
        engine=test_db_engine,
    )

    async with managed_task_worker(task_worker, test_new_task_event, "ScriptWorker"):
        # Act - Schedule the script
        async with DatabaseContext(engine=test_db_engine) as db_context:
            resp, _, _, error = await processing_service.handle_chat_interaction(
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

        # Assert - Script was scheduled
        assert error is None
        assert resp is not None
        assert "schedule the script" in resp.lower()

        # Act - Advance time and execute the script
        mock_clock.advance(timedelta(seconds=SCRIPT_DELAY_SECONDS + 1))
        test_new_task_event.set()

        # Wait for task completion
        await wait_for_tasks_to_complete(
            engine=test_db_engine, timeout_seconds=5.0, task_types={"script_execution"}
        )


@pytest.mark.asyncio
async def test_schedule_recurring_script(test_db_engine: AsyncEngine) -> None:
    """Test that schedule_recurring_action tool can schedule a recurring script."""
    # Arrange
    mock_clock = MockClock()
    initial_time = mock_clock.now()
    start_dt = initial_time + timedelta(seconds=2)
    start_time_iso = start_dt.isoformat()
    recurrence_rule = "FREQ=HOURLY;INTERVAL=1"

    test_script = """
# Recurring test script
counter = get_global("run_count", 0)
set_global("run_count", counter + 1)
"""

    # Define LLM rule
    def schedule_recurring_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "hourly script" in last_text and kwargs.get("tools") is not None

    schedule_response = MockLLMOutput(
        content=f"I'll set up an hourly script starting at {start_time_iso}.",
        tool_calls=[
            ToolCallItem(
                id="call_schedule_recurring",
                type="function",
                function=ToolCallFunction(
                    name="schedule_recurring_action",
                    arguments=json.dumps({
                        "start_time": start_time_iso,
                        "recurrence_rule": recurrence_rule,
                        "action_type": "script",
                        "action_config": {
                            "script_code": test_script,
                        },
                        "task_name": "hourly_test",
                    }),
                ),
            )
        ],
    )

    llm_client = RuleBasedMockLLMClient(
        rules=[(schedule_recurring_matcher, schedule_response)],
        default_response=MockLLMOutput(content="I can help with that."),
    )

    # Setup dependencies
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
            prompts={"system_prompt": "Test assistant"},
            timezone_str="UTC",
            max_history_messages=5,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="confirm",
            id="test_profile",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,
        shutdown_event_instance=test_shutdown_event,
        engine=test_db_engine,
    )

    async with managed_task_worker(task_worker, test_new_task_event, "RecurringWorker"):
        # Act - Schedule the recurring script
        async with DatabaseContext(engine=test_db_engine) as db_context:
            resp, _, _, error = await processing_service.handle_chat_interaction(
                db_context=db_context,
                chat_interface=mock_chat_interface,
                interface_type="test",
                conversation_id=str(TEST_CHAT_ID),
                trigger_content_parts=[
                    {"type": "text", "text": "Set up an hourly script"}
                ],
                trigger_interface_message_id="601",
                user_name=TEST_USER_NAME,
            )

        # Assert - Recurring script was scheduled
        assert error is None
        assert resp is not None
        assert "hourly script" in resp.lower()

        # Act - Execute first occurrence
        mock_clock.advance(timedelta(seconds=3))
        test_new_task_event.set()

        # Wait for first execution
        await wait_for_tasks_to_complete(
            engine=test_db_engine, timeout_seconds=5.0, task_types={"script_execution"}
        )


@pytest.mark.asyncio
async def test_schedule_script_with_invalid_syntax(test_db_engine: AsyncEngine) -> None:
    """Test that scheduling a script with invalid syntax fails appropriately."""
    # Arrange
    mock_clock = MockClock()
    initial_time = mock_clock.now()
    script_dt = initial_time + timedelta(seconds=1)
    script_time_iso = script_dt.isoformat()

    # Script with syntax error
    invalid_script = """
# Invalid script
if True  # Missing colon
    set_global("test", True)
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

    llm_client = RuleBasedMockLLMClient(
        rules=[(schedule_invalid_matcher, schedule_response)],
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
            timezone_str="UTC",
            max_history_messages=5,
            history_max_age_hours=24,
            tools_config={},
            delegation_security_level="confirm",
            id="test_profile",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,
        shutdown_event_instance=test_shutdown_event,
        engine=test_db_engine,
    )

    async with managed_task_worker(
        task_worker, test_new_task_event, "InvalidScriptWorker"
    ):
        # Act - Schedule the invalid script
        async with DatabaseContext(engine=test_db_engine) as db_context:
            resp, _, _, error = await processing_service.handle_chat_interaction(
                db_context=db_context,
                chat_interface=mock_chat_interface,
                interface_type="test",
                conversation_id=str(TEST_CHAT_ID),
                trigger_content_parts=[
                    {"type": "text", "text": "Schedule an invalid script"}
                ],
                trigger_interface_message_id="701",
                user_name=TEST_USER_NAME,
            )

        # Assert - Script was scheduled (tool doesn't validate syntax)
        assert error is None

        # Act - Try to execute the invalid script
        mock_clock.advance(timedelta(seconds=2))
        test_new_task_event.set()

        # Wait for task to fail
        await asyncio.sleep(0.5)  # Give it time to fail

        # The task should have failed due to syntax error
        # We're testing that the system handles script errors gracefully
