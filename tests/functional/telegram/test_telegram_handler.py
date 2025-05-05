import asyncio
import contextlib
import logging
from datetime import datetime, timezone
import uuid # Add import
import json # Add import
from unittest.mock import AsyncMock, MagicMock, call  # Import call
from typing import Optional, Dict, Any # Add missing typing imports
from datetime import timedelta # Import timedelta from datetime

import pytest
import pytest_asyncio # Keep this import if other fixtures need it
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.llm import LLMInterface, LLMOutput
import telegramify_markdown # Import the library
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

        # 2. Bot API Call Verification (Output to user)
        fix.mock_bot.send_message.assert_awaited_once()
        # Check specific arguments using call_args
        args, kwargs = fix.mock_bot.send_message.call_args

        # Use chained assertions and descriptive names for kwargs
        assert_that(kwargs).described_as("send_message kwargs").contains_key("chat_id")
        assert_that(kwargs["chat_id"]).described_as("send_message chat_id").is_equal_to(user_chat_id)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("text")
        # Calculate the expected escaped text using the library
        expected_escaped_text = telegramify_markdown.markdownify(llm_response_text)
        assert_that(kwargs["text"]).described_as("send_message text").is_equal_to(expected_escaped_text)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("reply_to_message_id")
        assert_that(kwargs["reply_to_message_id"]).described_as("send_message reply_to_message_id").is_equal_to(user_message_id)

        assert_that(kwargs).described_as("send_message kwargs").contains_key("parse_mode").is_not_none()
        assert_that(kwargs["parse_mode"]).described_as("send_message parse_mode") # Just check it exists and isn't None

        assert_that(kwargs).described_as("send_message kwargs").contains_key("reply_markup").is_not_none()
        assert_that(kwargs["reply_markup"]).described_as("send_message reply_markup") # Just check it exists and isn't None


@pytest.mark.asyncio
async def test_add_note_tool_usage(
    telegram_handler_fixture: TelegramHandlerTestFixture,
):
    """
    Tests the flow where user asks to add a note, LLM requests the tool,
    confirmation is granted, the tool executes, and the note is saved.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_chat_id = 123
    user_id = 12345
    user_message_id = 201
    assistant_final_message_id = 202 # ID for the final confirmation message
    test_note_title = f"Telegram Tool Test Note {uuid.uuid4()}"
    test_note_content = "Content added via Telegram handler test."
    user_text = f"Please remember this note. Title: {test_note_title}. Content: {test_note_content}"
    tool_call_id = f"call_{uuid.uuid4()}" # ID for the LLM's tool request
    llm_tool_request_text = "Okay, I can add that note for you." # Optional text alongside tool call
    llm_final_confirmation_text = f"Okay, I've saved the note titled '{test_note_title}'."

    # --- Mock LLM Rules ---
    # Rule 1: Match Add Note Request -> Respond with Tool Call
    def add_note_matcher(messages, tools, tool_choice):
        last_text = get_last_message_text(messages).lower()
        # Basic check, adjust if needed for more robustness
        return f"title: {test_note_title}".lower() in last_text and "remember this note" in last_text

    add_note_tool_call = LLMOutput(
        content=llm_tool_request_text, # Optional text
        tool_calls=[
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "add_or_update_note",
                    "arguments": json.dumps( # Use json.dumps
                        {"title": test_note_title, "content": test_note_content}
                    ),
                },
            }
        ],
    )
    rule_add_note_request: Rule = (add_note_matcher, add_note_tool_call)

    # Rule 2: Match Tool Result -> Respond with Final Confirmation
    # This matcher looks for a 'tool' role message with the correct tool_call_id
    def tool_result_matcher(messages, tools, tool_choice):
        if not messages: return False
        # Check previous messages too, as history might be added before tool result
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                logger.debug(f"Tool result matcher found tool message: {msg}")
                return True
        logger.debug(f"Tool result matcher did NOT find tool message with id {tool_call_id} in {messages}")
        return False

    final_confirmation_response = LLMOutput(
        content=llm_final_confirmation_text, tool_calls=None
    )
    rule_final_confirmation: Rule = (tool_result_matcher, final_confirmation_response)

    fix.mock_llm.rules = [rule_add_note_request, rule_final_confirmation] # Order matters if matchers overlap

    # --- Mock Confirmation Manager ---
    fix.mock_confirmation_manager.request_confirmation.return_value = True # Grant confirmation

    # --- Mock Bot Response ---
    mock_final_message = AsyncMock(spec=Message, message_id=assistant_final_message_id)
    # Assume send_message is called only for the *final* confirmation in this flow
    fix.mock_bot.send_message.return_value = mock_final_message

    # --- Create Mock Update/Context ---
    update = create_mock_update(user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id)
    context = create_mock_context(fix.mock_telegram_service.application, bot_data={"processing_service": fix.processing_service})

    # Act
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():
        # 1. Confirmation Manager Call
        fix.mock_confirmation_manager.request_confirmation.assert_awaited_once()
        args_conf, kwargs_conf = fix.mock_confirmation_manager.request_confirmation.call_args
        assert_that(kwargs_conf).described_as("Confirmation Manager kwargs").contains_key("chat_id").is_equal_to(user_chat_id)
        assert_that(kwargs_conf).described_as("Confirmation Manager kwargs").contains_key("tool_name").is_equal_to("add_or_update_note")
        assert_that(kwargs_conf).described_as("Confirmation Manager kwargs").contains_key("tool_args").is_equal_to({"title": test_note_title, "content": test_note_content})

        # 2. LLM Calls
        # Expect two calls: first triggers tool, second processes result
        assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

        # Implicitly verified LLM inputs:
        # - The first call must have matched add_note_matcher to produce the tool call.
        # - The second call must have matched tool_result_matcher (which checks for the
        #   tool result message with the correct tool_call_id) to produce the final response.
        # Therefore, explicit checks on the `messages` list sent to the LLM are removed.

        # 3. Bot API Call (Final Response)
        fix.mock_bot.send_message.assert_awaited_once() # Check it was called exactly once for the final message
        args_bot, kwargs_bot = fix.mock_bot.send_message.call_args
        expected_final_escaped_text = telegramify_markdown.markdownify(llm_final_confirmation_text)
        assert_that(kwargs_bot["text"]).described_as("Final bot message text").is_equal_to(expected_final_escaped_text)
        assert_that(kwargs_bot["reply_to_message_id"]).described_as("Final bot message reply ID").is_equal_to(user_message_id)

        # 4. Database State (Note)
        async with await fix.get_db_context_func() as db:
            from family_assistant.storage.notes import get_note_by_title # Import locally
            note = await get_note_by_title(db, test_note_title)
            assert_that(note).described_as(f"Note '{test_note_title}' in DB").is_not_none()
            assert_that(note["content"]).described_as("Note content in DB").is_equal_to(test_note_content)

        # 5. Database State (Message History)
        async with await fix.get_db_context_func() as db:
            history = await get_recent_history(
                db_context=db, interface_type="telegram", conversation_id=str(user_chat_id),
                limit=10, max_age=timedelta(minutes=5)
            )
            history.sort(key=lambda x: x.get('internal_id', 0))

            assert_that(history).described_as("Message history length").is_length(4) # user, assistant_tool_request, tool_response, assistant_final

            user_msg_db, assistant_req_db, tool_res_db, assistant_final_db = history

            assert_that(user_msg_db["role"]).is_equal_to("user")
            assert_that(user_msg_db["content"]).is_equal_to(user_text)
            assert_that(user_msg_db["interface_message_id"]).is_equal_to(str(user_message_id))
            assert_that(user_msg_db["turn_id"]).is_none()
            thread_root_id = user_msg_db.get("thread_root_id")

            assert_that(assistant_req_db["role"]).is_equal_to("assistant")
            assert_that(assistant_req_db["content"]).is_equal_to(llm_tool_request_text)
            assert_that(assistant_req_db["tool_calls"][0]["id"]).is_equal_to(tool_call_id)
            assert_that(assistant_req_db["tool_calls"][0]["function"]["name"]).is_equal_to("add_or_update_note")
            turn_id = assistant_req_db["turn_id"]
            assert_that(turn_id).is_not_none()
            assert_that(assistant_req_db["thread_root_id"]).is_equal_to(thread_root_id)
            assert_that(assistant_req_db["interface_message_id"]).is_none()

            assert_that(tool_res_db["role"]).is_equal_to("tool")
            assert_that(tool_res_db["tool_call_id"]).is_equal_to(tool_call_id)
            assert_that(tool_res_db["content"]).contains(f"Note '{test_note_title}' added/updated successfully.")
            assert_that(tool_res_db["turn_id"]).is_equal_to(turn_id)
            assert_that(tool_res_db["thread_root_id"]).is_equal_to(thread_root_id)
            assert_that(tool_res_db["interface_message_id"]).is_none()

            assert_that(assistant_final_db["role"]).is_equal_to("assistant")
            assert_that(assistant_final_db["content"]).is_equal_to(llm_final_confirmation_text)
            assert_that(assistant_final_db["tool_calls"]).is_none()
            assert_that(assistant_final_db["turn_id"]).is_equal_to(turn_id)
            assert_that(assistant_final_db["thread_root_id"]).is_equal_to(thread_root_id)
            assert_that(assistant_final_db["interface_message_id"]).is_equal_to(str(assistant_final_message_id))
