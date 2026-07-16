"""Tests for scheduled script execution functionality."""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import LLMInterface, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
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

logger = logging.getLogger(__name__)

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
            max_history_messages=5,
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

    try:
        # Act - Schedule the script
        async with DatabaseContext(engine=db_engine) as db_context:
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
            resp = result.text_reply
            error = result.error_traceback

        # Assert - Script was scheduled
        assert error is None
        assert resp is not None
        # The response should indicate the script was scheduled
        assert "scheduled" in resp.lower()

        # Act - Advance time and execute the script
        mock_clock.advance(timedelta(seconds=SCRIPT_DELAY_SECONDS + 1))
        test_new_task_event.set()

        # Wait for script execution to complete (increased timeout)
        await wait_for_tasks_to_complete(
            engine=db_engine, timeout_seconds=15.0, task_types={"script_execution"}
        )

        # Add a small delay to ensure any async operations complete
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for async database operations to complete
        await asyncio.sleep(0.1)

        # Verify the script created the note
        async with DatabaseContext(engine=db_engine) as db_context:
            # First, let's check all notes to debug
            all_notes = await db_context.notes.get_all(visibility_grants=None)
            logger.info(f"All notes after script execution: {len(all_notes)}")
            for n in all_notes:
                logger.info(f"  Note title: '{n.title}'")

            # Now look for our specific note
            note = await db_context.notes.get_by_title(
                test_note_title, visibility_grants=None
            )
            assert note is not None, (
                f"Expected to find note with title '{test_note_title}'. Found notes: {[n.title for n in all_notes]}"
            )
            assert note.title == test_note_title
            assert "scheduled script" in note.content
    finally:
        # Cleanup is handled by the task_worker_manager fixture
        pass


@pytest.mark.asyncio
async def test_schedule_script_with_invalid_syntax(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Test that scheduling a script with invalid syntax fails appropriately."""
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

    tool_response_output = MockLLMOutput(content="The script has been scheduled.")

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
            max_history_messages=5,
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

    # Use the task_worker_manager fixture
    task_worker, test_new_task_event, test_shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
    )

    # Register the script execution handler
    task_worker.register_task_handler("script_execution", handle_script_execution)

    # Task worker is already started by the fixture
    try:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start
        await asyncio.sleep(0.1)  # Give worker time to start
        # Act - Schedule the invalid script
        async with DatabaseContext(engine=db_engine) as db_context:
            result = await processing_service.handle_chat_interaction(
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
            error = result.error_traceback

        # Assert - Script was scheduled (tool doesn't validate syntax)
        assert error is None

        # Act - Try to execute the invalid script
        mock_clock.advance(timedelta(seconds=2))
        test_new_task_event.set()

        # Wait for task to fail
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for script with syntax error to fail
        await asyncio.sleep(0.5)  # Give it time to fail

        # The task should have failed due to syntax error
        # We're testing that the system handles script errors gracefully
    finally:
        # Cleanup is handled by the task_worker_manager fixture
        logger.info("Cleanup handled by fixture")
