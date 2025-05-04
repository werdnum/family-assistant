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
from family_assistant.task_worker import (
    TaskWorker,
    shutdown_event,
    new_task_event,
    handle_llm_callback,
)
from family_assistant import storage  # For direct task checking

from tests.helpers import wait_for_tasks_to_complete
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
CALLBACK_DELAY_SECONDS = 3  # Schedule callback this many seconds into the future
WAIT_BUFFER_SECONDS = 10  # Wait this much longer than the delay for processing
CALLBACK_CONTEXT = "Remind me to check the test results"


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
            system_message is not None
            and "processing a scheduled callback" in system_message.get("content", "")
            and user_message is not None
            and "System Callback Trigger:" in user_message.get("content", "")
            and CALLBACK_CONTEXT in user_message.get("content", "")
        )

    callback_final_response_text = (
        f"Rule-based mock: OK, executing callback. Reminder: {CALLBACK_CONTEXT}"
    )
    callback_response = LLMOutput(
        content=callback_final_response_text,
        tool_calls=None,  # No tool call expected for the callback response itself
    )
    callback_rule: Rule = (callback_trigger_matcher, callback_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[schedule_rule, callback_rule],
        default_response=LLMOutput(content="Default mock response for callback test."),
    )
    logger.info(f"Using RuleBasedMockLLMClient for callback test.")

    # --- Instantiate Dependencies ---
    # Tool Providers (using real local tools)
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(  # Mock MCP
        mcp_server_configs={}  # Use correct argument name
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()  # Eagerly fetch

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
        server_url=None,  # Added missing argument
    )

    # Mock Telegram Application and Bot
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=9999)
    )  # Mock sent message ID

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

    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),  # Pass the test event
        name=f"TaskWorker-{test_run_id}",
    )

    # --- Part 1: Schedule the callback ---
    logger.info("--- Part 1: Scheduling Callback ---")
    schedule_request_text = (
        f"Please schedule a reminder with context: {CALLBACK_CONTEXT}"
    )
    schedule_request_trigger = [{"type": "text", "text": schedule_request_text}]

    scheduled_task_id = None
    # Removed patch context manager
    async with DatabaseContext(engine=test_db_engine) as db_context:
        schedule_response_content, schedule_tool_info, _, _ = (
            await processing_service.generate_llm_response_for_chat(
                db_context=db_context, # Renamed db_context
                application=mock_application, # Pass mock application
                interface_type="test",
                conversation_id=str(TEST_CHAT_ID), # Added conversation ID as string
                trigger_content_parts=schedule_request_trigger,
                user_name=TEST_USER_NAME,
            )
        )

    logger.info(f"Schedule Request - Mock LLM Response: {schedule_response_content}")

    # --- Part 2: Run Worker and Wait for Callback ---
    logger.info("--- Part 2: Waiting for task completion ---")

    wait_time = CALLBACK_DELAY_SECONDS + WAIT_BUFFER_SECONDS
    await wait_for_tasks_to_complete(engine=test_db_engine, timeout_seconds=wait_time)

    # --- Part 3: Verify Callback Execution ---
    logger.info("--- Part 3: Verifying Callback Execution ---")

    # Assertion 4 (Renumbered to 2): Check if mock bot's send_message was called with the final response
    logger.info("Checking if mock_bot.send_message was called...")
    mock_bot.send_message.assert_called_once()
    call_args, call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.get("chat_id") == TEST_CHAT_ID
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    # Check if the *mock's* expected response content is in the sent text
    # Note: handle_llm_callback formats the response, so we check the raw content from the mock rule
    assert (
        CALLBACK_CONTEXT in sent_text
    ), f"Final message sent by bot did not contain expected mock response. Sent: '{sent_text}' Expected fragment: '{CALLBACK_CONTEXT}'"
    logger.info(
        "Verified mock_bot.send_message was called with the expected final response."
    )

    # --- Cleanup ---
    logger.info("--- Cleanup ---")
    logger.info("Signalling TaskWorker to shut down...")
    test_shutdown_event.set()  # Signal worker loop to stop
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info("TaskWorker task finished.")
    except asyncio.TimeoutError:
        logger.warning("TaskWorker task did not finish within timeout during cleanup.")
        worker_task.cancel()  # Force cancel if it didn't stop
        await asyncio.sleep(0.1)  # Allow cancellation to process

    logger.info(f"--- Callback Test ({test_run_id}) Passed ---")
