"""Test that recurring tasks respect user timezone when calculating next occurrences."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import select

from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker, handle_llm_callback
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
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_recurring_task_respects_user_timezone(
    test_db_engine: AsyncEngine,
) -> None:
    """
    Test that recurring tasks scheduled with BYHOUR respect the user's timezone.

    This test:
    1. Schedules a recurring task with BYHOUR=6 in Sydney timezone
    2. Verifies the first occurrence is at 6am Sydney time (20:00 UTC previous day)
    3. Executes the task and verifies the next occurrence is also at 6am Sydney time
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Recurring Task Timezone Test ({test_run_id}) ---")

    # Setup mock clock starting at 2025-06-16 05:00:00 UTC (3pm Sydney time)
    mock_clock = MockClock(
        initial_time=datetime(2025, 6, 16, 5, 0, 0, tzinfo=timezone.utc)
    )
    initial_time = mock_clock.now()

    # Sydney timezone
    sydney_tz = ZoneInfo("Australia/Sydney")
    test_chat_id = "test_chat_123"
    test_user_name = "TimezoneTester"

    # Calculate when 6am Sydney time is in UTC for the next day
    # Since it's 3pm Sydney time, the next 6am Sydney is tomorrow
    sydney_now = initial_time.astimezone(sydney_tz)
    next_6am_sydney = sydney_now.replace(hour=6, minute=0, second=0, microsecond=0)
    if next_6am_sydney <= sydney_now:
        next_6am_sydney += timedelta(days=1)

    # Convert to UTC for the initial schedule time
    initial_schedule_utc = next_6am_sydney.astimezone(timezone.utc)
    initial_schedule_iso = initial_schedule_utc.isoformat()

    logger.info(f"Current time: {initial_time} UTC ({sydney_now} Sydney)")
    logger.info(f"Next 6am Sydney: {next_6am_sydney}")
    logger.info(f"Initial schedule time: {initial_schedule_iso} UTC")

    # Define rules for mock LLM
    def schedule_recurring_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages).lower()
        return "schedule daily briefing" in last_text

    schedule_tool_call_id = f"call_schedule_recurring_{test_run_id}"
    schedule_response = MockLLMOutput(
        content="OK, I'll schedule the daily briefing at 6am Sydney time.",
        tool_calls=[
            ToolCallItem(
                id=schedule_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="schedule_recurring_task",
                    arguments=json.dumps({
                        "initial_schedule_time": initial_schedule_iso,
                        "recurrence_rule": "FREQ=DAILY;BYHOUR=6;BYMINUTE=0",
                        "callback_context": "Send the daily briefing",
                        "description": "daily_briefing_6am_sydney",
                    }),
                ),
            )
        ],
    )

    # Define rule for when the callback executes
    def callback_trigger_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, str):
                return (
                    "System Callback Trigger:" in content
                    and "Send the daily briefing" in content
                )
        return False

    callback_response = MockLLMOutput(
        content="Good morning! Here's your daily briefing for today.",
        tool_calls=None,
    )

    # Define rule for handling tool result
    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) >= 2:
            # Check if previous message has tool calls and current has tool results
            for msg in messages[-2:]:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    return True
        return False

    tool_result_response = MockLLMOutput(
        content="The daily briefing has been scheduled successfully at 6am Sydney time every day.",
        tool_calls=None,
    )

    # Create mock LLM
    llm_client = RuleBasedMockLLMClient(
        rules=[
            (schedule_recurring_matcher, schedule_response),
            (tool_result_matcher, tool_result_response),
            (callback_trigger_matcher, callback_response),
        ],
        default_response=MockLLMOutput(content="Default mock response."),
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

    test_service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt"},
        calendar_config={},
        timezone_str="Australia/Sydney",  # Sydney timezone
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="test_recurring_timezone",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config,
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_message_id"

    # Create task worker
    test_new_task_event = asyncio.Event()
    test_shutdown_event = asyncio.Event()

    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        calendar_config={},
        timezone_str="Australia/Sydney",  # Sydney timezone
        embedding_generator=AsyncMock(),
        clock=mock_clock,
        shutdown_event_instance=test_shutdown_event,
        engine=test_db_engine,
    )
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)

    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),
        name=f"TaskWorker-RecurringTimezone-{test_run_id}",
    )
    await asyncio.sleep(0.01)  # Allow worker to start

    # Part 1: Schedule the recurring task
    logger.info("--- Part 1: Scheduling recurring task ---")
    async with DatabaseContext(engine=test_db_engine) as db_context:
        _, _, _, error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface,
            interface_type="test",
            conversation_id=test_chat_id,
            trigger_content_parts=[
                {
                    "type": "text",
                    "text": "Please schedule daily briefing at 6am Sydney time",
                }
            ],
            trigger_interface_message_id="msg_001",
            user_name=test_user_name,
        )
    assert error is None, f"Error scheduling recurring task: {error}"

    # Give time for the task to be written to database
    await asyncio.sleep(0.1)

    # Verify the task is scheduled correctly
    async with DatabaseContext(engine=test_db_engine) as db_context:
        stmt = select(
            tasks_table.c.task_id,
            tasks_table.c.scheduled_at,
            tasks_table.c.recurrence_rule,
            tasks_table.c.original_task_id,
        ).where(
            tasks_table.c.task_type == "llm_callback", tasks_table.c.status == "pending"
        )
        result = await db_context.fetch_one(stmt)

    assert result is not None, "No pending task found"
    first_scheduled_at = result["scheduled_at"]
    if first_scheduled_at.tzinfo is None:
        first_scheduled_at = first_scheduled_at.replace(tzinfo=timezone.utc)

    # Verify it's scheduled for 6am Sydney time (should be 20:00 UTC previous day)
    first_scheduled_sydney = first_scheduled_at.astimezone(sydney_tz)
    assert first_scheduled_sydney.hour == 6, (
        f"First occurrence not at 6am Sydney: {first_scheduled_sydney}"
    )
    assert first_scheduled_sydney.minute == 0
    logger.info(
        f"First task scheduled correctly at {first_scheduled_at} UTC = {first_scheduled_sydney} Sydney"
    )

    # Part 2: Advance clock to trigger the first occurrence
    logger.info("--- Part 2: Triggering first occurrence ---")
    time_to_advance = (first_scheduled_at - mock_clock.now()) + timedelta(seconds=1)
    mock_clock.advance(time_to_advance)
    test_new_task_event.set()  # Notify worker
    await asyncio.sleep(0.5)  # Allow worker to process

    # Verify the callback was executed
    mock_chat_interface.send_message.assert_called_once()
    call_kwargs = mock_chat_interface.send_message.call_args[1]
    assert "daily briefing" in call_kwargs.get("text", "").lower()

    # Part 3: Verify the next occurrence is scheduled correctly
    logger.info("--- Part 3: Verifying next occurrence ---")
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Look for the new task (should have the recurring pattern in its ID)
        stmt = select(
            tasks_table.c.task_id,
            tasks_table.c.scheduled_at,
            tasks_table.c.recurrence_rule,
        ).where(
            tasks_table.c.task_type == "llm_callback",
            tasks_table.c.status == "pending",
            tasks_table.c.task_id.like("%recur_%"),
        )
        result = await db_context.fetch_one(stmt)

    assert result is not None, "No next recurring task found"
    next_scheduled_at = result["scheduled_at"]
    if next_scheduled_at.tzinfo is None:
        next_scheduled_at = next_scheduled_at.replace(tzinfo=timezone.utc)

    # Verify it's scheduled for 6am Sydney time the next day
    next_scheduled_sydney = next_scheduled_at.astimezone(sydney_tz)
    assert next_scheduled_sydney.hour == 6, (
        f"Next occurrence not at 6am Sydney: {next_scheduled_sydney}"
    )
    assert next_scheduled_sydney.minute == 0

    # Should be exactly 1 day after the first occurrence
    time_diff = next_scheduled_at - first_scheduled_at
    assert abs(time_diff.total_seconds() - 86400) < 60, (
        f"Next occurrence not 24 hours later: {time_diff}"
    )

    logger.info(
        f"Next task scheduled correctly at {next_scheduled_at} UTC = {next_scheduled_sydney} Sydney"
    )

    # Cleanup
    logger.info("--- Cleanup ---")
    test_shutdown_event.set()  # Signal worker to stop
    test_new_task_event.set()  # Wake up worker if it's waiting
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info("TaskWorker finished.")
    except asyncio.TimeoutError:
        logger.warning("TaskWorker did not finish within timeout. Cancelling.")
        worker_task.cancel()
        await asyncio.sleep(0.1)  # Allow cancellation to process
    except asyncio.CancelledError:
        logger.info("TaskWorker was cancelled.")
    finally:
        # Ensure the events are cleared
        test_shutdown_event.clear()

    logger.info(f"--- Recurring Task Timezone Test ({test_run_id}) Passed ---")
