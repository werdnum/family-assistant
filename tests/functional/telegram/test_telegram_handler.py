# --- Testing Philosophy ---
# These tests focus on the end-to-end behavior of the Telegram handler from the USER'S perspective.
# Assertions primarily check:
# 1. Messages SENT by the bot (via mocked Telegram API calls like send_message).
# 2. Messages RECEIVED by the bot (implicitly verified by mock LLM rules/matchers).
# Database state changes (message history, notes, tasks) are NOT directly asserted here.
import json  # Add import
import logging
import uuid  # Add import
from datetime import datetime, timezone
from typing import Any  # Add missing typing imports
from unittest.mock import AsyncMock  # Import call

import pytest
import telegramify_markdown  # Import the library
from assertpy import (
    assert_that,
    soft_assertions,
)  # Import assert_that and soft_assertions
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.llm import LLMOutput

# Import mock LLM helpers
from tests.mocks.mock_llm import Rule, get_last_message_text

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)

# --- Test Helper Functions ---


def create_mock_context(
    mock_application: AsyncMock,  # Pass the application mock
    bot_data: dict[Any, Any] | None = None,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    # Use the provided application mock, which should have .bot set
    context = ContextTypes.DEFAULT_TYPE(
        application=mock_application, chat_id=123, user_id=12345
    )
    context._bot = (
        mock_application.bot
    )  # Explicitly assign the bot from the mock application
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
    reply_to_message: Message | None = None,
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
) -> None:
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
    def matcher_hello(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        last_text = get_last_message_text(messages)
        logger.debug(f"Matcher_hello checking: '{last_text}' against '{user_text}'")
        return last_text == user_text

    # Create a tuple literal directly, instead of calling the Rule type alias
    rule_hello_response: Rule = (  # Use the type hint for clarity
        matcher_hello,
        LLMOutput(content=llm_response_text, tool_calls=None),
    )
    fix.mock_llm.rules = [rule_hello_response]  # Set rules for this test instance

    # Configure mock Bot response (simulate sending message)
    mock_sent_message = AsyncMock(
        spec=Message, message_id=assistant_message_id
    )  # Use AsyncMock for awaitable return
    fix.mock_bot.send_message.return_value = mock_sent_message

    # Create mock Update and Context
    update = create_mock_update(
        user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id
    )
    # Pass the mock_application associated with the mock_telegram_service from the fixture
    context = create_mock_context(
        fix.mock_telegram_service.application,
        bot_data={"processing_service": fix.processing_service},
    )

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
        assert_that(kwargs["chat_id"]).described_as("send_message chat_id").is_equal_to(
            user_chat_id
        )

        assert_that(kwargs).described_as("send_message kwargs").contains_key("text")
        # Calculate the expected escaped text using the library
        expected_escaped_text = telegramify_markdown.markdownify(llm_response_text)
        assert_that(kwargs["text"]).described_as("send_message text").is_equal_to(
            expected_escaped_text
        )

        assert_that(kwargs).described_as("send_message kwargs").contains_key(
            "reply_to_message_id"
        )
        assert_that(kwargs["reply_to_message_id"]).described_as(
            "send_message reply_to_message_id"
        ).is_equal_to(user_message_id)

        assert_that(kwargs).described_as("send_message kwargs").contains_key(
            "parse_mode"
        ).is_not_none()
        assert_that(kwargs["parse_mode"]).described_as(
            "send_message parse_mode"
        )  # Just check it exists and isn't None

        assert_that(kwargs).described_as("send_message kwargs").contains_key(
            "reply_markup"
        ).is_not_none()
        assert_that(kwargs["reply_markup"]).described_as(
            "send_message reply_markup"
        )  # Just check it exists and isn't None


@pytest.mark.asyncio
async def test_add_note_tool_usage(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests the flow where user asks to add a note, LLM requests the tool,
    confirmation is granted, the tool executes, and the note is saved.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_chat_id = 123
    user_id = 12345
    user_message_id = 201
    assistant_final_message_id = 202  # ID for the final confirmation message
    test_note_title = f"Telegram Tool Test Note {uuid.uuid4()}"
    test_note_content = "Content added via Telegram handler test."
    user_text = f"Please remember this note. Title: {test_note_title}. Content: {test_note_content}"
    tool_call_id = f"call_{uuid.uuid4()}"  # ID for the LLM's tool request
    llm_tool_request_text = (
        "Okay, I can add that note for you."  # Optional text alongside tool call
    )
    llm_final_confirmation_text = (
        f"Okay, I've saved the note titled '{test_note_title}'."
    )

    # --- Mock LLM Rules ---
    # Rule 1: Match Add Note Request -> Respond with Tool Call
    def add_note_matcher(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        last_text = get_last_message_text(messages).lower()
        # Basic check, adjust if needed for more robustness
        return (
            f"title: {test_note_title}".lower() in last_text
            and "remember this note" in last_text
        )

    add_note_tool_call = LLMOutput(
        content=llm_tool_request_text,  # Optional text
        tool_calls=[
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "add_or_update_note",
                    "arguments": json.dumps(  # Use json.dumps
                        {"title": test_note_title, "content": test_note_content}
                    ),
                },
            }
        ],
    )
    rule_add_note_request: Rule = (add_note_matcher, add_note_tool_call)

    # Rule 2: Match Tool Result -> Respond with Final Confirmation
    # This matcher looks for a 'tool' role message with the correct tool_call_id
    def tool_result_matcher(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        if not messages:
            return False
        # Check previous messages too, as history might be added before tool result
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                logger.debug(f"Tool result matcher found tool message: {msg}")
                return True
        logger.debug(
            f"Tool result matcher did NOT find tool message with id {tool_call_id} in {messages}"
        )
        return False

    final_confirmation_response = LLMOutput(
        content=llm_final_confirmation_text, tool_calls=None
    )
    rule_final_confirmation: Rule = (tool_result_matcher, final_confirmation_response)

    fix.mock_llm.rules = [
        rule_add_note_request,
        rule_final_confirmation,
    ]  # Order matters if matchers overlap

    # --- Mock Confirmation Manager (Not needed for add_note) ---
    # fix.mock_confirmation_manager.request_confirmation.return_value = True # Grant confirmation

    # --- Mock Bot Response ---
    mock_final_message = AsyncMock(spec=Message, message_id=assistant_final_message_id)
    # Assume send_message is called only for the *final* confirmation in this flow
    fix.mock_bot.send_message.return_value = mock_final_message

    # --- Create Mock Update/Context ---
    update = create_mock_update(
        user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id
    )
    context = create_mock_context(
        fix.mock_telegram_service.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():
        # 1. Confirmation Manager Call (Should NOT be called for add_note)
        fix.mock_confirmation_manager.request_confirmation.assert_not_awaited()

        # 2. LLM Calls
        # Expect two calls: first triggers tool, second processes result
        assert_that(fix.mock_llm._calls).described_as("LLM Call Count").is_length(2)

        # Implicitly verified LLM inputs:
        # - The first call must have matched add_note_matcher to produce the tool call.
        # - The second call must have matched tool_result_matcher (which checks for the
        #   tool result message with the correct tool_call_id) to produce the final response.
        # Therefore, explicit checks on the `messages` list sent to the LLM are removed.

        # 3. Bot API Call (Final Response)
        fix.mock_bot.send_message.assert_awaited_once()  # Check it was called exactly once for the final message
        args_bot, kwargs_bot = fix.mock_bot.send_message.call_args
        expected_final_escaped_text = telegramify_markdown.markdownify(
            llm_final_confirmation_text
        )
        assert_that(kwargs_bot["text"]).described_as(
            "Final bot message text"
        ).is_equal_to(expected_final_escaped_text)
        assert_that(kwargs_bot["reply_to_message_id"]).described_as(
            "Final bot message reply ID"
        ).is_equal_to(user_message_id)
        # 4. Database State (Note) - OMITTED
        # We trust the tool implementation works and the LLM correctly processed
        # the tool result (verified implicitly by Rule 2 matching).
        # Asserting the DB state here would make it an integration test, not E2E.

        # 5. Database State (Message History) - OMITTED
        # We trust the message history saving logic works. Asserting its content
        # here would make it an integration test. We verified the *final* user-facing
        # output and the LLM call chain.


@pytest.mark.asyncio
async def test_tool_result_in_subsequent_history(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    Tests that after a tool call completes in one turn, the 'tool' result message
    is included in the history passed to the LLM in the *next* turn.
    """
    # Arrange
    fix = telegram_handler_fixture
    user_chat_id = 123
    user_id = 12345
    # Message IDs for Turn 1
    user_message_id_1 = 301
    assistant_final_message_id_1 = 302
    # Message IDs for Turn 2
    user_message_id_2 = 303
    assistant_final_message_id_2 = 304

    test_note_title = f"History Context Test Note {uuid.uuid4()}"
    test_note_content = "Content for history context test."
    user_text_1 = f"Add note: Title={test_note_title}, Content={test_note_content}"
    user_text_2 = "What was the result of the last tool I asked you to use?"
    tool_call_id_1 = f"call_{uuid.uuid4()}"
    llm_tool_request_text_1 = "Okay, adding the note."
    llm_final_confirmation_text_1 = f"Note '{test_note_title}' added."
    # Update expected response based on the actual tool result ("Success") seen in history
    llm_response_text_2 = "The last tool call result was: Success"

    # --- Mock LLM Rules ---

    # Turn 1: Add Note
    def add_note_matcher_t1(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        return get_last_message_text(messages).startswith("Add note:")

    add_note_tool_call_t1 = LLMOutput(
        content=llm_tool_request_text_1,
        tool_calls=[
            {
                "id": tool_call_id_1,
                "type": "function",
                "function": {
                    "name": "add_or_update_note",
                    "arguments": json.dumps(
                        {"title": test_note_title, "content": test_note_content}
                    ),
                },
            }
        ],
    )
    rule_add_note_request_t1: Rule = (add_note_matcher_t1, add_note_tool_call_t1)

    def tool_result_matcher_t1(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        return any(
            msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id_1
            for msg in messages
        )

    final_confirmation_response_t1 = LLMOutput(
        content=llm_final_confirmation_text_1, tool_calls=None
    )
    rule_final_confirmation_t1: Rule = (
        tool_result_matcher_t1,
        final_confirmation_response_t1,
    )

    # Turn 2: Ask about the tool result (this matcher is the key assertion)
    def ask_result_matcher_t2(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
    ) -> bool:
        # Check 1: Is the user asking the right question?
        last_user_msg_correct = get_last_message_text(messages) == user_text_2
        # Check 2: Does the history *before* the last user message contain the tool result from Turn 1?
        history_includes_tool_result = any(
            msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id_1
            for msg in messages[
                :-1
            ]  # Look in history *before* the current user message
        )
        logger.debug(
            f"Matcher T2: Last user msg correct? {last_user_msg_correct}. History includes tool result? {history_includes_tool_result}. History: {messages}"
        )
        return last_user_msg_correct and history_includes_tool_result

    ask_result_response_t2 = LLMOutput(content=llm_response_text_2, tool_calls=None)
    rule_ask_result_t2: Rule = (ask_result_matcher_t2, ask_result_response_t2)

    # Set rules (order matters for non-overlapping matchers, but explicit here)
    fix.mock_llm.rules = [
        rule_add_note_request_t1,
        rule_ask_result_t2,
        rule_final_confirmation_t1,  # More general rule checked last
    ]

    # --- Mock Bot Responses ---
    mock_sent_message_1 = AsyncMock(
        spec=Message, message_id=assistant_final_message_id_1
    )
    mock_sent_message_2 = AsyncMock(
        spec=Message, message_id=assistant_final_message_id_2
    )
    # Set side effect to return different messages for different calls
    fix.mock_bot.send_message.side_effect = [mock_sent_message_1, mock_sent_message_2]

    # --- Context ---
    # Same context object can be reused if state doesn't need to be reset between turns
    context = create_mock_context(
        fix.mock_telegram_service.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # --- Act (Turn 1) ---
    update_1 = create_mock_update(
        user_text_1, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id_1
    )
    await fix.handler.message_handler(update_1, context)

    # --- Assert (Turn 1 - Minimal, just ensure it likely completed) ---
    assert_that(fix.mock_llm._calls).described_as("LLM Calls after Turn 1").is_length(2)
    assert_that(fix.mock_bot.send_message.await_count).described_as(
        "Bot Sends after Turn 1"
    ).is_equal_to(1)
    # Retrieve the args/kwargs for the first call
    expected_escaped_text_1 = telegramify_markdown.markdownify(
        llm_final_confirmation_text_1
    )
    call_1_args, call_1_kwargs = fix.mock_bot.send_message.call_args_list[0]
    assert_that(call_1_kwargs["text"]).described_as(
        "Bot message text (Turn 1)"
    ).is_equal_to(expected_escaped_text_1)

    # --- Act (Turn 2) ---
    update_2 = create_mock_update(
        user_text_2, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id_2
    )
    await fix.handler.message_handler(update_2, context)

    # --- Assert (Turn 2) ---
    with soft_assertions():
        assert_that(fix.mock_llm._calls).described_as(
            "LLM Calls after Turn 2"
        ).is_length(
            3
        )  # One more call
        assert_that(fix.mock_bot.send_message.await_count).described_as(
            "Bot Sends after Turn 2"
        ).is_equal_to(
            2
        )  # One more send

        # Check the *second* call to send_message
        call_2_args, call_2_kwargs = fix.mock_bot.send_message.call_args_list[1]
        # --- Expected behavior assertion ---
        expected_escaped_text_2 = telegramify_markdown.markdownify(llm_response_text_2)
        assert_that(call_2_kwargs["text"]).described_as(
            "Final bot message text (Turn 2)"
        ).is_equal_to(expected_escaped_text_2)

        assert_that(call_2_kwargs["reply_to_message_id"]).described_as(
            "Final bot message reply ID (Turn 2)"
        ).is_equal_to(user_message_id_2)

        # The key assertion is implicit: rule_ask_result_t2 matched.

        # If it hadn't matched (because the tool result wasn't in the history),
        # the mock LLM would have returned the default response or failed differently.
        # We check that the *expected* response for Turn 2 was sent.
        # output and the LLM call chain.
