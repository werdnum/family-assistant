import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call  # Import call

import pytest
import pytest_asyncio
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from family_assistant.llm import LLMOutput
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.message_history import get_recent_history

# Import the fixture and its type hint
from .conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)

# --- Test Helper Functions ---


def create_mock_context(
    bot: AsyncMock, bot_data: Optional[Dict[Any, Any]] = None
) -> ContextTypes.DEFAULT_TYPE:
    """Creates a mock CallbackContext."""
    context = ContextTypes.DEFAULT_TYPE(application=MagicMock(), chat_id=123, user_id=12345)
    context._bot = bot  # Assign the mock bot
    context.bot_data = bot_data if bot_data is not None else {}
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
    llm_response_text = "Hello TestUser!"

    # Configure mock LLM response
    mock_llm_output = LLMOutput(
        content=llm_response_text, tool_calls=None, reasoning=None, error=None
    )
    # Simulate the processing service wrapping the LLM output structure
    # The generate_llm_response_for_chat returns a list of message dicts
    # Let's mock the return value of generate_llm_response_for_chat directly for simplicity
    # OR configure the mock LLM client *used by* the processing service
    turn_id = "test-turn-1"
    mock_generated_messages = [
        {
            "role": "assistant",
            "content": llm_response_text,
            "tool_calls": None,
            "tool_call_id": None,
            "reasoning_info": None,
            "error_traceback": None,
            "turn_id": turn_id, # Simulate turn_id being added
            "timestamp": datetime.now(timezone.utc), # Simulate timestamp
        }
    ]
    fix.processing_service.generate_llm_response_for_chat = AsyncMock(
        return_value=(mock_generated_messages, None, None) # (messages, reasoning, error)
    )

    # Configure mock Bot response (simulate sending message)
    mock_sent_message = MagicMock(spec=Message)
    mock_sent_message.message_id = assistant_message_id
    fix.mock_bot.send_message.return_value = mock_sent_message

    # Create mock Update and Context
    update = create_mock_update(user_text, chat_id=user_chat_id, user_id=user_id, message_id=user_message_id)
    context = create_mock_context(fix.mock_bot, bot_data={"processing_service": fix.processing_service})

    # Act
    await fix.handler.message_handler(update, context)

    # Assert
    # 1. Processing Service Call Verification (Input to the core logic)
    fix.processing_service.generate_llm_response_for_chat.assert_awaited_once()
    call_args, call_kwargs = fix.processing_service.generate_llm_response_for_chat.call_args
    assert call_kwargs.get("interface_type") == "telegram"
    assert call_kwargs.get("conversation_id") == str(user_chat_id)
    assert call_kwargs.get("trigger_content_parts") == [{"type": "text", "text": user_text}]
    assert call_kwargs.get("user_name") == "TestUser"
    assert call_kwargs.get("replied_to_interface_id") is None

    # 2. Bot API Call Verification (Output to user)
    fix.mock_bot.send_message.assert_awaited_once()
    # Check specific arguments using call_args
    args, kwargs = fix.mock_bot.send_message.call_args
    assert kwargs.get("chat_id") == user_chat_id
    # Assuming markdown conversion happens, check for escaped chars or use exact expected output
    assert llm_response_text in kwargs.get("text") # Simple check for content presence
    assert kwargs.get("reply_to_message_id") == user_message_id
    assert kwargs.get("parse_mode") is not None # Check parse mode was set
    assert kwargs.get("reply_markup") is not None # Check ForceReply was added

    # 3. Database State Verification (History saved correctly)
    async with await fix.get_db_context_func() as db:
        history = await get_recent_history(db, "telegram", str(user_chat_id), limit=10, max_age=timedelta(minutes=5))

    assert len(history) == 2, f"Expected 2 messages in history, found {len(history)}"

    user_msg = history[0] # Assuming oldest first
    assistant_msg = history[1]

    # Verify User Message
    assert user_msg["role"] == "user"
    assert user_msg["content"] == user_text
    assert user_msg["interface_type"] == "telegram"
    assert user_msg["conversation_id"] == str(user_chat_id)
    assert user_msg["interface_message_id"] == str(user_message_id)
    assert user_msg["turn_id"] is None
    assert user_msg["thread_root_id"] is None # First message in thread
    assert user_msg["tool_calls"] is None
    assert user_msg["error_traceback"] is None

    # Verify Assistant Message
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == llm_response_text
    assert assistant_msg["interface_type"] == "telegram"
    assert assistant_msg["conversation_id"] == str(user_chat_id)
    assert assistant_msg["interface_message_id"] == str(assistant_message_id) # Check if updated
    assert assistant_msg["turn_id"] == turn_id
    assert assistant_msg["thread_root_id"] is None # Should inherit from user message
    assert assistant_msg["tool_calls"] is None
    assert assistant_msg["error_traceback"] is None

