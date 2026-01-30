import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import telegramify_markdown  # type: ignore[import-untyped]
from assertpy import assert_that, soft_assertions  # type: ignore[import-untyped]
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import ToolMessage
from tests.mocks.mock_llm import (
    LLMOutput,
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture
from .helpers import wait_for_bot_response

logger = logging.getLogger(__name__)

# --- Test Helper Functions (Copied from test_telegram_handler.py for self-containment) ---


def create_context(
    application: Any,  # noqa: ANN401 - telegram Application has complex generics
    bot_data: dict[Any, Any] | None = None,
    chat_id: int = 123,
    user_id: int = 12345,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a CallbackContext from an Application."""
    context = ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=chat_id, user_id=user_id
    )
    if bot_data:
        context.bot_data.update(bot_data)
    return context


def create_mock_update(
    message_text: str,
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    reply_to_message: Message | None = None,
) -> Update:
    """Creates a mock Telegram Update object for a text message."""
    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
        reply_to_message=reply_to_message,
    )
    update = Update(update_id=1, message=message)
    return update


# --- Test Case ---


@pytest.mark.asyncio
async def test_send_message_to_user_tool(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where a user (Alice) asks the assistant to send a message
    to another known user (Bob).
    Verifies:
    - LLM identifies Bob and calls the send_message_to_user tool.
    - The message is sent to Bob via telegram-test-api.
    - Alice receives a confirmation.
    - Correct number of LLM calls.
    """
    # Arrange
    fix = telegram_handler_fixture
    alice_chat_id = 123  # Sender
    alice_user_id = 12345
    alice_message_id = 401

    bob_chat_id = 789  # Recipient
    bob_name = "Bob TestUser"

    # --- Configure KnownUsersContextProvider for this test ---
    chat_id_map = {bob_chat_id: bob_name}
    # Ensure prompts are available in the processing_service config
    if not fix.processing_service.prompts:
        fix.processing_service.service_config.prompts = {
            "known_users_header": "Known users:",
            "known_user_item_format": "- {name} (ID: {chat_id})",
            "no_known_users": "No other users configured.",
        }

    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map=chat_id_map,
        prompts=fix.processing_service.prompts,
    )

    # Add the provider to the processing service instance for this test
    # Make a copy of the list to avoid modifying the original fixture's list if it's shared
    original_providers = list(fix.processing_service.context_providers)
    fix.processing_service.context_providers.append(known_users_provider)

    user_a_text = (
        f"Hey assistant, please tell {bob_name} that 'the meeting is at 3 PM'."
    )
    message_for_bob = "the meeting is at 3 PM"
    tool_call_id = f"call_{uuid.uuid4()}"

    llm_intermediate_response_to_alice = (
        f"Okay Alice, I will tell {bob_name}: '{message_for_bob}'."
    )
    llm_final_response_to_alice = (
        f"I've sent the message '{message_for_bob}' to {bob_name}."
    )

    # --- Mock LLM Rules ---
    def send_message_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages)
        return last_text == user_a_text

    send_message_tool_call = LLMOutput(
        content=llm_intermediate_response_to_alice,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="send_message_to_user",
                    arguments=json.dumps({
                        "target_chat_id": bob_chat_id,
                        "message_content": message_for_bob,
                    }),
                ),
            )
        ],
    )
    rule_send_message_request: Rule = (send_message_matcher, send_message_tool_call)

    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            isinstance(msg, ToolMessage)
            and msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            for msg in messages
        )

    final_confirmation_output = LLMOutput(
        content=llm_final_response_to_alice, tool_calls=None
    )
    rule_final_confirmation: Rule = (tool_result_matcher, final_confirmation_output)

    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    mock_llm_client.rules = [rule_send_message_request, rule_final_confirmation]

    # --- Create Update/Context for Alice's message ---
    update_alice = create_mock_update(
        user_a_text,
        chat_id=alice_chat_id,
        user_id=alice_user_id,
        message_id=alice_message_id,
    )
    context_alice = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    try:
        await fix.handler.message_handler(update_alice, context_alice)

        async with fix.get_db_context_func() as db_context:
            bob_history_all = await db_context.message_history.get_recent_with_metadata(
                interface_type="telegram",
                conversation_id=str(bob_chat_id),
            )

        # Assert - verify bot responses via telegram-test-api
        bot_responses = await wait_for_bot_response(
            fix.telegram_client, timeout=5.0, min_messages=1
        )

        with soft_assertions():  # type: ignore[attr-defined]
            # 1. LLM Calls
            mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
            assert_that(mock_llm_client._calls).described_as(
                "LLM Call Count"
            ).is_length(2)

            # 2. Bot sent messages - verify via telegram_client
            # We should have at least one response (the final confirmation to Alice)
            assert_that(bot_responses).described_as(
                "Bot responses via telegram-test-api"
            ).is_not_empty()

            # The final response to Alice should contain the confirmation
            final_response = bot_responses[-1]
            final_response_text = final_response.get("message", {}).get("text", "")
            expected_final_escaped_text = telegramify_markdown.markdownify(
                llm_final_response_to_alice
            )
            assert_that(final_response_text).described_as(
                "Final confirmation text to Alice"
            ).is_equal_to(expected_final_escaped_text)

            # 3. Confirmation Manager (should not be called for this tool by default)
            fix.mock_confirmation_manager.request_confirmation.assert_not_awaited()

            # 4. Message history records include processing profile identifier
            assert_that(bob_history_all).described_as(
                "History entries for Bob's conversation"
            ).is_not_empty()
            assert_that([
                msg["processing_profile_id"] for msg in bob_history_all
            ]).contains(fix.processing_service.service_config.id)
    finally:
        # Restore original context providers
        fix.processing_service.context_providers = original_providers


@pytest.mark.asyncio
async def test_send_message_to_self_rejected(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests that the send_message_to_user tool correctly rejects attempts to send
    a message to oneself.
    """
    # Arrange
    fix = telegram_handler_fixture
    alice_chat_id = 123  # Sender's chat ID
    alice_user_id = 12345
    alice_message_id = 501

    # Alice tries to send a message to herself
    user_text = "Send a message to myself saying 'test self message'"
    tool_call_id = f"call_{uuid.uuid4()}"

    # --- Mock LLM Rules ---
    def self_message_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages)
        return "Send a message to myself" in last_text

    self_message_tool_call = LLMOutput(
        content="I'll send that message to you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="send_message_to_user",
                    arguments=json.dumps({
                        "target_chat_id": alice_chat_id,  # Same as sender
                        "message_content": "test self message",
                    }),
                ),
            )
        ],
    )
    rule_self_message_request: Rule = (self_message_matcher, self_message_tool_call)

    def tool_error_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        # Look for the tool response with error message
        return any(
            isinstance(msg, ToolMessage)
            and msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and (
                "Cannot use send_message_to_user tool"
                in (content := str(msg.content or ""))
                and "already replying to" in content
            )
            for msg in messages
        )

    error_acknowledgment_output = LLMOutput(
        content="I understand - my response will be delivered directly to you in this conversation.",
        tool_calls=None,
    )
    rule_error_acknowledgment: Rule = (tool_error_matcher, error_acknowledgment_output)

    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    mock_llm_client.rules = [rule_self_message_request, rule_error_acknowledgment]

    # --- Create Update/Context ---
    update = create_mock_update(
        user_text,
        chat_id=alice_chat_id,
        user_id=alice_user_id,
        message_id=alice_message_id,
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    await fix.handler.message_handler(update, context)

    # Assert - verify bot responses via telegram-test-api
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)

    with soft_assertions():  # type: ignore[attr-defined]
        # 1. LLM should be called twice (initial request + error handling)
        mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
        assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

        # 2. Bot should have sent at least one message (the final response)
        assert_that(bot_responses).described_as("Bot responses").is_not_empty()

        # 3. The sent message should be the error acknowledgment
        final_response = bot_responses[-1]
        response_text = final_response.get("message", {}).get("text", "")
        expected_text = telegramify_markdown.markdownify(
            "I understand - my response will be delivered directly to you in this conversation."
        )
        assert_that(response_text).described_as("Response text").is_equal_to(
            expected_text
        )


@pytest.mark.asyncio
async def test_callback_send_message_to_self_rejected(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests that when awakened by a callback, the LLM correctly handles
    the case where it tries to use send_message_to_user to send to the
    same user (which should be rejected).
    """
    # Arrange
    fix = telegram_handler_fixture
    alice_chat_id = 123
    alice_user_id = 12345
    alice_message_id = 601

    # Simulate a callback trigger message
    callback_text = "System Callback Trigger:\n\nThe time is now 2024-01-01 10:00:00 UTC.\nYour scheduled context was:\n---\nRemind user about the meeting\n---"
    tool_call_id = f"call_{uuid.uuid4()}"

    # --- Mock LLM Rules ---
    def callback_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages)
        return "System Callback Trigger" in last_text and "meeting" in last_text

    # LLM incorrectly tries to use send_message_to_user
    callback_tool_call = LLMOutput(
        content="I'll remind you about the meeting.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="send_message_to_user",
                    arguments=json.dumps({
                        "target_chat_id": alice_chat_id,
                        "message_content": "Don't forget about your meeting!",
                    }),
                ),
            )
        ],
    )
    rule_callback_request: Rule = (callback_matcher, callback_tool_call)

    def callback_error_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            isinstance(msg, ToolMessage)
            and msg.role == "tool"
            and msg.tool_call_id == tool_call_id
            and (
                "Cannot use send_message_to_user tool"
                in (content := str(msg.content or ""))
                and "already replying to" in content
            )
            for msg in messages
        )

    callback_correction_output = LLMOutput(
        content="Don't forget about your meeting!", tool_calls=None
    )
    rule_callback_correction: Rule = (
        callback_error_matcher,
        callback_correction_output,
    )

    mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
    mock_llm_client.rules = [rule_callback_request, rule_callback_correction]

    # --- Create Update/Context for callback ---
    update = create_mock_update(
        callback_text,
        chat_id=alice_chat_id,
        user_id=alice_user_id,
        message_id=alice_message_id,
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    await fix.handler.message_handler(update, context)

    # Assert - verify bot responses via telegram-test-api
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)

    with soft_assertions():  # type: ignore[attr-defined]
        # 1. LLM should be called twice
        mock_llm_client = cast("RuleBasedMockLLMClient", fix.mock_llm)
        assert_that(mock_llm_client._calls).described_as("LLM Call Count").is_length(2)

        # 2. Bot should have sent at least one message (the corrected response)
        assert_that(bot_responses).described_as("Bot responses").is_not_empty()

        # 3. The sent message should be the meeting reminder
        final_response = bot_responses[-1]
        response_text = final_response.get("message", {}).get("text", "")
        expected_text = telegramify_markdown.markdownify(
            "Don't forget about your meeting!"
        )
        assert_that(response_text).described_as("Response text").is_equal_to(
            expected_text
        )
