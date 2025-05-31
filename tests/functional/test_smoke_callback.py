import asyncio
import json
import logging
import uuid  # Added for turn_id
from datetime import timedelta, timezone
from typing import TYPE_CHECKING  # Added TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface  # LLMOutput removed
# Import select for direct DB queries
from sqlalchemy.sql import select  # Import text

# Import necessary components from the application
from family_assistant import storage  # Import storage module
from family_assistant.interfaces import ChatInterface  # Import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem  # Added imports
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext

# Import tasks_table for querying
from family_assistant.storage.tasks import tasks_table

# Import TaskWorker, events, and the specific handler needed for registration
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
)
from family_assistant.task_worker import (
    shutdown_event as global_task_worker_shutdown_event,  # Import global shutdown event
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
from family_assistant.utils.clock import MockClock
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

    # --- Setup Mock Clock ---
    mock_clock = MockClock()
    initial_time = mock_clock.now()  # Use clock's initial time

    # --- Calculate future callback time ---
    user_message_id_schedule = 401  # Added user message ID for the scheduling request
    callback_dt = initial_time + timedelta(seconds=CALLBACK_DELAY_SECONDS)
    callback_time_iso = callback_dt.isoformat()
    logger.info(
        f"Scheduling callback for: {callback_time_iso} (current mock time: {initial_time.isoformat()})"
    )

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
        if not messages:
            return False

        # The "System Callback Trigger" is added as the last user message by handle_chat_interaction
        # when invoked by handle_llm_callback.
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, str):
                is_trigger = "System Callback Trigger:" in content
                has_context = CALLBACK_CONTEXT in content
                return is_trigger and has_context
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
        delegation_security_level="confirm",  # Added
        id="smoke_callback_profile",  # Added
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
        clock=mock_clock,  # Inject mock_clock
    )
    # Register the necessary handler for this test
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)

    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),  # Pass the test event
        name=f"TaskWorker-{test_run_id}",
    )
    # Allow worker to start up and potentially process initial tasks if any (though none expected here)
    await asyncio.sleep(0.01)

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

    # --- Part 2: Advance time and let worker process the callback ---
    logger.info(
        f"--- Part 2: Advancing clock by {CALLBACK_DELAY_SECONDS + 1} seconds and letting worker run ---"
    )
    mock_clock.advance(
        timedelta(seconds=CALLBACK_DELAY_SECONDS + 1)
    )  # Advance past callback time
    test_new_task_event.set()  # Notify worker to check for tasks
    await asyncio.sleep(0.1)  # Allow worker to process the task

    # --- Part 3: Verify Callback Execution ---
    logger.info("--- Part 3: Verifying Callback Execution ---")

    # Assertion: Check if the mock_chat_interface_for_worker's send_message was called
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


@pytest.mark.asyncio
async def test_modify_pending_callback(test_db_engine: AsyncEngine) -> None:
    """
    Tests modifying a scheduled callback:
    1. User asks to schedule a callback.
    2. Mock LLM calls schedule_future_callback tool.
    3. Verify task is created.
    4. User asks to modify the callback (time and context).
    5. Mock LLM calls modify_pending_callback tool.
    6. Verify task is updated in DB.
    7. TaskWorker picks up and executes the MODIFIED task.
    8. Mock LLM provides final response based on MODIFIED callback context.
    9. Verify final response and task/history.
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Modify Callback Test ({test_run_id}) ---")

    user_message_id_schedule = 501
    user_message_id_modify = 502

    mock_clock = MockClock()
    initial_time = mock_clock.now()

    initial_callback_delay_seconds = 5
    initial_callback_dt = initial_time + timedelta(
        seconds=initial_callback_delay_seconds
    )
    initial_callback_time_iso = initial_callback_dt.isoformat()
    initial_context = "Initial context for modification test"

    modified_callback_delay_seconds = (
        initial_callback_delay_seconds
        + 10  # Ensure it's later than initial, relative to initial_time
    )
    modified_callback_dt = initial_time + timedelta(
        seconds=modified_callback_delay_seconds
    )
    modified_callback_time_iso = modified_callback_dt.isoformat()
    modified_context = "This is the MODIFIED context"

    schedule_tool_call_id = f"call_schedule_modify_test_{test_run_id}"
    # This will be the task_id generated by schedule_future_callback
    # We need to capture it to use in the modify_pending_callback tool call.
    # For the test, we'll predict it based on the tool's logic or extract from DB.
    # Let's assume we can predict it for the mock rule, or make the rule more flexible.
    # For simplicity, the mock rule for modify will accept any task_id for now.
    # In a real scenario, the LLM would get this from list_pending_callbacks.
    # We will fetch it from the DB after scheduling for the actual tool call.
    scheduled_task_id_placeholder = "task_id_to_be_modified"  # Placeholder for rule
    modify_tool_call_id = f"call_modify_test_{test_run_id}"

    # --- Define Rules for Mock LLM ---
    # Rule 1: Schedule initial callback
    def schedule_matcher_for_modify(kwargs: MatcherArgs) -> bool:
        return (
            "schedule initial for modify"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    schedule_response_for_modify = MockLLMOutput(
        content=f"OK, scheduling initial callback for {initial_callback_time_iso}.",
        tool_calls=[
            ToolCallItem(
                id=schedule_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="schedule_future_callback",
                    arguments=json.dumps({
                        "callback_time": initial_callback_time_iso,
                        "context": initial_context,
                    }),
                ),
            )
        ],
    )

    # Rule 2: Modify the callback
    def modify_matcher(kwargs: MatcherArgs) -> bool:
        return (
            "modify the callback"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    modify_response = MockLLMOutput(
        content=f"OK, attempting to modify callback to {modified_callback_time_iso} with new context.",
        tool_calls=[
            ToolCallItem(
                id=modify_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="modify_pending_callback",
                    arguments=json.dumps({
                        "task_id": scheduled_task_id_placeholder,  # LLM would provide actual ID
                        "new_callback_time": modified_callback_time_iso,
                        "new_context": modified_context,
                    }),
                ),
            )
        ],
    )

    # Rule 3: System trigger for the MODIFIED callback
    def modified_callback_trigger_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, str):
                return (
                    "System Callback Trigger:" in content
                    and modified_context in content
                )
        return False

    modified_callback_final_response_text = (
        f"Rule-based mock: Executing MODIFIED callback. Reminder: {modified_context}"
    )
    modified_callback_response = MockLLMOutput(
        content=modified_callback_final_response_text
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (schedule_matcher_for_modify, schedule_response_for_modify),
            (modify_matcher, modify_response),
            (modified_callback_trigger_matcher, modified_callback_response),
        ],
        default_response=MockLLMOutput(
            content="Default mock response for modify test."
        ),
    )

    # --- Instantiate Dependencies (similar to the first test) ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    dummy_prompts = {"system_prompt": "Test system prompt for modify."}
    test_service_config_obj_modify = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="smoke_modify_profile",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_modify,
        app_config={},
        context_providers=[],
        server_url=None,
    )

    mock_chat_interface_for_worker = AsyncMock(spec=ChatInterface)
    mock_chat_interface_for_worker.send_message.return_value = (
        "mock_message_id_modified_callback"
    )

    test_new_task_event = asyncio.Event()
    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface_for_worker,
        new_task_event=test_new_task_event,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,  # Inject mock_clock
    )
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)
    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),
        name=f"TaskWorker-Modify-{test_run_id}",
    )
    await asyncio.sleep(0.01)  # Allow worker to start

    # --- Part 1: Schedule the initial callback ---
    logger.info("--- Part 1: Scheduling initial callback for modification test ---")
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # mock_clock.now() is used by schedule_future_callback_tool via exec_context
        _resp, _, _, schedule_error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": "Please schedule initial for modify"}
            ],
            trigger_interface_message_id=str(user_message_id_schedule),
            user_name=TEST_USER_NAME,
        )
    assert schedule_error is None, (
        f"Error scheduling initial callback: {schedule_error}"
    )

    # Find the scheduled task_id
    scheduled_task_id = None
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Query for the task based on type and context (or part of it)
        # This is a bit fragile; ideally, the schedule tool would return the task_id
        stmt = select(tasks_table.c.task_id, tasks_table.c.payload).where(
            tasks_table.c.task_type == "llm_callback", tasks_table.c.status == "pending"
        )
        all_pending_callbacks = await db_context.fetch_all(stmt)
        for task_row in all_pending_callbacks:
            payload = task_row["payload"]
            if payload and payload.get("callback_context") == initial_context:
                scheduled_task_id = task_row["task_id"]
                break
    assert scheduled_task_id, "Could not find the initially scheduled task in DB"
    logger.info(f"Initial callback scheduled with task_id: {scheduled_task_id}")

    # --- Part 2: Modify the scheduled callback ---
    logger.info(f"--- Part 2: Modifying callback task_id: {scheduled_task_id} ---")
    # Update the placeholder in the LLM rule's arguments for modify_pending_callback
    # This is a bit of a hack for the test; in reality, LLM gets this from user or list_tool
    if modify_response.tool_calls and len(modify_response.tool_calls) > 0:
        original_tool_call_item = modify_response.tool_calls[0]
        new_function_args = json.dumps({
            "task_id": scheduled_task_id,
            "new_callback_time": modified_callback_time_iso,
            "new_context": modified_context,
        })
        new_function = ToolCallFunction(
            name=original_tool_call_item.function.name, arguments=new_function_args
        )
        new_tool_call_item = ToolCallItem(
            id=original_tool_call_item.id,
            type=original_tool_call_item.type,
            function=new_function,
        )
        modify_response.tool_calls = [new_tool_call_item]
    else:
        # This case should not happen based on how modify_response is constructed,
        # but handle defensively.
        logger.error(
            "modify_response.tool_calls is None or empty, cannot update arguments."
        )
        # Optionally, raise an error or handle as appropriate for the test.

    async with DatabaseContext(engine=test_db_engine) as db_context:
        _resp, _, _, modify_error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": "Please modify the callback"}
            ],
            trigger_interface_message_id=str(user_message_id_modify),
            user_name=TEST_USER_NAME,
        )
    assert modify_error is None, f"Error modifying callback: {modify_error}"

    # Verify task is updated in DB
    async with DatabaseContext(engine=test_db_engine) as db_context:
        stmt = select(tasks_table.c.scheduled_at, tasks_table.c.payload).where(
            tasks_table.c.task_id == scheduled_task_id
        )
        modified_task_data = await db_context.fetch_one(stmt)
    assert modified_task_data, f"Modified task {scheduled_task_id} not found in DB"

    # Convert stored scheduled_at (UTC) to the test's modified_callback_dt for comparison
    # Ensure modified_task_data["scheduled_at"] is offset-aware UTC
    db_scheduled_at = modified_task_data["scheduled_at"]
    if db_scheduled_at.tzinfo is None:  # Should be stored as UTC
        db_scheduled_at = db_scheduled_at.replace(tzinfo=timezone.utc)

    assert db_scheduled_at == modified_callback_dt.astimezone(timezone.utc), (
        "Callback time not updated correctly"
    )
    assert modified_task_data["payload"]["callback_context"] == modified_context, (
        "Callback context not updated"
    )
    logger.info(f"Callback task {scheduled_task_id} verified as modified in DB.")

    # --- Part 3: Wait for MODIFIED callback execution ---
    logger.info("--- Part 3: Waiting for MODIFIED task completion ---")
    # Advance clock to just after the modified callback time
    # The modified_callback_dt is absolute, so calculate duration from current mock time
    duration_to_advance = (modified_callback_dt - mock_clock.now()) + timedelta(
        seconds=1
    )
    if duration_to_advance.total_seconds() > 0:
        logger.info(
            f"Advancing clock by {duration_to_advance.total_seconds()}s to trigger modified callback."
        )
        mock_clock.advance(duration_to_advance)
    else:  # If modify tool call took "longer" than the difference, clock might already be past
        logger.info(
            f"Clock already at or past modified callback time. Current: {mock_clock.now()}, Target: {modified_callback_dt}"
        )

    test_new_task_event.set()  # Notify worker
    await asyncio.sleep(0.1)  # Allow worker to process

    # --- Part 4: Verify MODIFIED Callback Execution ---
    logger.info("--- Part 4: Verifying MODIFIED Callback Execution ---")
    mock_chat_interface_for_worker.send_message.assert_awaited_once()
    _call_args, call_kwargs = mock_chat_interface_for_worker.send_message.call_args
    assert call_kwargs.get("conversation_id") == str(TEST_CHAT_ID)
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    assert modified_context in sent_text, (
        f"Final message did not contain MODIFIED context. Sent: '{sent_text}'"
    )
    logger.info(
        "Verified mock_bot.send_message was called with the MODIFIED final response."
    )

    # --- Cleanup ---
    logger.info("--- Cleanup for Modify Test ---")
    global_task_worker_shutdown_event.set()  # Signal worker to stop using global event
    test_new_task_event.set()  # Wake up worker if it's waiting on the event
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info(f"TaskWorker-Modify-{test_run_id} task finished.")
    except asyncio.TimeoutError:
        logger.warning(
            f"TaskWorker-Modify-{test_run_id} task did not finish within timeout. Cancelling."
        )
        worker_task.cancel()
        try:
            await worker_task  # Allow cancellation to propagate
        except asyncio.CancelledError:
            logger.info(f"TaskWorker-Modify-{test_run_id} task was cancelled.")
    except Exception as e:
        logger.error(
            f"Error during TaskWorker-Modify-{test_run_id} cleanup: {e}", exc_info=True
        )
    finally:
        global_task_worker_shutdown_event.clear()  # Ensure global event is reset
    logger.info(f"--- Modify Callback Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_cancel_pending_callback(test_db_engine: AsyncEngine) -> None:
    """
    Tests cancelling a scheduled callback:
    1. User asks to schedule a callback.
    2. Mock LLM calls schedule_future_callback tool.
    3. Verify task is created.
    4. User asks to cancel the callback.
    5. Mock LLM calls cancel_pending_callback tool.
    6. Verify task is marked as 'failed' (or 'cancelled') in DB.
    7. Verify TaskWorker does NOT execute the callback.
    8. Verify no callback message is sent by the bot.
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Cancel Callback Test ({test_run_id}) ---")

    user_message_id_schedule = 601
    user_message_id_cancel = 602

    mock_clock = MockClock()
    initial_time = mock_clock.now()

    initial_callback_delay_seconds = (
        5  # Short delay, it should be cancelled before this
    )
    initial_callback_dt = initial_time + timedelta(
        seconds=initial_callback_delay_seconds
    )
    initial_callback_time_iso = initial_callback_dt.isoformat()
    initial_context_for_cancel = "Initial context for cancellation test"
    scheduled_task_id_placeholder_cancel = "task_id_to_be_cancelled"

    # --- Define Rules for Mock LLM ---
    # Rule 1: Schedule initial callback
    def schedule_matcher_for_cancel(kwargs: MatcherArgs) -> bool:
        return (
            "schedule initial for cancel"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    schedule_response_for_cancel = MockLLMOutput(
        content=f"OK, scheduling initial callback for {initial_callback_time_iso} (cancel test).",
        tool_calls=[
            ToolCallItem(
                id=f"call_schedule_cancel_test_{test_run_id}",
                type="function",
                function=ToolCallFunction(
                    name="schedule_future_callback",
                    arguments=json.dumps({
                        "callback_time": initial_callback_time_iso,
                        "context": initial_context_for_cancel,
                    }),
                ),
            )
        ],
    )

    # Rule 2: Cancel the callback
    def cancel_matcher(kwargs: MatcherArgs) -> bool:
        return (
            "cancel the callback"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    cancel_response = MockLLMOutput(
        content="OK, attempting to cancel callback.",
        tool_calls=[
            ToolCallItem(
                id=f"call_cancel_test_{test_run_id}",
                type="function",
                function=ToolCallFunction(
                    name="cancel_pending_callback",
                    arguments=json.dumps({
                        "task_id": scheduled_task_id_placeholder_cancel
                    }),
                ),
            )
        ],
    )

    # Rule 3: System trigger for the callback (SHOULD NOT BE MATCHED if cancel works)
    def cancelled_callback_trigger_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, str):
                # This matcher should ideally NOT be hit if cancellation is successful
                is_trigger = "System Callback Trigger:" in content
                has_context = initial_context_for_cancel in content
                if is_trigger and has_context:
                    logger.error(
                        "CANCEL TEST FAILURE: Callback trigger was matched, meaning cancellation likely failed."
                    )
                return is_trigger and has_context
        return False

    cancelled_callback_response = MockLLMOutput(
        content="ERROR: This callback should have been cancelled!"
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (schedule_matcher_for_cancel, schedule_response_for_cancel),
            (cancel_matcher, cancel_response),
            (
                cancelled_callback_trigger_matcher,
                cancelled_callback_response,
            ),  # Should not be hit
        ],
        default_response=MockLLMOutput(
            content="Default mock response for cancel test."
        ),
    )

    # --- Instantiate Dependencies (similar to other tests) ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    dummy_prompts = {"system_prompt": "Test system prompt for cancel."}
    test_service_config_obj_cancel = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="smoke_cancel_profile",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_cancel,
        app_config={},
        context_providers=[],
        server_url=None,
    )

    mock_chat_interface_for_worker = AsyncMock(spec=ChatInterface)
    # send_message should NOT be called by the worker if cancellation is successful
    mock_chat_interface_for_worker.send_message.return_value = (
        "mock_message_id_cancelled_callback"
    )

    test_new_task_event = asyncio.Event()
    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface_for_worker,
        new_task_event=test_new_task_event,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,  # Inject mock_clock
    )
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)
    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),
        name=f"TaskWorker-Cancel-{test_run_id}",
    )
    await asyncio.sleep(0.01)  # Allow worker to start

    # --- Part 1: Schedule the initial callback ---
    logger.info("--- Part 1: Scheduling initial callback for cancellation test ---")
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # mock_clock.now() is used by schedule_future_callback_tool via exec_context
        _resp, _, _, schedule_error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": "Please schedule initial for cancel"}
            ],
            trigger_interface_message_id=str(user_message_id_schedule),
            user_name=TEST_USER_NAME,
        )
    assert schedule_error is None, (
        f"Error scheduling initial callback for cancel test: {schedule_error}"
    )

    # Find the scheduled task_id
    scheduled_task_id_for_cancel = None
    async with DatabaseContext(engine=test_db_engine) as db_context:
        stmt = select(tasks_table.c.task_id, tasks_table.c.payload).where(
            tasks_table.c.task_type == "llm_callback", tasks_table.c.status == "pending"
        )
        all_pending_callbacks = await db_context.fetch_all(stmt)
        for task_row in all_pending_callbacks:
            payload = task_row["payload"]
            if (
                payload
                and payload.get("callback_context") == initial_context_for_cancel
            ):
                scheduled_task_id_for_cancel = task_row["task_id"]
                break
    assert scheduled_task_id_for_cancel, (
        "Could not find the initially scheduled task for cancel test in DB"
    )
    logger.info(
        f"Initial callback for cancel test scheduled with task_id: {scheduled_task_id_for_cancel}"
    )

    # --- Part 2: Cancel the scheduled callback ---
    logger.info(
        f"--- Part 2: Cancelling callback task_id: {scheduled_task_id_for_cancel} ---"
    )
    if cancel_response.tool_calls and len(cancel_response.tool_calls) > 0:
        original_tool_call_item = cancel_response.tool_calls[0]
        new_function_args = json.dumps({"task_id": scheduled_task_id_for_cancel})
        new_function = ToolCallFunction(
            name=original_tool_call_item.function.name, arguments=new_function_args
        )
        new_tool_call_item = ToolCallItem(
            id=original_tool_call_item.id,
            type=original_tool_call_item.type,
            function=new_function,
        )
        cancel_response.tool_calls = [new_tool_call_item]
    else:
        logger.error(
            "cancel_response.tool_calls is None or empty, cannot update arguments."
        )
        # Optionally, raise an error or handle as appropriate for the test.

    async with DatabaseContext(engine=test_db_engine) as db_context:
        _resp, _, _, cancel_error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {"type": "text", "text": "Please cancel the callback"}
            ],
            trigger_interface_message_id=str(user_message_id_cancel),
            user_name=TEST_USER_NAME,
        )
    assert cancel_error is None, f"Error cancelling callback: {cancel_error}"

    # Verify task is marked as 'failed' in DB
    async with DatabaseContext(engine=test_db_engine) as db_context:
        stmt = select(tasks_table.c.status, tasks_table.c.error).where(
            tasks_table.c.task_id == scheduled_task_id_for_cancel
        )
        cancelled_task_data = await db_context.fetch_one(stmt)
    assert cancelled_task_data, (
        f"Cancelled task {scheduled_task_id_for_cancel} not found in DB"
    )
    assert cancelled_task_data["status"] == "failed", (
        "Callback status not updated to 'failed' after cancellation"
    )
    assert "cancelled by user" in cancelled_task_data["error"].lower(), (
        "Error message does not indicate user cancellation"
    )
    logger.info(
        f"Callback task {scheduled_task_id_for_cancel} verified as cancelled in DB."
    )

    # --- Part 3: Wait a bit to ensure worker does NOT process it ---
    logger.info("--- Part 3: Waiting to ensure cancelled task is NOT processed ---")
    # Advance clock past the original schedule time
    duration_to_advance_past_schedule = (
        initial_callback_dt - mock_clock.now()
    ) + timedelta(seconds=2)
    if duration_to_advance_past_schedule.total_seconds() > 0:
        mock_clock.advance(duration_to_advance_past_schedule)
    test_new_task_event.set()  # Notify worker
    await asyncio.sleep(
        0.1
    )  # Allow worker to process (it should find nothing or a failed task)

    # --- Part 4: Verify Callback Was NOT Executed ---
    logger.info("--- Part 4: Verifying Cancelled Callback Was NOT Executed ---")
    mock_chat_interface_for_worker.send_message.assert_not_called()
    logger.info(
        "Verified mock_bot.send_message was NOT called, as expected for a cancelled task."
    )

    # Check LLM client calls to ensure the "cancelled_callback_trigger_matcher" was not hit
    # Each user interaction (schedule, then cancel) involves:
    # 1. LLM call for initial processing + tool request
    # 2. LLM call after tool execution for final response
    # So, 2 interactions * 2 LLM calls/interaction = 4 calls total.
    assert len(llm_client.get_calls()) == 4, (  # type: ignore[attr-defined]
        f"LLM was called {len(llm_client.get_calls())} times, expected 4 (schedule, then cancel)"  # type: ignore[attr-defined]
    )

    # --- Cleanup ---
    logger.info("--- Cleanup for Cancel Test ---")
    global_task_worker_shutdown_event.set()  # Signal worker to stop using global event
    test_new_task_event.set()  # Wake up worker if it's waiting on the event
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info(f"TaskWorker-Cancel-{test_run_id} task finished.")
    except asyncio.TimeoutError:
        logger.warning(
            f"TaskWorker-Cancel-{test_run_id} task did not finish within timeout. Cancelling."
        )
        worker_task.cancel()
        try:
            await worker_task  # Allow cancellation to propagate
        except asyncio.CancelledError:
            logger.info(f"TaskWorker-Cancel-{test_run_id} task was cancelled.")
    except Exception as e:
        logger.error(
            f"Error during TaskWorker-Cancel-{test_run_id} cleanup: {e}", exc_info=True
        )
    finally:
        global_task_worker_shutdown_event.clear()  # Ensure global event is reset
    logger.info(f"--- Cancel Callback Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_if_user_responded_flag", [True, False])
async def test_callback_skip_behavior_on_user_response(
    test_db_engine: AsyncEngine, skip_if_user_responded_flag: bool
) -> None:
    """
    Tests the callback skipping behavior based on the `skip_if_user_responded` flag
    when a user message is sent after scheduling but before execution.

    - If skip_if_user_responded_flag is True: Callback should be skipped.
    - If skip_if_user_responded_flag is False: Callback should execute (nag mode).
    """
    test_run_id = uuid.uuid4()
    scenario_name = "SkipCallback" if skip_if_user_responded_flag else "NagCallback"
    logger.info(
        f"\n--- Running {scenario_name} Test ({test_run_id}, skip_flag={skip_if_user_responded_flag}) ---"
    )

    mock_clock = MockClock()
    initial_time = mock_clock.now()
    callback_dt = initial_time + timedelta(seconds=CALLBACK_DELAY_SECONDS)
    callback_time_iso = callback_dt.isoformat()
    user_message_id_schedule = 701
    user_message_id_intervening = 702

    schedule_tool_call_id = f"call_schedule_skip_test_{test_run_id}"

    # Rule 1: Schedule callback
    def schedule_matcher_for_skip_test(kwargs: MatcherArgs) -> bool:
        # Ensure the boolean part of the f-string is also lowercased for comparison
        # with the lowercased message content.
        expected_substring = f"schedule for skip_if_user_responded={str(skip_if_user_responded_flag).lower()}"
        message_content = get_last_message_text(kwargs.get("messages", [])).lower()
        return expected_substring in message_content

    schedule_response_for_skip_test = MockLLMOutput(
        content=f"OK, scheduling callback with skip_flag={skip_if_user_responded_flag}.",
        tool_calls=[
            ToolCallItem(
                id=schedule_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="schedule_future_callback",
                    arguments=json.dumps({
                        "callback_time": callback_time_iso,
                        "context": CALLBACK_CONTEXT,
                        "skip_if_user_responded": skip_if_user_responded_flag,
                    }),
                ),
            )
        ],
    )

    # Rule 2: System trigger for the callback (should only be hit if not skipped)
    def callback_trigger_matcher_for_skip_test(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        last_message = messages[-1]
        if last_message.get("role") == "user":
            content = last_message.get("content")
            if isinstance(content, str):
                return (
                    "System Callback Trigger:" in content
                    and CALLBACK_CONTEXT in content
                )
        return False

    callback_final_response_text_for_skip_test = f"Rule-based mock: Executing callback (skip_flag={skip_if_user_responded_flag}). Reminder: {CALLBACK_CONTEXT}"
    callback_response_for_skip_test = MockLLMOutput(
        content=callback_final_response_text_for_skip_test
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (
                schedule_matcher_for_skip_test,
                schedule_response_for_skip_test,
            ),
            (
                callback_trigger_matcher_for_skip_test,
                callback_response_for_skip_test,
            ),
        ],
        default_response=MockLLMOutput(
            content=f"Default mock response for {scenario_name} test."
        ),
    )

    # --- Instantiate Dependencies (similar to other tests) ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    dummy_prompts = {"system_prompt": f"Test system prompt for {scenario_name}."}
    test_service_config_obj_skip = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id=f"smoke_{scenario_name.lower()}_profile",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_skip,
        app_config={},
        context_providers=[],
        server_url=None,
    )

    mock_chat_interface_for_worker = AsyncMock(spec=ChatInterface)
    mock_chat_interface_for_worker.send_message.return_value = (
        f"mock_message_id_{scenario_name.lower()}_callback"
    )

    test_new_task_event = asyncio.Event()
    task_worker_instance = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface_for_worker,
        new_task_event=test_new_task_event,
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=AsyncMock(),
        clock=mock_clock,  # Inject mock_clock
    )
    task_worker_instance.register_task_handler("llm_callback", handle_llm_callback)
    worker_task = asyncio.create_task(
        task_worker_instance.run(test_new_task_event),
        name=f"TaskWorker-{scenario_name}-{test_run_id}",
    )
    await asyncio.sleep(0.01)  # Allow worker to start

    # --- Part 1: Schedule the callback ---
    logger.info(
        f"--- Part 1: Scheduling callback for {scenario_name} test (skip_flag={skip_if_user_responded_flag}) ---"
    )
    async with DatabaseContext(engine=test_db_engine) as db_context:
        # mock_clock.now() is used by schedule_future_callback_tool via exec_context
        _resp, _, _, schedule_error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface_for_worker,
            new_task_event=test_new_task_event,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            trigger_content_parts=[
                {
                    "type": "text",
                    "text": f"Please schedule for skip_if_user_responded={skip_if_user_responded_flag}",
                }
            ],
            trigger_interface_message_id=str(user_message_id_schedule),
            user_name=TEST_USER_NAME,
        )
    assert schedule_error is None, f"Error scheduling callback: {schedule_error}"
    logger.info(
        f"Callback scheduled for {scenario_name} test. LLM calls: {len(llm_client.get_calls())}"  # type: ignore
    )

    # --- Part 2: Simulate an intervening user message ---
    logger.info(
        f"--- Part 2: Simulating intervening user message for {scenario_name} test ---"
    )
    # Add a message directly to history *after* scheduling_timestamp but *before* callback_dt
    # The scheduling_timestamp is created inside schedule_future_callback_tool.
    # For the test, we know it's roughly `initial_time` (mock_clock.now() at scheduling)
    # when the tool call happens. The callback is scheduled for `callback_dt`.
    # We need to ensure this intervening message's timestamp is > scheduling_timestamp and < callback_dt.
    # Let's place it halfway.
    # The scheduling_timestamp is set by schedule_future_callback_tool using exec_context.clock.now()
    # So, it will be initial_time.
    intervening_message_timestamp = initial_time + timedelta(
        seconds=CALLBACK_DELAY_SECONDS / 2
    )

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # No need for asyncio.sleep(0.01) here as mock_clock controls time precisely.
        # The important part is that intervening_message_timestamp is correctly set relative to
        # the scheduling_timestamp that will be recorded by the tool.
        await storage.add_message_to_history(  # Call storage.add_message_to_history directly
            db_context=db_context,
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            interface_message_id=str(user_message_id_intervening),
            turn_id=str(uuid.uuid4()),  # New turn for this user message
            thread_root_id=None,  # Assuming this is a new interaction for simplicity
            timestamp=intervening_message_timestamp,
            role="user",
            content="This is an intervening user message.",
            tool_calls=None,
            tool_call_id=None,
            reasoning_info=None,
            error_traceback=None,
        )
    logger.info(
        f"Intervening user message added at {intervening_message_timestamp.isoformat()} for {scenario_name} test."
    )

    # --- Part 3: Wait for potential callback execution ---
    logger.info(f"--- Part 3: Waiting for task completion for {scenario_name} test ---")
    # Advance clock past callback time
    duration_to_advance_past_schedule = (callback_dt - mock_clock.now()) + timedelta(
        seconds=1
    )
    if duration_to_advance_past_schedule.total_seconds() > 0:
        mock_clock.advance(duration_to_advance_past_schedule)
    test_new_task_event.set()  # Notify worker
    await asyncio.sleep(0.1)  # Allow worker to process

    # --- Part 4: Verify behavior ---
    logger.info(f"--- Part 4: Verifying behavior for {scenario_name} test ---")
    if skip_if_user_responded_flag:
        # Callback should have been SKIPPED
        mock_chat_interface_for_worker.send_message.assert_not_called()
        logger.info(
            f"Verified: Callback was SKIPPED for {scenario_name} (skip_flag=True), send_message not called."
        )
        # LLM calls: 2 for scheduling. Callback trigger rule should NOT have been hit.
        assert len(llm_client.get_calls()) == 2, (  # type: ignore
            f"LLM calls for {scenario_name} (skip=True) was {len(llm_client.get_calls())}, expected 2."  # type: ignore
        )
    else:
        # Callback should have EXECUTED (Nag mode)
        mock_chat_interface_for_worker.send_message.assert_awaited_once()
        _call_args, call_kwargs = mock_chat_interface_for_worker.send_message.call_args
        assert call_kwargs.get("conversation_id") == str(TEST_CHAT_ID)
        sent_text = call_kwargs.get("text")
        assert sent_text is not None
        assert CALLBACK_CONTEXT in sent_text, (
            f"Final message for {scenario_name} (skip_flag=False) did not contain expected context. Sent: '{sent_text}'"
        )
        logger.info(
            f"Verified: Callback EXECUTED for {scenario_name} (skip_flag=False), send_message called with context."
        )
        # LLM calls:
        # 1. Initial user request -> LLM requests tool call (schedule_future_callback).
        # 2. Tool result for scheduling -> LLM gives final response for the scheduling interaction.
        # 3. Task worker triggers callback -> LLM processes "System Callback Trigger" and gives final response for callback.
        assert len(llm_client.get_calls()) == 3, (  # type: ignore
            f"LLM calls for {scenario_name} (skip=False) was {len(llm_client.get_calls())}, expected 3."  # type: ignore
        )

    # --- Cleanup ---
    logger.info(f"--- Cleanup for {scenario_name} Test ---")
    global_task_worker_shutdown_event.set()
    test_new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
        logger.info(f"TaskWorker-{scenario_name}-{test_run_id} task finished.")
    except asyncio.TimeoutError:
        logger.warning(
            f"TaskWorker-{scenario_name}-{test_run_id} task did not finish within timeout. Cancelling."
        )
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            logger.info(f"TaskWorker-{scenario_name}-{test_run_id} task was cancelled.")
    except Exception as e:
        logger.error(
            f"Error during TaskWorker-{scenario_name}-{test_run_id} cleanup: {e}",
            exc_info=True,
        )
    finally:
        global_task_worker_shutdown_event.clear()
    logger.info(f"--- {scenario_name} Test ({test_run_id}) Passed ---")
