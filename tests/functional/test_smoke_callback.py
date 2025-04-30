import pytest
import uuid
import asyncio
import logging
import json
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from typing import List, Dict, Any, Optional, Callable, Tuple
from unittest.mock import MagicMock, AsyncMock, patch

# Import necessary components from the application
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.processing import ProcessingService
from family_assistant.llm import LLMInterface, LLMOutput
from family_assistant.tools import (
    LocalToolsProvider,
    MCPToolsProvider,
    CompositeToolsProvider,
    TOOLS_DEFINITION as local_tools_definition,
    AVAILABLE_FUNCTIONS as local_tool_implementations,
    ToolExecutionContext,
)
# Import TaskWorker, events, and the specific handler needed for registration
from family_assistant.task_worker import TaskWorker, shutdown_event, new_task_event, handle_llm_callback
from family_assistant import storage # For direct task checking

# Import the rule-based mock LLM
from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    Rule,
    MatcherFunction,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_CHAT_ID = 54321
TEST_USER_NAME = "CallbackTester"
CALLBACK_DELAY_SECONDS = 3 # Schedule callback this many seconds into the future
WAIT_BUFFER_SECONDS = 2 # Wait this much longer than the delay for processing
CALLBACK_CONTEXT = "Remind me to check the test results."


@pytest.mark.asyncio
async def test_schedule_and_execute_callback(test_db_engine):
    """
    Tests the full flow:
    1. User asks to schedule a callback.
    2. Mock LLM calls schedule_future_callback tool.
    3. Verify task is created in DB.
    4. TaskWorker picks up and executes the task after the delay.
    5. handle_llm_callback triggers ProcessingService again.
    6. Mock LLM provides the final response based on the callback context.
    7. Verify the final response is sent via mock bot and task/history are updated.
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Callback Test ({test_run_id}) ---")

    # --- Calculate future callback time ---
    now_utc = datetime.now(timezone.utc)
    callback_dt = now_utc + timedelta(seconds=CALLBACK_DELAY_SECONDS)
    callback_time_iso = callback_dt.isoformat()
    logger.info(f"Scheduling callback for: {callback_time_iso}")

    # --- Define Rules for Mock LLM ---
    schedule_tool_call_id = f"call_schedule_{test_run_id}"

    # Rule 1: Match request to schedule callback
    def schedule_matcher(messages, tools, tool_choice):
        last_text = get_last_message_text(messages).lower()
        return (
            "schedule a reminder" in last_text
            and f"context: {CALLBACK_CONTEXT}".lower() in last_text
            and tools is not None
        )

    schedule_response = LLMOutput(
        content=f"OK, I will schedule a callback for {callback_time_iso} with context: '{CALLBACK_CONTEXT}'.",
        tool_calls=[
            {
                "id": schedule_tool_call_id,
                "type": "function",
                "function": {
                    "name": "schedule_future_callback",
                    "arguments": json.dumps(
                        {
                            "callback_time": callback_time_iso,
                            "context": CALLBACK_CONTEXT,
                        }
                    ),
                },
            }
        ],
    )
    schedule_rule: Rule = (schedule_matcher, schedule_response)

    # Rule 2: Match the system trigger from handle_llm_callback
    def callback_trigger_matcher(messages, tools, tool_choice):
        # Check system message content
        system_message = next((m for m in messages if m.get("role") == "system"), None)
        user_message = next((m for m in messages if m.get("role") == "user"), None)

        return (
            system_message is not None and "processing a scheduled callback" in system_message.get("content", "")
            and user_message is not None and "System Callback Trigger:" in user_message.get("content", "")
            and CALLBACK_CONTEXT in user_message.get("content", "")
        )

    callback_final_response_text = f"Rule-based mock: OK, executing callback. Reminder: {CALLBACK_CONTEXT}"
    callback_response = LLMOutput(
        content=callback_final_response_text,
        tool_calls=None, # No tool call expected for the callback response itself
    )
    callback_rule: Rule = (callback_trigger_matcher, callback_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[schedule_rule, callback_rule],
        default_response=LLMOutput(content="Default mock response for callback test.")
    )
    logger.info(f"Using RuleBasedMockLLMClient for callback test.")

    # --- Instantiate Dependencies ---
    # Tool Providers (using real local tools)
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider( # Mock MCP
        mcp_definitions=[], mcp_sessions={}, tool_name_to_server_id={}
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions() # Eagerly fetch

    # Processing Service
    dummy_prompts = {"system_prompt": "Test system prompt for callback."}
    dummy_calendar_config = {}
    dummy_timezone_str = "UTC"
    dummy_max_history = 5
    dummy_history_age = 24

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
    )

    # Mock Telegram Application and Bot
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=9999)) # Mock sent message ID

    mock_application = MagicMock()
    mock_application.bot = mock_bot
    # Add other attributes if needed by tools/handlers (e.g., job_queue if used directly)

    # Task Worker Events (use fresh events for each test run)
    test_shutdown_event = asyncio.Event()
    test_new_task_event = asyncio.Event()

    # Instantiate Task Worker
    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        application=mock_application,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
    )
    # Register the necessary handler for this test
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)

    # --- Patch storage.enqueue_task to notify our test event ---
    # This ensures the worker wakes up immediately if the task is ready
    original_enqueue_task = storage.enqueue_task
    async def patched_enqueue_task(*args, **kwargs):
        # Call the original function first
        result = await original_enqueue_task(*args, **kwargs)
        # If a notify_event kwarg wasn't explicitly passed *or* if it was our event,
        # set our test event. This is a bit broad but ensures notification.
        # A more precise patch could check scheduled_at vs now.
        if 'notify_event' not in kwargs or kwargs['notify_event'] is test_new_task_event:
             logger.debug(f"Patched enqueue_task: Notifying test_new_task_event for task {kwargs.get('task_id')}")
             test_new_task_event.set()
        return result

    # --- Part 1: Schedule the callback ---
    logger.info("--- Part 1: Scheduling Callback ---")
    schedule_request_text = f"Please schedule a reminder with context: {CALLBACK_CONTEXT}"
    schedule_request_trigger = [{"type": "text", "text": schedule_request_text}]

    scheduled_task_id = None
    with patch('family_assistant.storage.enqueue_task', new=patched_enqueue_task):
        async with DatabaseContext(engine=test_db_engine) as db_context:
            schedule_response_content, schedule_tool_info, _, _ = (
                await processing_service.generate_llm_response_for_chat(
                    db_context=db_context,
                    application=mock_application, # Pass mock application
                    chat_id=TEST_CHAT_ID,
                    trigger_content_parts=schedule_request_trigger,
                    user_name=TEST_USER_NAME,
                )
            )

    logger.info(f"Schedule Request - Mock LLM Response: {schedule_response_content}")
    logger.info(f"Schedule Request - Tool Info: {schedule_tool_info}")

    # Assertion 1: Check tool call info
    assert schedule_tool_info is not None
    assert len(schedule_tool_info) == 1
    assert schedule_tool_info[0]["function_name"] == "schedule_future_callback"
    assert schedule_tool_info[0]["tool_call_id"] == schedule_tool_call_id
    assert schedule_tool_info[0]["arguments"]["callback_time"] == callback_time_iso
    assert schedule_tool_info[0]["arguments"]["context"] == CALLBACK_CONTEXT
    assert "Error:" not in schedule_tool_info[0].get("response_content", "")

    # Assertion 2: Check database for the scheduled task
    task_in_db = None
    logger.info("Checking database for the scheduled task...")
    async with test_db_engine.connect() as connection:
        # Find task by type and payload content (since ID is random)
        result = await connection.execute(
            text("""
                SELECT task_id, task_type, payload, scheduled_at, status
                FROM tasks
                WHERE task_type = :task_type
                  AND json_extract(payload, '$.callback_context') = :context
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"task_type": "llm_callback", "context": CALLBACK_CONTEXT},
        )
        task_in_db = result.fetchone()

    assert task_in_db is not None, "Scheduled task 'llm_callback' not found in DB or not pending."
    scheduled_task_id = task_in_db.task_id
    logger.info(f"Found scheduled task in DB with ID: {scheduled_task_id}")
    assert task_in_db.status == "pending"
    # Check scheduled time (allow for minor precision differences)
    db_scheduled_at = task_in_db.scheduled_at.astimezone(timezone.utc)
    time_diff = abs((db_scheduled_at - callback_dt).total_seconds())
    assert time_diff < 1, f"Scheduled time in DB ({db_scheduled_at}) differs significantly from expected ({callback_dt}). Diff: {time_diff}s"
    logger.info(f"Verified task {scheduled_task_id} is pending in DB with correct context and time.")

    # --- Part 2: Run Worker and Wait for Callback ---
    logger.info("--- Part 2: Running Task Worker and Waiting ---")
    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event), # Pass the test event
        name=f"TaskWorker-{test_run_id}"
    )
    # Give the worker a moment to start up
    await asyncio.sleep(0.2)

    wait_time = CALLBACK_DELAY_SECONDS + WAIT_BUFFER_SECONDS
    logger.info(f"Waiting for {wait_time:.1f} seconds for callback to execute...")
    await asyncio.sleep(wait_time)

    # --- Part 3: Verify Callback Execution ---
    logger.info("--- Part 3: Verifying Callback Execution ---")

    # Assertion 3: Check task status in DB
    logger.info(f"Checking status of task {scheduled_task_id} in DB...")
    task_status = None
    async with test_db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT status FROM tasks WHERE task_id = :task_id"),
            {"task_id": scheduled_task_id},
        )
        row = result.fetchone()
        if row:
            task_status = row.status

    assert task_status == "done", f"Task {scheduled_task_id} status is '{task_status}', expected 'done'."
    logger.info(f"Verified task {scheduled_task_id} status is 'done'.")

    # Assertion 4: Check if mock bot's send_message was called with the final response
    logger.info("Checking if mock_bot.send_message was called...")
    mock_bot.send_message.assert_called_once()
    call_args, call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.get("chat_id") == TEST_CHAT_ID
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    # Check if the *mock's* expected response content is in the sent text
    # Note: handle_llm_callback formats the response, so we check the raw content from the mock rule
    assert callback_final_response_text in sent_text, \
        f"Final message sent by bot did not contain expected mock response. Sent: '{sent_text}' Expected fragment: '{callback_final_response_text}'"
    logger.info("Verified mock_bot.send_message was called with the expected final response.")

    # Assertion 5: Check message history (optional but good)
    logger.info("Checking message history for trigger and response...")
    history_entries = []
    async with test_db_engine.connect() as connection:
         result = await connection.execute(
             text("""
                 SELECT role, content
                 FROM message_history
                 WHERE chat_id = :chat_id
                 ORDER BY timestamp DESC
                 LIMIT 2
             """),
             {"chat_id": TEST_CHAT_ID},
         )
         history_entries = result.fetchall()

    assert len(history_entries) >= 2, "Expected at least 2 history entries (trigger and response)"
    # Check the last two entries (order might vary slightly depending on insertion timing)
    roles = {entry.role for entry in history_entries}
    contents = {entry.content for entry in history_entries}
    assert "assistant" in roles, "Assistant response missing from history"
    assert "system" in roles, "System trigger message missing from history" # handle_llm_callback logs trigger as system
    assert any(callback_final_response_text in content for content in contents if content), "Final response content not found in history"
    assert any("System Callback Trigger:" in content for content in contents if content), "System trigger text not found in history"
    logger.info("Verified message history contains callback trigger and response.")


    # --- Cleanup ---
    logger.info("--- Cleanup ---")
    logger.info("Signalling TaskWorker to shut down...")
    test_shutdown_event.set() # Signal worker loop to stop
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info("TaskWorker task finished.")
    except asyncio.TimeoutError:
        logger.warning("TaskWorker task did not finish within timeout during cleanup.")
        worker_task.cancel() # Force cancel if it didn't stop
        await asyncio.sleep(0.1) # Allow cancellation to process

    logger.info(f"--- Callback Test ({test_run_id}) Passed ---")
