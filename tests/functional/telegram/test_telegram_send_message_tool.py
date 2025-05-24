import json
import logging
import uuid
from datetime import datetime, timezone  # Keep this, create_mock_update uses it
from typing import Any  # Keep this, create_mock_context uses it
from unittest.mock import AsyncMock

import pytest
import telegramify_markdown  # type: ignore[import-untyped]
from assertpy import assert_that, soft_assertions  # type: ignore[import-untyped]
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.context_providers import KnownUsersContextProvider
from family_assistant.llm import LLMOutput
from tests.mocks.mock_llm import MatcherArgs, Rule, get_last_message_text

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)

# --- Test Helper Functions (Copied from test_telegram_handler.py for self-containment) ---


def create_mock_context(
    mock_application: AsyncMock,
    bot_data: dict[Any, Any] | None = None,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    context = ContextTypes.DEFAULT_TYPE(
        application=mock_application, chat_id=123, user_id=12345
    )
    context._bot = mock_application.bot
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
        date=datetime.now(timezone.utc),  # Use timezone.utc
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
    - The message is "sent" to Bob (mocked bot call).
    - Alice receives a confirmation.
    - Correct number of LLM and bot calls.
    """
    # Arrange
    fix = telegram_handler_fixture
    alice_chat_id = 123  # Sender
    alice_user_id = 12345
    alice_message_id = 401

    bob_chat_id = 789  # Recipient
    bob_name = "Bob TestUser"

    # Message IDs for bot's communications
    message_to_bob_id = 402
    final_confirmation_to_alice_id = 403

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
        # Check if the system prompt contains Bob's info (implicitly tested by tool call args)
        # Check if the user's last message is the trigger
        return last_text == user_a_text

    send_message_tool_call = LLMOutput(
        content=llm_intermediate_response_to_alice,
        tool_calls=[
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "send_message_to_user",
                    "arguments": json.dumps({
                        "target_chat_id": bob_chat_id,
                        "message_content": message_for_bob,
                    }),
                },
            }
        ],
    )
    rule_send_message_request: Rule = (send_message_matcher, send_message_tool_call)

    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id
            for msg in messages
        )

    final_confirmation_output = LLMOutput(
        content=llm_final_response_to_alice, tool_calls=None
    )
    rule_final_confirmation: Rule = (tool_result_matcher, final_confirmation_output)

    fix.mock_llm.rules = [rule_send_message_request, rule_final_confirmation]

    # --- Mock Bot Responses ---
    mock_message_sent_to_bob = AsyncMock(spec=Message, message_id=message_to_bob_id)
    mock_final_reply_to_alice = AsyncMock(
        spec=Message, message_id=final_confirmation_to_alice_id
    )
    fix.mock_bot.send_message.side_effect = [
        mock_message_sent_to_bob,
        mock_final_reply_to_alice,
    ]

    # --- Create Mock Update/Context for Alice's message ---
    update_alice = create_mock_update(
        user_a_text,
        chat_id=alice_chat_id,
        user_id=alice_user_id,
        message_id=alice_message_id,
    )
    context_alice = create_mock_context(
        fix.mock_telegram_service.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    try:
        await fix.handler.message_handler(update_alice, context_alice)

        # Assert
        with soft_assertions():
            # 1. LLM Calls
            assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

            # 2. Bot API Calls (send_message)
            assert_that(fix.mock_bot.send_message.await_count).described_as(
                "Bot send_message call count"
            ).is_equal_to(2)

            # Call 1: Message sent to Bob by the tool
            args_to_bob, kwargs_to_bob = fix.mock_bot.send_message.call_args_list[0]
            assert_that(kwargs_to_bob["chat_id"]).described_as(
                "Chat ID for message to Bob"
            ).is_equal_to(bob_chat_id)
            assert_that(kwargs_to_bob["text"]).described_as(
                "Text for message to Bob"
            ).is_equal_to(message_for_bob)
            assert_that(kwargs_to_bob).described_as(
                "kwargs for message to Bob"
            ).does_not_contain_key("reply_to_message_id")
            assert_that(kwargs_to_bob).described_as(
                "kwargs for message to Bob"
            ).does_not_contain_key("parse_mode")

            # Call 2: Final confirmation sent to Alice by the handler
            args_to_alice, kwargs_to_alice = fix.mock_bot.send_message.call_args_list[1]
            assert_that(kwargs_to_alice["chat_id"]).described_as(
                "Chat ID for confirmation to Alice"
            ).is_equal_to(alice_chat_id)

            expected_final_escaped_text = telegramify_markdown.markdownify(
                llm_final_response_to_alice
            )
            assert_that(kwargs_to_alice["text"]).described_as(
                "Text for confirmation to Alice"
            ).is_equal_to(expected_final_escaped_text)
            assert_that(kwargs_to_alice["reply_to_message_id"]).described_as(
                "Reply ID for confirmation to Alice"
            ).is_equal_to(alice_message_id)
            assert_that(kwargs_to_alice).described_as(
                "kwargs for confirmation to Alice"
            ).contains_key("parse_mode")
            assert_that(kwargs_to_alice["parse_mode"]).described_as(
                "Parse mode for confirmation to Alice"
            ).is_not_none()
            assert_that(kwargs_to_alice).described_as(
                "kwargs for confirmation to Alice"
            ).contains_key("reply_markup")
            assert_that(kwargs_to_alice["reply_markup"]).described_as(
                "Reply markup for confirmation to Alice"
            ).is_not_none()

            # 3. Confirmation Manager (should not be called for this tool by default)
            fix.mock_confirmation_manager.request_confirmation.assert_not_awaited()
    finally:
        # Restore original context providers
        fix.processing_service.context_providers = original_providers
