"""Test that message history correctly includes recent messages when limited."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from tests.mocks.mock_llm import LLMOutput

from .conftest import TelegramHandlerTestFixture


def create_mock_update(
    message_text: str,
    chat_id: int = 123,
    user_id: int = 12345,
    message_id: int = 101,
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
    )
    update = Update(update_id=1, message=message)
    return update


def create_mock_context(
    mock_application: AsyncMock,
    chat_id: int = 123,
    user_id: int = 12345,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    context = ContextTypes.DEFAULT_TYPE(
        application=mock_application, chat_id=chat_id, user_id=user_id
    )
    return context


@pytest.mark.asyncio
async def test_message_history_includes_most_recent_when_limited(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """Test that when message history is limited, the most recent messages are included."""
    fixture = telegram_handler_fixture

    # Configure the processing service to have a very small history limit
    fixture.processing_service.service_config.max_history_messages = 3

    # Simulate a conversation with multiple back-and-forth messages
    chat_id = 123
    user_id = 12345
    context = create_mock_context(fixture.mock_application, chat_id, user_id)

    # Configure mock bot to return message IDs in sequence
    message_counter = 2  # Start at 2 (1 is user's first message)

    def mock_send_message(*args: Any, **kwargs: Any) -> Message:
        nonlocal message_counter
        msg_id = message_counter
        message_counter += 2  # Skip user message IDs
        return Message(
            message_id=msg_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
        )

    fixture.mock_bot.send_message.side_effect = mock_send_message

    # Set up LLM responses for the conversation
    # We'll use a single rule that responds differently based on the conversation state
    def dynamic_response(messages: list[dict[str, Any]]) -> LLMOutput | bool:
        # Get the last user message
        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        if not user_messages:
            return False

        last_user_msg = user_messages[-1].get("content", "")

        # Determine response based on conversation flow
        if "First message" in last_user_msg:
            return LLMOutput(content="First response from assistant")
        elif "Second message" in last_user_msg:
            return LLMOutput(content="Second response from assistant")
        elif "Third message" in last_user_msg:
            return LLMOutput(content="Third response - task completed successfully!")
        elif "New unrelated request" in last_user_msg:
            # Check if we can see the recent completion
            messages_str = str(messages)
            has_completed = "task completed successfully" in messages_str
            has_old = (
                "First message" in messages_str or "First response" in messages_str
            )

            if has_completed and not has_old:
                return LLMOutput(
                    content="I can see from our recent conversation that I successfully completed the previous task. "
                    "Now I'll help with your new request."
                )
            else:
                return LLMOutput(
                    content="I don't see the recent completion in my history. "
                    "Let me help with your new request."
                )

        return False

    # Create matcher function that always returns True
    def always_match(messages: list[dict[str, Any]], **kwargs: Any) -> bool:
        return True

    # Create rule function that returns the dynamic response
    def get_response(messages: list[dict[str, Any]], **kwargs: Any) -> LLMOutput | bool:
        return dynamic_response(messages)

    # Set up the mock LLM with a custom generate_response method
    async def mock_generate_response(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        result = dynamic_response(messages)
        if isinstance(result, LLMOutput):
            return result
        return LLMOutput(content="Default response")

    fixture.mock_llm.generate_response = mock_generate_response  # type: ignore[assignment]

    # Have the actual conversation
    # First exchange
    update1 = create_mock_update(
        "First message from user", chat_id=chat_id, message_id=1
    )
    await fixture.handler.message_handler(update1, context)

    # Second exchange
    update2 = create_mock_update(
        "Second message from user", chat_id=chat_id, message_id=3
    )
    await fixture.handler.message_handler(update2, context)

    # Third exchange - assistant completes a task
    update3 = create_mock_update(
        "Third message from user", chat_id=chat_id, message_id=5
    )
    await fixture.handler.message_handler(update3, context)

    # Clear previous mock calls to check only the final response
    fixture.mock_bot.send_message.reset_mock()
    fixture.mock_bot.send_message.side_effect = mock_send_message

    # Now send a new unrelated request
    # With max_history_messages=3, it should include the 3 most recent messages
    # and the assistant should "remember" the completed task
    update4 = create_mock_update("New unrelated request", chat_id=chat_id, message_id=7)
    await fixture.handler.message_handler(update4, context)

    # Verify the assistant's response acknowledges the completed task
    fixture.mock_bot.send_message.assert_called_once()
    call_args = fixture.mock_bot.send_message.call_args
    response_text = call_args[1]["text"]

    assert "successfully completed the previous task" in response_text
    assert "help with your new request" in response_text


@pytest.mark.asyncio
async def test_reminder_after_completed_conversation(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """Test that reminders don't resurrect old completed conversations."""
    fixture = telegram_handler_fixture

    # Configure small history limit
    fixture.processing_service.service_config.max_history_messages = 3

    chat_id = 123
    user_id = 12345
    context = create_mock_context(fixture.mock_application, chat_id, user_id)

    # Configure mock bot responses
    message_counter = 2

    def mock_send_message(*args: Any, **kwargs: Any) -> Message:
        nonlocal message_counter
        msg_id = message_counter
        message_counter += 2
        return Message(
            message_id=msg_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
        )

    fixture.mock_bot.send_message.side_effect = mock_send_message

    # Set up dynamic LLM response
    def dynamic_response(messages: list[dict[str, Any]]) -> LLMOutput | bool:
        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        if not user_messages:
            return False

        last_user_msg = user_messages[-1].get("content", "")

        if "clobbered" in last_user_msg:
            return LLMOutput(
                content="Oh dear, I apologize! Please provide the new content."
            )
        elif "new content for the note" in last_user_msg:
            return LLMOutput(
                content="I've successfully updated your note with the new content!"
            )
        elif "Reminder triggered" in last_user_msg:
            # Check conversation context
            if "water meter" in last_user_msg:
                # This is the reminder we should handle
                return LLMOutput(
                    content="Here's your reminder: Don't forget to check the water meter!"
                )
            else:
                # Fallback
                return LLMOutput(content="Here's your reminder!")

        return False

    # Set up the mock LLM with a custom generate_response method
    async def mock_generate_response(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        result = dynamic_response(messages)
        if isinstance(result, LLMOutput):
            return result
        return LLMOutput(content="Default response")

    fixture.mock_llm.generate_response = mock_generate_response  # type: ignore[assignment]

    # Simulate a conversation about updating a note
    # User complains about clobbered content
    update1 = create_mock_update(
        "You clobbered my note content!", chat_id=chat_id, message_id=1
    )
    await fixture.handler.message_handler(update1, context)

    # User provides new content
    update2 = create_mock_update(
        "Here's the new content for the note: Important information...",
        chat_id=chat_id,
        message_id=3,
    )
    await fixture.handler.message_handler(update2, context)

    # Clear previous mock calls
    fixture.mock_bot.send_message.reset_mock()
    fixture.mock_bot.send_message.side_effect = mock_send_message

    # Now process a reminder trigger
    # The assistant should handle the reminder, not continue the note conversation
    update3 = create_mock_update(
        "System: Reminder triggered\n\nThe time is now 2025-01-15 12:00:00 AEST.\n"
        "Task: Send a reminder about: check the water meter",
        chat_id=chat_id,
        message_id=5,
    )
    await fixture.handler.message_handler(update3, context)

    # Verify the response is about the reminder, not the old note conversation
    fixture.mock_bot.send_message.assert_called_once()
    response_text = fixture.mock_bot.send_message.call_args[1]["text"]

    assert "reminder" in response_text.lower()
    assert "water meter" in response_text
    # Should NOT mention the note update
    assert "note" not in response_text.lower()
    assert "updated" not in response_text.lower()
    assert "content" not in response_text.lower()
