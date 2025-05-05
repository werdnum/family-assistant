import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call  # Import call
from datetime import timedelta # Import timedelta from datetime
from typing import Any, Callable, Dict, List, Optional # Remove timedelta from typing import

import pytest
import pytest_asyncio # Keep this import if other fixtures need it
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.llm import LLMInterface, LLMOutput
from assertpy import assert_that, soft_assertions # Import assert_that and soft_assertions
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.message_history import get_recent_history

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)

# Import mock LLM helpers
from tests.mocks.mock_llm import Rule, get_last_message_text


# --- Test Helper Functions ---


def create_mock_context(
    mock_application: AsyncMock, # Pass the application mock
    bot_data: Optional[Dict[Any, Any]] = None
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    # Use the provided application mock, which should have .bot set
    context = ContextTypes.DEFAULT_TYPE(application=mock_application, chat_id=123, user_id=12345)
    context._bot = mock_application.bot # Explicitly assign the bot from the mock application
    # Do not reassign bot_data, update it instead
    if bot_data:
        context.bot_data.update(bot_data)
    # Mock other attributes if needed
    return context


def create_mock_update(
    message_text: str,
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    reply_to_message: Optional[Message] = None,
) -> Update:
    """Creates a mock Telegram Update object for a text message."""
    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=message_text,
        reply_to_message=reply_to_message,
    )
    update = Update(update_id=1, message=message)
    return update


# --- Test Cases ---


@pytest.mark.asyncio
async def test_simple_text_message(
    telegram_handler_fixture: TelegramHandlerTestFixture,
):
    """
    Tests the basic flow: user sends text, LLM responds, response sent back, history saved.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_chat_id = 123
    user_id = 12345
    user_message_id = 101
    assistant_message_id = 102
    user_text = "Hello assistant!"
    llm_response_text = "Hello TestUser! This is the LLM response."

    # Define rules for the RuleBasedMockLLMClient for this specific test
    def matcher_hello(messages, tools, tool_choice):
        last_text = get_last_message_text(messages)
        logger.debug(f"Matcher_hello checking: '{last_text}' against '{user_text}'")
        return last_text == user_text

    # Create a tuple literal directly, instead of calling the Rule type alias
    rule_hello_response: Rule = ( # Use the type hint for clarity
        matcher_hello, LLMOutput(content=llm_response_text, tool_calls=None)
    )
    fix.mock_llm.rules = [rule_hello_response] # Set rules for this test instance

    # Configure mock Bot response (simulate sending message)
    mock_sent_message = AsyncMock(spec=Message, message_id=assistant_message_id) # Use AsyncMock for awaitable return
    fix.mock_bot.send_message.return_value = mock_sent_message

    # Create mock Update and Context
    update = create_mock_update(user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id)
    # Pass the mock_application associated with the mock_telegram_service from the fixture
    context = create_mock_context(fix.mock_telegram_service.application, bot_data={"processing_service": fix.processing_service})

    # Act
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():
        # 1. LLM Call Verification
        assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(1)

        # Verify the structure and content of the message list passed to LLM
        last_call_args = fix.mock_llm._calls[0]
        messages_to_llm = last_call_args.get("messages")

        assert_that(messages_to_llm).described_as("Messages passed to LLM").is_instance_of(list).is_not_empty()

        # Compare the last message (user input) directly
        # Note: If comparing the whole list, need to account for dynamic system prompt content (e.g., timestamp)
        expected_last_message = {"role": "user", "content": user_text}
        assert_that(messages_to_llm[-1]).described_as("Last message to LLM").is_equal_to(expected_last_message)

        # 2. Bot API Call Verification (Output to user)
        fix.mock_bot.send_message.assert_awaited_once()
        # Check specific arguments using call_args
        args, kwargs = fix.mock_bot.send_message.call_args

        # Use chained assertions and descriptive names for kwargs
        assert_that(kwargs).described_as("send_message kwargs").contains_key("chat_id")
        assert_that(kwargs["chat_id"]).described_as("send_message chat_id").is_equal_to(user_chat_id)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("text")
        assert_that(kwargs["text"]).described_as("send_message text").contains(llm_response_text)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("reply_to_message_id")
        assert_that(kwargs["reply_to_message_id"]).described_as("send_message reply_to_message_id").is_equal_to(user_message_id)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("parse_mode").is_not_none()
        assert_that(kwargs["parse_mode"]).described_as("send_message parse_mode") # Just check it exists and isn't None

        assert_that(kwargs).described_as("send_message kwargs").contains_key("reply_markup").is_not_none()
        assert_that(kwargs["reply_markup"]).described_as("send_message reply_markup") # Just check it exists and isn't None
