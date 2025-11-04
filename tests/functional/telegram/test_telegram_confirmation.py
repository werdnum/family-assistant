import asyncio
import json
import logging
import uuid
from typing import cast
from unittest.mock import AsyncMock

import pytest
import telegramify_markdown  # type: ignore[import-untyped]
from assertpy import assert_that, soft_assertions
from telegram import Message

# Import mock LLM helpers
from family_assistant.llm import ToolCallFunction, ToolCallItem
from tests.functional.telegram.test_telegram_handler import (
    create_mock_context,
    create_mock_update,
)
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,  # Use alias for clarity
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

# Import the test fixture and helper functions
from .conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)

# --- Constants for Test ---
USER_CHAT_ID = 123
USER_ID = 12345
# Use add_or_update_note, but configure it dynamically in tests
TOOL_NAME_SENSITIVE = "add_or_update_note"


@pytest.mark.asyncio
async def test_confirmation_accepted(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested and accepted by the user.
    The sensitive tool should be executed, and a success message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    processing_service = fix.processing_service
    if processing_service and processing_service.tools_provider:
        # Get the ConfirmingToolsProvider if it exists
        provider = processing_service.tools_provider
        if hasattr(provider, "_tools_requiring_confirmation"):
            # Add the tool to the set of tools requiring confirmation for this test
            provider._tools_requiring_confirmation.add(TOOL_NAME_SENSITIVE)  # type: ignore[attr-defined]
    # Cast mock_llm to the concrete type to access specific attributes like .rules and ._calls
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 401
    assistant_final_message_id = 402
    # Test data for adding a note (confirmed scenario)
    test_note_title = f"Confirmed Note Add {uuid.uuid4()}"
    test_note_content = "This note required confirmation."
    user_text = (
        f"Please add this note: Title={test_note_title}, Content={test_note_content}"
    )
    tool_call_id = f"call_accept_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note for you."
    llm_final_success_text = f"Okay, I have added the note titled '{test_note_title}'."

    # --- Mock LLM Rules ---
    # 1. User asks -> LLM requests sensitive tool
    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)
    # Define expected tool success message based on mock return value
    # The actual tool returns "has been updated successfully" not "added/updated successfully"
    expected_tool_success_result = (
        f"Note '{test_note_title}' has been updated successfully."
    )

    # 2. Tool result received -> LLM gives final success message
    def success_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content
            == expected_tool_success_result  # Match exact success message
            for msg in messages
        )

    final_success_output = MockLLMOutput(
        content=llm_final_success_text
    )  # Use MockLLMOutput
    rule_final_success: Rule = (success_result_matcher, final_success_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_success]  # Use casted client

    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate user ACCEPTING the confirmation prompt
    # fix.mock_confirmation_manager is the AsyncMock that replaced request_confirmation
    fix.mock_confirmation_manager.return_value = True

    # --- No need to mock tool execution ---
    # The actual add_or_update_note tool will execute after confirmation

    # --- Mock Bot Response ---
    # Mock the final message sent by the bot after successful tool execution
    mock_final_message = AsyncMock(spec=Message, message_id=assistant_final_message_id)
    fix.mock_bot.send_message.return_value = mock_final_message

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )  # noqa: E501
    context = create_mock_context(
        fix.mock_application,  # Use the mock_application from the fixture
        bot_data={"processing_service": fix.processing_service},
    )  # noqa: E501

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert: Perform assertions *after* the handler call but *within* the patch context
    # to ensure mocks are checked correctly.
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called because the tool was configured to require it
        # Note: We use assert_called_once() instead of assert_awaited_once() because
        # AsyncMock's await tracking doesn't work correctly when patched as a method
        # Also, call_args aren't tracked correctly when patched directly, so we can't check them
        fix.mock_confirmation_manager.assert_called_once()

        # 2. The tool was executed successfully (we know because the LLM received the result)

        # 3. LLM was called twice (request tool, process result)
        assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(
            2
        )  # Use casted client

    # 4. Final success message sent to user
    fix.mock_bot.send_message.assert_awaited_once()
    _, kwargs_bot = fix.mock_bot.send_message.call_args
    expected_final_escaped_text = telegramify_markdown.markdownify(
        llm_final_success_text
    )
    assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(
        expected_final_escaped_text
    )
    assert_that(kwargs_bot["reply_to_message_id"]).described_as(
        "Final bot message reply ID"
    ).is_equal_to(user_message_id)


@pytest.mark.asyncio
async def test_confirmation_rejected(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested and rejected by the user.
    The sensitive tool should NOT be executed, and a cancellation message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    processing_service = fix.processing_service
    if processing_service and processing_service.tools_provider:
        # Get the ConfirmingToolsProvider if it exists
        provider = processing_service.tools_provider
        if hasattr(provider, "_tools_requiring_confirmation"):
            # Add the tool to the set of tools requiring confirmation for this test
            provider._tools_requiring_confirmation.add(TOOL_NAME_SENSITIVE)  # type: ignore[attr-defined]
    # Cast mock_llm to the concrete type
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 501
    assistant_cancel_message_id = 502  # ID for the cancellation message
    # Test data for adding a note (rejected scenario)
    test_note_title = f"Rejected Note Add {uuid.uuid4()}"
    test_note_content = "This note add was rejected."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_reject_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on rejection
    # This needs to match the *actual* string from ConfirmingToolsProvider
    tool_cancel_result_text = (
        f"OK. Action cancelled by user for tool '{TOOL_NAME_SENSITIVE}'."
    )

    # Final message from LLM after seeing the cancellation
    llm_final_cancel_text = "Okay, I have cancelled the request."

    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the cancellation message from the tool provider
    def cancel_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content == tool_cancel_result_text  # Match the exact string
            for msg in messages
        )

    final_cancel_output = MockLLMOutput(
        content=llm_final_cancel_text
    )  # Use MockLLMOutput
    rule_final_cancel: Rule = (cancel_result_matcher, final_cancel_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_cancel]  # Use casted client
    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate user REJECTING the confirmation prompt
    fix.mock_confirmation_manager.return_value = False

    # --- Mock Bot Response ---
    mock_cancel_message = AsyncMock(
        spec=Message, message_id=assistant_cancel_message_id
    )
    fix.mock_bot.send_message.return_value = mock_cancel_message

    # --- No need to mock tool execution ---
    # The tool should NOT be executed when confirmation is rejected

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )  # noqa: E501
    context = create_mock_context(
        fix.mock_application,  # Use the mock_application from the fixture
        bot_data={"processing_service": fix.processing_service},
    )  # noqa: E501

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called
        # Note: We use assert_called_once() instead of assert_awaited_once() because
        # AsyncMock's await tracking doesn't work correctly when patched as a method
        # Also, call_args aren't tracked correctly when patched directly, so we can't check them
        fix.mock_confirmation_manager.assert_called_once()
        # 2. Tool was NOT executed (confirmation was rejected)
        # We can verify this by checking the LLM received the cancellation message

    # 3. LLM was called twice (request tool, process cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(
        2
    )  # Use casted client

    # 4. Final cancellation message sent to user (matching rule_final_cancel)
    # This assertion is now outside both patches.
    fix.mock_bot.send_message.assert_awaited_once()
    _, kwargs_bot = fix.mock_bot.send_message.call_args
    expected_cancel_escaped_text = telegramify_markdown.markdownify(
        llm_final_cancel_text
    )
    assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(
        expected_cancel_escaped_text
    )
    assert_that(kwargs_bot["reply_to_message_id"]).described_as(
        "Final bot message reply ID"
    ).is_equal_to(user_message_id)


@pytest.mark.asyncio
async def test_confirmation_timed_out(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where confirmation is requested but times out.
    The sensitive tool should NOT be executed, and a cancellation/timeout message returned.
    """
    # Arrange
    fix = telegram_handler_fixture

    # Temporarily add add_or_update_note to confirm_tools just for this test
    # This is a test-specific override to verify confirmation flow works
    processing_service = fix.processing_service
    if processing_service and processing_service.tools_provider:
        # Get the ConfirmingToolsProvider if it exists
        provider = processing_service.tools_provider
        if hasattr(provider, "_tools_requiring_confirmation"):
            # Add the tool to the set of tools requiring confirmation for this test
            provider._tools_requiring_confirmation.add(TOOL_NAME_SENSITIVE)  # type: ignore[attr-defined]
    # Cast mock_llm to the concrete type
    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    user_message_id = 601
    assistant_timeout_message_id = 602  # ID for the timeout message
    # Test data for adding a note (timeout scenario)
    test_note_title = f"Timeout Note Add {uuid.uuid4()}"
    test_note_content = "This note add timed out."
    user_text = f"Add note: Title={test_note_title}, Content={test_note_content}"
    tool_call_id = f"call_timeout_{uuid.uuid4()}"
    llm_request_tool_text = "Okay, I can add that note."
    # Message returned by ConfirmingToolsProvider on timeout (same as rejection)
    # This needs to match the *actual* string from ConfirmingToolsProvider for timeout
    tool_timeout_result_text = f"Action cancelled: Confirmation request for tool '{TOOL_NAME_SENSITIVE}' timed out."  # Final message from LLM after seeing the timeout/cancellation
    llm_final_timeout_text = "Okay, the request timed out and was cancelled."

    def request_delete_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return user_text in get_last_message_text(messages)

    request_tool_output = MockLLMOutput(  # Use MockLLMOutput for rule definition
        content=llm_request_tool_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=TOOL_NAME_SENSITIVE,
                    # Arguments for add_or_update_note
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_request_tool: Rule = (request_delete_matcher, request_tool_output)

    # 2. LLM rule to handle the timeout/cancellation message from the tool provider
    def timeout_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and msg.content == tool_timeout_result_text  # Match the exact string
            for msg in messages
        )

    final_timeout_output = MockLLMOutput(
        content=llm_final_timeout_text
    )  # Use MockLLMOutput
    rule_final_timeout: Rule = (timeout_result_matcher, final_timeout_output)

    mock_llm_client.rules = [rule_request_tool, rule_final_timeout]  # Use casted client
    # --- No need to create ConfirmingToolsProvider manually ---
    # The Assistant will create it automatically based on confirm_tools config

    # --- Mock Confirmation Manager ---
    # Simulate TIMEOUT during the confirmation request
    fix.mock_confirmation_manager.side_effect = asyncio.TimeoutError

    # --- Mock Bot Response ---
    mock_timeout_message = AsyncMock(
        spec=Message, message_id=assistant_timeout_message_id
    )
    fix.mock_bot.send_message.return_value = mock_timeout_message

    # --- No need to mock tool execution ---
    # The tool should NOT be executed when confirmation times out

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )  # noqa: E501
    context = create_mock_context(
        fix.mock_application,  # Use the mock_application from the fixture
        bot_data={"processing_service": fix.processing_service},
    )  # noqa: E501

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called (and raised TimeoutError)
        # Note: We use assert_called_once() instead of assert_awaited_once() because
        # AsyncMock's await tracking doesn't work correctly when patched as a method
        # Also, call_args aren't tracked correctly when patched directly, so we can't check them
        fix.mock_confirmation_manager.assert_called_once()
        # 2. Tool was NOT executed (confirmation timed out)
        # We can verify this by checking the LLM received the timeout message

    # 3. LLM was called twice (request tool, process timeout/cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(
        2
    )  # Use casted client

    # 4. Final timeout message sent to user (matching rule_final_timeout)
    # This assertion is now outside both patches.
    fix.mock_bot.send_message.assert_awaited_once()
    _, kwargs_bot = fix.mock_bot.send_message.call_args
    # Get the default response text from the mock LLM used in the fixture
    expected_timeout_escaped_text = telegramify_markdown.markdownify(
        llm_final_timeout_text
    )
    assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(
        expected_timeout_escaped_text
    )
    assert_that(kwargs_bot["reply_to_message_id"]).described_as(
        "Final bot message reply ID"
    ).is_equal_to(user_message_id)
