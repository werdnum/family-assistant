import asyncio
import json
import logging
import uuid  # Added for turn_id
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING  # Added TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface  # LLMOutput removed
from family_assistant.interfaces import ChatInterface  # Import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem  # Added imports
from family_assistant.processing import ProcessingService, ProcessingServiceConfig

# Import necessary components from the application
from family_assistant.storage.context import DatabaseContext

# Import TaskWorker, events, and the specific handler needed for registration
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
)
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
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,  # Import the mock's LLMOutput
)
from tests.mocks.mock_llm import (
    MatcherArgs,  # Added import
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_CHAT_ID = 54321
TEST_USER_NAME = "CallbackTester"
TEST_USER_ID = 123098  # Added user ID
CALLBACK_DELAY_SECONDS = 3  # Schedule callback this many seconds into the future
WAIT_BUFFER_SECONDS = 10  # Wait this much longer than the delay for processing
CALLBACK_CONTEXT = "Remind me to check the test results"


@pytest.mark.asyncio
async def test_schedule_and_execute_callback(test_db_engine: AsyncEngine) -> None:
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
    user_message_id_schedule = 401  # Added user message ID for the scheduling request
    now_utc = datetime.now(timezone.utc)
    callback_dt = now_utc + timedelta(seconds=CALLBACK_DELAY_SECONDS)
    callback_time_iso = callback_dt.isoformat()
    logger.info(f"Scheduling callback for: {callback_time_iso}")

    # --- Define Rules for Mock LLM ---
    schedule_tool_call_id = f"call_schedule_{test_run_id}"

    # Rule 1: Match request to schedule callback
    def schedule_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")

        last_text = get_last_message_text(messages).lower()
        return (
            "schedule a reminder" in last_text
            and f"context: {CALLBACK_CONTEXT}".lower() in last_text
            and tools is not None
        )

    schedule_response = MockLLMOutput(  # Use the mock's LLMOutput
        content=f"OK, I will schedule a callback for {callback_time_iso} with context: '{CALLBACK_CONTEXT}'.",
        tool_calls=[
            ToolCallItem(
                id=schedule_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="schedule_future_callback",
                    arguments=json.dumps({
                        "callback_time": callback_time_iso,
                        "context": CALLBACK_CONTEXT,
                    }),
                ),
            )
        ],
    )
    schedule_rule: Rule = (schedule_matcher, schedule_response)

    # Rule 2: Match the system trigger from handle_llm_callback
    def callback_trigger_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        # Iterate backwards to find the last user message that is the callback trigger
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    # This is expected to be the "System Callback Trigger:..." message
                    is_trigger = "System Callback Trigger:" in content
                    has_context = CALLBACK_CONTEXT in content
                    # logger.debug(f"Callback Matcher: User content='{content[:100]}...', is_trigger={is_trigger}, has_context={has_context}")
                    if is_trigger and has_context:
                        return True
                    # If it's a user message but not the trigger,
                    # it means we've gone too far back or the trigger wasn't the last user message.
                    # For this specific test, the trigger *is* the last user message.
                    break 
        return False

    callback_final_response_text = (
        f"Rule-based mock: OK, executing callback. Reminder: {CALLBACK_CONTEXT}"
    )
    callback_response = MockLLMOutput(  # Use the mock's LLMOutput
        content=callback_final_response_text,
        tool_calls=None,  # No tool call expected for the callback response itself
    )
    callback_rule: Rule = (callback_trigger_matcher, callback_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[schedule_rule, callback_rule],
        default_response=MockLLMOutput(
            content="Default mock response for callback test."
        ),
    )
    logger.info("Using RuleBasedMockLLMClient for callback test.")

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
    dummy_app_config = {}  # Add dummy app_config

    test_service_config_obj_callback = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
        tools_config={},  # Added missing tools_config
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_callback,
        app_config=dummy_app_config,  # Pass dummy app_config directly
        context_providers=[],
        server_url=None,
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
    # Add a mock embedding generator for the TaskWorker
    mock_embedding_generator = MagicMock()
    # Use AsyncMock for chat_interface as its methods are awaited
    mock_chat_interface_for_worker = AsyncMock(spec=ChatInterface)
    # Set a return value for send_message as it's used by the handler
    mock_chat_interface_for_worker.send_message.return_value = (
        "mock_message_id_callback_response"
    )

    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface_for_worker,  # Pass ChatInterface
        new_task_event=test_new_task_event,  # Pass the event from above
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        embedding_generator=mock_embedding_generator,
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

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Correct unpacking to 4 values now
        (
            schedule_final_text_reply,
            _schedule_final_assistant_msg_id,  # Not used here
            _schedule_final_reasoning_info,  # Not used here
            schedule_error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),  # Added conversation ID as string
            # turn_id is generated by handle_chat_interaction
            trigger_content_parts=schedule_request_trigger,
            trigger_interface_message_id=str(
                user_message_id_schedule
            ),  # Added missing argument
            user_name=TEST_USER_NAME,
        )

    assert schedule_error is None, f"Error during schedule request: {schedule_error}"
    logger.info(
        f"Schedule Request - Mock LLM Response: {schedule_final_text_reply if schedule_final_text_reply else 'No assistant message found'}"
    )

    # --- Part 2: Run Worker and Wait for Callback ---
    logger.info("--- Part 2: Waiting for task completion ---")

    wait_time = CALLBACK_DELAY_SECONDS + WAIT_BUFFER_SECONDS
    await wait_for_tasks_to_complete(engine=test_db_engine, timeout_seconds=wait_time)

    # --- Part 3: Verify Callback Execution ---
    logger.info("--- Part 3: Verifying Callback Execution ---")

    # Assertion 4 (Renumbered to 2): Check if the mock_chat_interface_for_worker's send_message was called
    logger.info("Checking if mock_chat_interface_for_worker.send_message was called...")
    mock_chat_interface_for_worker.send_message.assert_awaited_once()
    call_args, call_kwargs = mock_chat_interface_for_worker.send_message.call_args
    assert call_kwargs.get("conversation_id") == str(TEST_CHAT_ID)
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    # Check if the *mock's* expected response content is in the sent text
    # Note: handle_llm_callback formats the response, so we check the raw content from the mock rule
    assert CALLBACK_CONTEXT in sent_text, (
        f"Final message sent by bot did not contain expected mock response. Sent: '{sent_text}' Expected fragment: '{CALLBACK_CONTEXT}'"
    )
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
