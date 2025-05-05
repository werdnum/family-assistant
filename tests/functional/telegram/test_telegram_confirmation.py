import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch # Import ANY

import pytest
import telegramify_markdown
from assertpy import assert_that, soft_assertions
from telegram import Chat, ForceReply, Message, Update, User
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from family_assistant.llm import LLMOutput

# Import the test fixture and helper functions
from .conftest import TelegramHandlerTestFixture
from tests.functional.telegram.test_telegram_handler import (
    create_mock_context,
    create_mock_update,
)

# Import mock LLM helpers
from tests.mocks.mock_llm import Rule, get_last_message_text

logger = logging.getLogger(__name__)

# --- Constants for Test ---
USER_CHAT_ID = 123
USER_ID = 12345
# Use add_or_update_note, but configure it dynamically in tests
TOOL_NAME_SENSITIVE = "add_or_update_note"


@pytest.mark.asyncio
async def test_confirmation_accepted(
    telegram_handler_fixture: TelegramHandlerTestFixture,
):
    """
    Tests the flow where confirmation is requested and accepted by the user.
    The sensitive tool should be executed, and a success message returned.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_message_id = 401
    assistant_final_message_id = 402
    # Test data for adding a note (confirmed scenario)
    test_note_title = f"Confirmed Note Add {uuid.uuid4()}"
    test_note_content = "This note required confirmation."
    user_text = f"Please add this note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_accept_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note for you."
    llm_final_success_text = f"Okay, I have added the note titled '{test_note_title}'."

    # --- Mock LLM Rules ---
    # 1. User asks -> LLM requests sensitive tool
    def request_delete_matcher(messages, tools, tool_choice):
        return user_text in get_last_message_text(messages)

    request_tool_output = LLMOutput(
        content=llm_request_tool_text,
        tool_calls=[{
            "id": tool_call_id, "type": "function",
            "function": {
                "name": TOOL_NAME_SENSITIVE,
                # Arguments for add_or_update_note
                "arguments": json.dumps({"title": test_note_title, "content": test_note_content}),
            }
        }]
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. Tool result received -> LLM gives final success message
    def success_result_matcher(messages, tools, tool_choice):
        return any(
            msg.get("role") == "tool"
            and msg.get("tool_call_id") == tool_call_id
            # Check content to ensure it's the *actual success message* from the real tool
            and "Success" in msg.get("content", "")
            for msg in messages
        )

    final_success_output = LLMOutput(content=llm_final_success_text)
    rule_final_success: Rule = (success_result_matcher, final_success_output)

    fix.mock_llm.rules = [rule_request_tool, rule_final_success]

    # --- Configure Confirmation for this test ---
    # Explicitly tell the provider to require confirmation for the note tool
    fix.tools_provider.tools_requiring_confirmation = {TOOL_NAME_SENSITIVE}

    # --- Mock Confirmation Manager ---
    # Simulate user ACCEPTING the confirmation prompt
    fix.mock_confirmation_manager.request_confirmation.return_value = True

    # --- Mock Tool Execution ---
    # Mock the *wrapped* provider's execute_tool to simulate success *after* confirmation
    # Patch the *wrapped* provider's execute_tool to capture the call
    with patch.object(
        fix.wrapped_tools_provider, 'execute_tool', new_callable=AsyncMock
    ) as mock_execute_wrapped:
        # Simulate the tool execution succeeding after confirmation
        mock_execute_wrapped.return_value = {"result": f"Success: Note '{test_note_title}' added."}

        # --- Mock Bot Response ---
        # Mock the final message sent by the bot after successful tool execution
        mock_final_message = AsyncMock(spec=Message, message_id=assistant_final_message_id)
        fix.mock_bot.send_message.return_value = mock_final_message

        # --- Create Mock Update/Context ---
        update = create_mock_update(user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id)
        context = create_mock_context(fix.mock_telegram_service.application, bot_data={"processing_service": fix.processing_service})

        # Act
        await fix.handler.message_handler(update, context)

        # Assert (moved inside the 'with patch' block to access mock_execute_wrapped)
        with soft_assertions():
            # 1. Confirmation Manager was called because the tool was configured to require it
            fix.mock_confirmation_manager.request_confirmation.assert_awaited_once()
            # Check args
            conf_args, conf_kwargs = fix.mock_confirmation_manager.request_confirmation.call_args
            assert_that(conf_kwargs.get("tool_name")).is_equal_to(TOOL_NAME_SENSITIVE)
            assert_that(conf_kwargs.get("tool_args")).is_equal_to({"title": test_note_title, "content": test_note_content})

            # 2. Wrapped Tool Provider's execute_tool was called (meaning confirmation passed)
            mock_execute_wrapped.assert_awaited_once() # Check it was called
            # Check arguments passed to the wrapped tool
            call_args_tuple, call_kwargs_dict = mock_execute_wrapped.await_args
            called_name = call_args_tuple[0] if call_args_tuple else call_kwargs_dict.get("name")
            called_arguments = call_args_tuple[1] if len(call_args_tuple) > 1 else call_kwargs_dict.get("arguments")
            assert_that(called_name).is_equal_to(TOOL_NAME_SENSITIVE)
            assert_that(called_arguments).is_equal_to({"title": test_note_title, "content": test_note_content})

            # 3. LLM was called twice (request tool, process result)
            assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

            # 4. Final success message sent to user
            fix.mock_bot.send_message.assert_awaited_once()
            args_bot, kwargs_bot = fix.mock_bot.send_message.call_args
            expected_final_escaped_text = telegramify_markdown.markdownify(llm_final_success_text)
            assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(expected_final_escaped_text)
            assert_that(kwargs_bot["reply_to_message_id"]).described_as("Final bot message reply ID").is_equal_to(user_message_id)


@pytest.mark.asyncio
async def test_confirmation_rejected(
    telegram_handler_fixture: TelegramHandlerTestFixture,
):
    """
    Tests the flow where confirmation is requested and rejected by the user.
    The sensitive tool should NOT be executed, and a cancellation message returned.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_message_id = 501
    assistant_cancel_message_id = 502 # ID for the cancellation message
    # Test data for adding a note (rejected scenario)
    test_note_title = f"Rejected Note Add {uuid.uuid4()}"
    test_note_content = "This note add was rejected."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_reject_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on rejection
    tool_cancel_result_text = f"Okay, I will not run the tool `{TOOL_NAME_SENSITIVE}`."
    # Final message from LLM after seeing the cancellation
    llm_final_cancel_text = "Okay, I have cancelled the request."

    def request_delete_matcher(messages, tools, tool_choice):
        return user_text in get_last_message_text(messages)

    request_tool_output = LLMOutput(
        content=llm_request_tool_text,
        tool_calls=[{
            "id": tool_call_id, "type": "function",
            "function": {
                "name": TOOL_NAME_SENSITIVE,
                # Arguments for add_or_update_note
                "arguments": json.dumps({"title": test_note_title, "content": test_note_content}),
            }
        }]
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the cancellation message from the tool provider
    def cancel_result_matcher(messages, tools, tool_choice):
        return any(
            msg.get("role") == "tool"
            and msg.get("tool_call_id") == tool_call_id
            and tool_cancel_result_text in msg.get("content", "")
            for msg in messages
        )
    final_cancel_output = LLMOutput(content=llm_final_cancel_text)
    rule_final_cancel: Rule = (cancel_result_matcher, final_cancel_output)

    fix.mock_llm.rules = [rule_request_tool, rule_final_cancel]
    # --- Configure Confirmation for this test ---
    fix.tools_provider.tools_requiring_confirmation = {TOOL_NAME_SENSITIVE}
    # --- Mock Confirmation Manager ---
    # Simulate user REJECTING the confirmation prompt
    fix.mock_confirmation_manager.request_confirmation.return_value = False

    # --- Mock Bot Response ---
    mock_cancel_message = AsyncMock(spec=Message, message_id=assistant_cancel_message_id)
    fix.mock_bot.send_message.return_value = mock_cancel_message

    # --- Mock Tool Execution (Should NOT be called) ---
    # Patch the *wrapped* provider's execute_tool to fail if called
    with patch.object(
        fix.wrapped_tools_provider, 'execute_tool', new_callable=AsyncMock
    ) as mock_execute_wrapped:
        # --- Create Mock Update/Context ---
        update = create_mock_update(user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id)
        context = create_mock_context(fix.mock_telegram_service.application, bot_data={"processing_service": fix.processing_service})

        # Act
        await fix.handler.message_handler(update, context)

        # Assert
        with soft_assertions():
            # 1. Confirmation Manager was called
            fix.mock_confirmation_manager.request_confirmation.assert_awaited_once()

            # 2. Wrapped tool provider was NOT called
            mock_execute_wrapped.assert_not_awaited()

            # 3. LLM was called twice (request tool, process cancellation result)
            assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

            # 4. Final cancellation message sent to user (matching rule_final_cancel)
            fix.mock_bot.send_message.assert_awaited_once()
            args_bot, kwargs_bot = fix.mock_bot.send_message.call_args
            expected_cancel_escaped_text = telegramify_markdown.markdownify(llm_final_cancel_text)
            assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(expected_cancel_escaped_text)
            assert_that(kwargs_bot["reply_to_message_id"]).described_as("Final bot message reply ID").is_equal_to(user_message_id)


@pytest.mark.asyncio
async def test_confirmation_timed_out(
    telegram_handler_fixture: TelegramHandlerTestFixture,
):
    """
    Tests the flow where confirmation is requested but times out.
    The sensitive tool should NOT be executed, and a cancellation/timeout message returned.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_message_id = 601
    assistant_timeout_message_id = 602 # ID for the timeout message
    # Test data for adding a note (timeout scenario)
    test_note_title = f"Timeout Note Add {uuid.uuid4()}"
    test_note_content = "This note add timed out."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_timeout_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on timeout (same as rejection)
    tool_timeout_result_text = f"Okay, I will not run the tool `{TOOL_NAME_SENSITIVE}`."
    # Final message from LLM after seeing the timeout/cancellation
    llm_final_timeout_text = "Okay, the request timed out and was cancelled."

    def request_delete_matcher(messages, tools, tool_choice):
        return user_text in get_last_message_text(messages)

    request_tool_output = LLMOutput(
        content=llm_request_tool_text,
        tool_calls=[{
            "id": tool_call_id, "type": "function",
            "function": {
                "name": TOOL_NAME_SENSITIVE,
                # Arguments for add_or_update_note
                "arguments": json.dumps({"title": test_note_title, "content": test_note_content}),
            }
        }]
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the timeout/cancellation message from the tool provider
    def timeout_result_matcher(messages, tools, tool_choice):
        return any(
            msg.get("role") == "tool"
            and msg.get("tool_call_id") == tool_call_id
            and tool_timeout_result_text in msg.get("content", "")
            for msg in messages
        )
    final_timeout_output = LLMOutput(content=llm_final_timeout_text)
    rule_final_timeout: Rule = (timeout_result_matcher, final_timeout_output)

    fix.mock_llm.rules = [rule_request_tool, rule_final_timeout]
    # --- Configure Confirmation for this test ---
    fix.tools_provider.tools_requiring_confirmation = {TOOL_NAME_SENSITIVE}
    # --- Mock Confirmation Manager ---
    # Simulate TIMEOUT during the confirmation request
    fix.mock_confirmation_manager.request_confirmation.side_effect = asyncio.TimeoutError

    # --- Mock Bot Response ---
    mock_timeout_message = AsyncMock(spec=Message, message_id=assistant_timeout_message_id)
    fix.mock_bot.send_message.return_value = mock_timeout_message

    # --- Mock Tool Execution (Should NOT be called) ---
    # Patch the *wrapped* provider's execute_tool to fail if called
    with patch.object(
        fix.wrapped_tools_provider, 'execute_tool', new_callable=AsyncMock
    ) as mock_execute_wrapped:
        # --- Create Mock Update/Context ---
        update = create_mock_update(user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id)
        context = create_mock_context(fix.mock_telegram_service.application, bot_data={"processing_service": fix.processing_service})

        # Act
        await fix.handler.message_handler(update, context)

        # Assert
        with soft_assertions():
            # 1. Confirmation Manager was called
            fix.mock_confirmation_manager.request_confirmation.assert_awaited_once()
            # 2. Wrapped tool provider was NOT called
            mock_execute_wrapped.assert_not_awaited()

            # 3. LLM was called twice (request tool, process timeout/cancellation result)
            assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

            # 4. Final timeout message sent to user (matching rule_final_timeout)
            fix.mock_bot.send_message.assert_awaited_once()
            args_bot, kwargs_bot = fix.mock_bot.send_message.call_args
            # Get the default response text from the mock LLM used in the fixture
            expected_timeout_escaped_text = telegramify_markdown.markdownify(
                llm_final_timeout_text
            )
            assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(expected_timeout_escaped_text)
            assert_that(kwargs_bot["reply_to_message_id"]).described_as("Final bot message reply ID").is_equal_to(user_message_id)
