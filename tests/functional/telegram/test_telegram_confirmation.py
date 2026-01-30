import asyncio
import json
import logging
import uuid
from typing import cast

import pytest
import telegramify_markdown  # type: ignore[import-untyped]  # No type stubs available
from assertpy import assert_that, soft_assertions

# Import mock LLM helpers
from family_assistant.llm import ToolCallFunction, ToolCallItem
from tests.functional.telegram.test_telegram_handler import (
    create_context,
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
from .helpers import wait_for_bot_response

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

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called because the tool was configured to require it
        fix.mock_confirmation_manager.assert_called_once()

        # 2. LLM was called twice (request tool, process result)
        assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_final_escaped_text = telegramify_markdown.markdownify(
        llm_final_success_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_final_escaped_text
    )


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

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called
        fix.mock_confirmation_manager.assert_called_once()

    # 2. LLM was called twice (request tool, process cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_cancel_escaped_text = telegramify_markdown.markdownify(
        llm_final_cancel_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_cancel_escaped_text
    )


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

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=USER_CHAT_ID, user_id=USER_ID, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act: Call the handler - it will use the ConfirmingToolsProvider created by Assistant
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager was called (and raised TimeoutError)
        fix.mock_confirmation_manager.assert_called_once()

    # 2. LLM was called twice (request tool, process timeout/cancellation result)
    assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

    # 3. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the final bot message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    expected_timeout_escaped_text = telegramify_markdown.markdownify(
        llm_final_timeout_text
    )
    assert_that(bot_message_text).described_as("Final bot message text").is_equal_to(
        expected_timeout_escaped_text
    )
