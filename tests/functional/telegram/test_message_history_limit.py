"""Test that message history correctly includes recent messages when limited."""

from datetime import UTC, datetime
from typing import Any

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import Application, ContextTypes

from family_assistant.llm.messages import TextContentPart
from tests.mocks.mock_llm import LLMOutput

from .conftest import TelegramHandlerTestFixture
from .helpers import wait_for_bot_response


def extract_text_from_content(content: str | list[Any] | None) -> str:
    """Extract text content from message content (handles both str and list of ContentPart)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Handle list of content parts
    text_parts = []
    for part in content:
        if isinstance(part, TextContentPart) or hasattr(part, "text"):
            text_parts.append(part.text)
    return " ".join(text_parts)


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
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=message_text,
    )
    update = Update(update_id=1, message=message)
    return update


def create_context(
    application: Application[Any, Any, Any, Any, Any, Any],
    chat_id: int = 123,
    user_id: int = 12345,
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a CallbackContext from an Application."""
    context = ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=chat_id, user_id=user_id
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
    context = create_context(fixture.application, chat_id, user_id)

    # Set up LLM responses for the conversation
    # We'll use a single rule that responds differently based on the conversation state
    def dynamic_response(messages: Any) -> LLMOutput | bool:  # noqa: ANN401 - LLM message list has complex nested types
        # Get the last user message
        user_messages = [msg for msg in messages if msg.role == "user"]
        if not user_messages:
            return False

        last_user_msg = extract_text_from_content(user_messages[-1].content)

        # Determine response based on conversation flow
        if "First message" in last_user_msg:
            return LLMOutput(content="First response from assistant")
        elif "Second message" in last_user_msg:
            return LLMOutput(content="Second response from assistant")
        elif "Third message" in last_user_msg:
            return LLMOutput(content="Third response - task completed successfully!")
        elif "New unrelated request" in last_user_msg:
            # Check if we can see the recent completion by examining all message contents
            all_text = " ".join(
                extract_text_from_content(msg.content)
                for msg in messages
                if hasattr(msg, "content")
            )
            has_completed = "task completed successfully" in all_text
            has_old = "First message" in all_text or "First response" in all_text

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

    # Set up the mock LLM with a custom generate_response method
    async def mock_generate_response(
        messages: Any,  # noqa: ANN401 - LLM message list has complex nested types
        # ast-grep-ignore: no-dict-any - Matches LLM interface signature
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        result = dynamic_response(messages)
        if isinstance(result, LLMOutput):
            return result
        return LLMOutput(content="Default response")

    fixture.mock_llm.generate_response = mock_generate_response  # type: ignore[assignment] - Replacing method for test

    # Have the actual conversation
    # First exchange
    update1 = create_mock_update(
        "First message from user", chat_id=chat_id, message_id=101
    )
    await fixture.handler.message_handler(update1, context)
    await wait_for_bot_response(fixture.telegram_client)

    # Second exchange
    update2 = create_mock_update(
        "Second message from user", chat_id=chat_id, message_id=103
    )
    await fixture.handler.message_handler(update2, context)
    await wait_for_bot_response(fixture.telegram_client)

    # Third exchange - assistant completes a task
    update3 = create_mock_update(
        "Third message from user", chat_id=chat_id, message_id=105
    )
    await fixture.handler.message_handler(update3, context)
    await wait_for_bot_response(fixture.telegram_client)

    # Now send a new unrelated request
    # With max_history_messages=3, it should include the 3 most recent messages
    # and the assistant should "remember" the completed task
    update4 = create_mock_update(
        "New unrelated request", chat_id=chat_id, message_id=107
    )
    await fixture.handler.message_handler(update4, context)

    # Verify the assistant's response acknowledges the completed task
    bot_responses = await wait_for_bot_response(fixture.telegram_client, timeout=5.0)
    assert len(bot_responses) >= 4, (
        f"Expected at least 4 bot responses, got {len(bot_responses)}"
    )

    # Find the response that acknowledges the completed task (could be in any position
    # depending on how the mock server orders responses)
    found_correct_response = False
    for resp in bot_responses:
        text = resp.get("message", {}).get("text", "")
        if (
            "successfully completed the previous task" in text
            and "help with your new request" in text
        ):
            found_correct_response = True
            break

    assert found_correct_response, (
        f"Expected a response acknowledging the completed task, but got: "
        f"{[r.get('message', {}).get('text', '') for r in bot_responses]}"
    )


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
    context = create_context(fixture.application, chat_id, user_id)

    # Set up dynamic LLM response
    def dynamic_response(messages: Any) -> LLMOutput | bool:  # noqa: ANN401
        user_messages = [msg for msg in messages if msg.role == "user"]
        if not user_messages:
            return False

        last_user_msg = extract_text_from_content(user_messages[-1].content)

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
        messages: Any,  # noqa: ANN401 - LLM message list has complex nested types
        # ast-grep-ignore: no-dict-any - Matches LLM interface signature
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        result = dynamic_response(messages)
        if isinstance(result, LLMOutput):
            return result
        return LLMOutput(content="Default response")

    fixture.mock_llm.generate_response = mock_generate_response  # type: ignore[assignment] - Replacing method for test

    # Simulate a conversation about updating a note
    # User complains about clobbered content
    update1 = create_mock_update(
        "You clobbered my note content!", chat_id=chat_id, message_id=201
    )
    await fixture.handler.message_handler(update1, context)
    await wait_for_bot_response(fixture.telegram_client)

    # User provides new content
    update2 = create_mock_update(
        "Here's the new content for the note: Important information...",
        chat_id=chat_id,
        message_id=203,
    )
    await fixture.handler.message_handler(update2, context)
    await wait_for_bot_response(fixture.telegram_client)

    # Now process a reminder trigger
    # The assistant should handle the reminder, not continue the note conversation
    update3 = create_mock_update(
        "System: Reminder triggered\n\nThe time is now 2025-01-15 12:00:00 AEST.\n"
        "Task: Send a reminder about: check the water meter",
        chat_id=chat_id,
        message_id=205,
    )
    await fixture.handler.message_handler(update3, context)

    # Verify the response is about the reminder, not the old note conversation
    bot_responses = await wait_for_bot_response(fixture.telegram_client, timeout=5.0)
    assert len(bot_responses) >= 3, (
        f"Expected at least 3 bot responses, got {len(bot_responses)}"
    )

    # Find the reminder response (could be in any position depending on mock server ordering)
    reminder_response = None
    for resp in bot_responses:
        text = resp.get("message", {}).get("text", "")
        if "reminder" in text.lower() and "water meter" in text:
            reminder_response = text
            break

    assert reminder_response is not None, (
        f"Expected a reminder response about water meter, but got: "
        f"{[r.get('message', {}).get('text', '') for r in bot_responses]}"
    )
    # Should NOT mention the note update
    assert "note" not in reminder_response.lower()
    assert "updated" not in reminder_response.lower()
    assert "content" not in reminder_response.lower()
