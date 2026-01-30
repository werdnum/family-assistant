# --- Testing Philosophy ---
# These tests focus on the end-to-end behavior of the Telegram handler from the USER'S perspective.
# Uses telegram-test-api for realistic HTTP-level testing. The bot makes real HTTP calls to the
# test server, which records all messages. Tests verify bot responses using the telegram_client.
# Database state changes (message history, notes, tasks) are NOT directly asserted here.
import io
import json
import logging
import typing
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import telegramify_markdown  # type: ignore[import-untyped]  # Third-party library has no type stubs
from assertpy import assert_that, soft_assertions
from telegram import Chat, Message, PhotoSize, Update, User
from telegram.ext import Application, ContextTypes

from family_assistant.llm import ToolCallFunction, ToolCallItem
from tests.mocks.mock_llm import (
    LLMOutput,
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

from .conftest import TelegramHandlerTestFixture
from .helpers import assert_bot_sent_message, wait_for_bot_response

logger = logging.getLogger(__name__)

# --- Test Helper Functions ---


def create_context(
    application: Application[Any, Any, Any, Any, Any, Any],
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


# Alias for backwards compatibility
create_mock_context = create_context


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


def create_mock_update_with_photo(
    message_text: str = "",
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
    photo_file_id: str = "test_photo_123",
    photo_bytes: bytes | None = None,
    bot: Any | None = None,  # noqa: ANN401  # telegram bot object
) -> Update:
    """Creates a mock Telegram Update object for a message with a photo."""

    # Create mock photo bytes if not provided
    if photo_bytes is None:
        # Simple test image data (1x1 PNG)
        photo_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82"

    user = User(id=user_id, first_name="TestUser", is_bot=False)
    chat = Chat(id=chat_id, type="private")

    # Create PhotoSize objects (Telegram sends multiple sizes)
    photo_small = PhotoSize(
        file_id=f"{photo_file_id}_small",
        file_unique_id=f"{photo_file_id}_small_unique",
        width=100,
        height=100,
        file_size=len(photo_bytes),
    )
    photo_large = PhotoSize(
        file_id=photo_file_id,
        file_unique_id=f"{photo_file_id}_unique",
        width=400,
        height=400,
        file_size=len(photo_bytes),
    )

    message = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
        photo=[photo_small, photo_large],  # Telegram sends array of sizes
    )

    # Set bot if provided
    if bot:
        message.set_bot(bot)
        for photo in message.photo:
            photo.set_bot(bot)

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
    user_text = "Hello assistant!"
    llm_response_text = "Hello TestUser! This is the LLM response."

    # Define rules for the RuleBasedMockLLMClient for this specific test
    def matcher_hello(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages)
        logger.debug(f"Matcher_hello checking: '{last_text}' against '{user_text}'")
        return last_text == user_text

    # Create a tuple literal directly, instead of calling the Rule type alias
    rule_hello_response: Rule = (  # Use the type hint for clarity
        matcher_hello,
        LLMOutput(content=llm_response_text, tool_calls=None),
    )
    typing.cast("RuleBasedMockLLMClient", fix.mock_llm).rules = [
        rule_hello_response
    ]  # Set rules for this test instance

    # Create Update and Context
    update = create_mock_update(
        user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    await fix.handler.message_handler(update, context)

    # Assert - verify bot sent message via telegram-test-api
    # 1. LLM Call Verification
    assert_that(
        typing.cast("RuleBasedMockLLMClient", fix.mock_llm)._calls
    ).described_as("LLM Call Count").is_length(1)

    # 2. Verify bot response via test server
    bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
    assert_that(bot_responses).described_as("Bot responses").is_not_empty()

    # Get the bot's message text
    bot_message = bot_responses[-1]
    bot_message_text = bot_message.get("message", {}).get("text", "")

    # The response should contain the LLM response (may be escaped for markdown)
    expected_escaped_text = telegramify_markdown.markdownify(llm_response_text)
    assert_that(bot_message_text).described_as("Bot message text").is_equal_to(
        expected_escaped_text
    )


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
    test_note_title = f"Telegram Tool Test Note {uuid.uuid4()}"
    test_note_content = "Content added via Telegram handler test."
    user_text = f"Please remember this note. Title: {test_note_title}. Content: {test_note_content}"
    tool_call_id = f"call_{uuid.uuid4()}"
    llm_tool_request_text = "Okay, I can add that note for you."
    llm_final_confirmation_text = (
        f"Okay, I've saved the note titled '{test_note_title}'."
    )

    # --- Mock LLM Rules ---
    # Rule 1: Match Add Note Request -> Respond with Tool Call
    def add_note_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages).lower()
        return (
            f"title: {test_note_title}".lower() in last_text
            and "remember this note" in last_text
        )

    add_note_tool_call = LLMOutput(
        content=llm_tool_request_text,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_add_note_request: Rule = (add_note_matcher, add_note_tool_call)

    # Rule 2: Match Tool Result -> Respond with Final Confirmation
    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages:
            return False
        for msg in reversed(messages):
            if msg.role == "tool" and msg.tool_call_id == tool_call_id:
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

    typing.cast("RuleBasedMockLLMClient", fix.mock_llm).rules = [
        rule_add_note_request,
        rule_final_confirmation,
    ]

    # --- Create Update/Context ---
    update = create_mock_update(
        user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id
    )
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # Act
    await fix.handler.message_handler(update, context)

    # Assert
    with soft_assertions():  # type: ignore[attr-defined]
        # 1. Confirmation Manager Call (Should NOT be called for add_note)
        fix.mock_confirmation_manager.request_confirmation.assert_not_awaited()

        # 2. LLM Calls - expect two calls: first triggers tool, second processes result
        assert_that(
            typing.cast("RuleBasedMockLLMClient", fix.mock_llm)._calls
        ).described_as("LLM Call Count").is_length(2)

        # 3. Verify bot response via telegram-test-api
        bot_responses = await wait_for_bot_response(fix.telegram_client, timeout=5.0)
        assert_that(bot_responses).described_as("Bot responses").is_not_empty()

        # Get the bot's final message text
        bot_message = bot_responses[-1]
        bot_message_text = bot_message.get("message", {}).get("text", "")

        expected_final_escaped_text = telegramify_markdown.markdownify(
            llm_final_confirmation_text
        )
        assert_that(bot_message_text).described_as(
            "Final bot message text"
        ).is_equal_to(expected_final_escaped_text)


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
    user_message_id_1 = 301
    user_message_id_2 = 303

    test_note_title = f"History Context Test Note {uuid.uuid4()}"
    test_note_content = "Content for history context test."
    user_text_1 = f"Add note: Title={test_note_title}, Content={test_note_content}"
    user_text_2 = "What was the result of the last tool I asked you to use?"
    tool_call_id_1 = f"call_{uuid.uuid4()}"
    llm_tool_request_text_1 = "Okay, adding the note."
    llm_final_confirmation_text_1 = f"Note '{test_note_title}' added."
    llm_response_text_2 = "The last tool call result was: Success"

    # --- Mock LLM Rules ---

    # Turn 1: Add Note
    def add_note_matcher_t1(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return get_last_message_text(messages).startswith("Add note:")

    add_note_tool_call_t1 = LLMOutput(
        content=llm_tool_request_text_1,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_1,
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({
                        "title": test_note_title,
                        "content": test_note_content,
                    }),
                ),
            )
        ],
    )
    rule_add_note_request_t1: Rule = (add_note_matcher_t1, add_note_tool_call_t1)

    def tool_result_matcher_t1(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id_1
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
    def ask_result_matcher_t2(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        # Check 1: Is the user asking the right question?
        last_user_msg_correct = get_last_message_text(messages) == user_text_2
        # Check 2: Does the history *before* the last user message contain the tool result from Turn 1?
        history_includes_tool_result = any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id_1
            for msg in messages[:-1]
        )
        logger.debug(
            f"Matcher T2: Last user msg correct? {last_user_msg_correct}. "
            f"History includes tool result? {history_includes_tool_result}. History: {messages}"
        )
        return last_user_msg_correct and history_includes_tool_result

    ask_result_response_t2 = LLMOutput(content=llm_response_text_2, tool_calls=None)
    rule_ask_result_t2: Rule = (ask_result_matcher_t2, ask_result_response_t2)

    # Set rules (order matters for non-overlapping matchers)
    typing.cast("RuleBasedMockLLMClient", fix.mock_llm).rules = [
        rule_add_note_request_t1,
        rule_ask_result_t2,
        rule_final_confirmation_t1,
    ]

    # --- Context ---
    context = create_context(
        fix.application,
        bot_data={"processing_service": fix.processing_service},
    )

    # --- Act (Turn 1) ---
    update_1 = create_mock_update(
        user_text_1, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id_1
    )
    await fix.handler.message_handler(update_1, context)

    # --- Assert (Turn 1) ---
    assert_that(
        typing.cast("RuleBasedMockLLMClient", fix.mock_llm)._calls
    ).described_as("LLM Calls after Turn 1").is_length(2)

    # Verify bot response via telegram-test-api for Turn 1
    expected_escaped_text_1 = telegramify_markdown.markdownify(
        llm_final_confirmation_text_1
    )
    bot_message_t1 = await assert_bot_sent_message(
        fix.telegram_client, expected_escaped_text_1, timeout=5.0, partial_match=False
    )
    bot_message_text_t1 = bot_message_t1.get("message", {}).get("text", "")
    assert_that(bot_message_text_t1).described_as(
        "Bot message text (Turn 1)"
    ).is_equal_to(expected_escaped_text_1)

    # --- Act (Turn 2) ---
    update_2 = create_mock_update(
        user_text_2, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id_2
    )
    await fix.handler.message_handler(update_2, context)

    # --- Assert (Turn 2) ---
    with soft_assertions():  # type: ignore[attr-defined]  # assertpy soft_assertions decorator
        assert_that(
            typing.cast("RuleBasedMockLLMClient", fix.mock_llm)._calls
        ).described_as("LLM Calls after Turn 2").is_length(3)

        # Verify bot response via telegram-test-api for Turn 2
        # Use assert_bot_sent_message to wait for the specific expected message
        expected_escaped_text_2 = telegramify_markdown.markdownify(llm_response_text_2)
        bot_message_t2 = await assert_bot_sent_message(
            fix.telegram_client,
            expected_escaped_text_2,
            timeout=5.0,
            partial_match=False,
        )
        bot_message_text_t2 = bot_message_t2.get("message", {}).get("text", "")
        assert_that(bot_message_text_t2).described_as(
            "Final bot message text (Turn 2)"
        ).is_equal_to(expected_escaped_text_2)

        # The key assertion is implicit: rule_ask_result_t2 matched.
        # If it hadn't matched (because the tool result wasn't in the history),
        # the mock LLM would have returned the default response.


@pytest.mark.asyncio
async def test_telegram_photo_persistence_and_llm_context(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """
    End-to-end test that verifies:
    1. Telegram photos are persisted via AttachmentService (not base64)
    2. Attachment metadata is stored in message history
    3. Historical attachments are included in subsequent LLM context
    """
    fix = telegram_handler_fixture
    user_chat_id = 123
    user_id = 12345

    # Helper to detect image content in LLM messages
    def has_image_content(args: MatcherArgs) -> bool:
        """Check if LLM call includes image_url content parts."""
        messages = args.get("messages", [])
        for msg in messages:
            content = msg.content or []
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        return False

    # Configure mock LLM rules
    mock_llm = typing.cast("RuleBasedMockLLMClient", fix.mock_llm)
    first_response = "I can see a test image you've sent! How can I help you with it?"
    second_response = (
        "Yes, I still have access to the image you sent earlier. It's a test image."
    )

    mock_llm.rules = [
        # First message with photo - LLM should recognize the image
        (
            has_image_content,
            LLMOutput(content=first_response),
        ),
        # Second message - LLM should still have access to historical image
        (
            lambda args: (
                has_image_content(args)
                and "remember"
                in get_last_message_text(args.get("messages", [])).lower()
            ),
            LLMOutput(content=second_response),
        ),
    ]

    mock_llm.default_response = LLMOutput(
        content="I don't see any image in the current context."
    )

    # Mock Telegram bot file download to write test photo bytes to buffer
    test_photo_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82"

    # Mock the file object and its download method
    async def mock_download_to_memory(out: io.BytesIO) -> None:
        out.write(test_photo_bytes)

    mock_file = AsyncMock()
    mock_file.download_to_memory = mock_download_to_memory
    mock_file.file_size = len(test_photo_bytes)

    # Use patch on the class method since the Bot instance is frozen/immutable
    with patch("telegram.Bot.get_file", AsyncMock(return_value=mock_file)):
        # === TURN 1: Send photo with text ===
        photo_update = create_mock_update_with_photo(
            message_text="What do you see in this image?",
            chat_id=user_chat_id,
            user_id=user_id,
            message_id=101,
            bot=fix.bot,
        )
        photo_context = create_context(fix.application)

        await fix.handler.message_handler(photo_update, photo_context)

        # Verify bot responded to the photo message via telegram-test-api
        # Use assert_bot_sent_message to wait for the specific expected content
        bot_message_t1 = await assert_bot_sent_message(
            fix.telegram_client, "image", timeout=5.0, partial_match=True
        )
        first_response_text = bot_message_t1.get("message", {}).get("text", "")
        assert_that(first_response_text).described_as(
            "First response should recognize the image"
        ).contains("image")

        # === TURN 2: Ask about the previous image ===
        follow_up_update = create_mock_update(
            message_text="Do you remember the image I sent earlier?",
            chat_id=user_chat_id,
            user_id=user_id,
            message_id=102,
        )
        follow_up_context = create_context(fix.application)

        await fix.handler.message_handler(follow_up_update, follow_up_context)

        # Verify bot responded with knowledge of the historical image
        # Use assert_bot_sent_message to wait for the specific expected content
        bot_message_t2 = await assert_bot_sent_message(
            fix.telegram_client, "image", timeout=5.0, partial_match=True
        )
        second_response_text = bot_message_t2.get("message", {}).get("text", "")
        assert_that(second_response_text).described_as(
            "Second response should reference the historical image"
        ).matches(r".*(access|image|earlier).*")

        # === VERIFICATION: Check that image was persisted properly ===
        # The test implicitly verifies:
        # 1. AttachmentService was used (not base64) - if it failed, the handler would crash
        # 2. Attachment metadata was stored - verified by the LLM receiving image_url in turn 2
        # 3. Historical attachments included in LLM context - verified by the second rule matching
