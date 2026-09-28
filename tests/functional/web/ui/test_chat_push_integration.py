"""Integration tests for WebChatInterface push notification functionality."""

import logging
from datetime import timedelta
from typing import Any, NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.services.push_notification import PushNotificationService
from family_assistant.storage.database import Database
from family_assistant.utils.clock import SystemClock
from family_assistant.web.web_chat_interface import WebChatInterface

logger = logging.getLogger(__name__)

# Test constants
TEST_PRIVATE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_CONTACT_EMAIL = "test@example.com"


@pytest.mark.parametrize("with_push_service", [False, True])
@pytest.mark.asyncio
async def test_web_chat_message_saved_successfully(
    db_engine: AsyncEngine,
    with_push_service: bool,
) -> None:
    """Test that WebChatInterface saves messages successfully regardless of push service."""
    # Arrange
    conversation_id = "test-conv-123"
    message_text = "Test message"
    service = (
        PushNotificationService(
            vapid_private_key=TEST_PRIVATE_KEY,
            vapid_contact_email=TEST_CONTACT_EMAIL,
        )
        if with_push_service
        else None
    )

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        notifier=service,
    )

    # Act
    result = await chat_interface.send_message(
        conversation_id=conversation_id,
        text=message_text,
    )

    # Assert - message should be saved even if push service doesn't find user_id
    assert result is not None

    # Verify message is in database
    db_context = Database(engine=db_engine)
    recent = await db_context.message_history.get_recent(
        interface_type="web",
        conversation_id=conversation_id,
        limit=10,
        max_age=timedelta(hours=1),
    )
    assert len(recent) == 1
    assert recent[0].content == message_text
    assert recent[0].role == "assistant"


@pytest.mark.asyncio
async def test_web_chat_no_notification_when_disabled(
    db_engine: AsyncEngine,
) -> None:
    """Test that no notification is sent when service is disabled."""
    conversation_id = "test-disabled"
    db_context = Database(engine=db_engine)
    await db_context.message_history.add_message(
        message=UserMessage(content="Hello, assistant"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=SystemClock().now(),
        user_id="test-user",
    )

    disabled_service = PushNotificationService(
        vapid_private_key=None,
        vapid_contact_email=None,
    )

    # Create mock to track if send_notification is called
    send_called = False

    async def mock_send(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        nonlocal send_called
        send_called = True

    disabled_service.send_notification = mock_send  # type: ignore[assignment]

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        notifier=disabled_service,
    )

    # Act
    await chat_interface.send_message(
        conversation_id=conversation_id,
        text="No push for this",
    )

    # Assert - send_notification should not be called when service is disabled
    assert not send_called


@pytest.mark.asyncio
async def test_web_chat_handles_push_notification_error_gracefully(
    db_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that message delivery succeeds even if push notification fails."""
    # Arrange
    conversation_id = "test-conv-error"
    db_context = Database(engine=db_engine)
    await db_context.message_history.add_message(
        message=UserMessage(content="Hello, assistant"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=SystemClock().now(),
        user_id="test-user",
    )

    # Create service that will fail
    failing_service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    send_called = False

    async def failing_send(  # pylint: disable=broad-exception-raised
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> NoReturn:
        nonlocal send_called
        send_called = True
        raise Exception("Push service is down!")

    failing_service.send_notification = failing_send  # type: ignore[assignment]

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        notifier=failing_service,
    )

    # Act
    with caplog.at_level(logging.WARNING):
        result = await chat_interface.send_message(
            conversation_id=conversation_id,
            text="Message should still be saved",
        )

    # Assert
    assert result is not None
    assert send_called
    assert any(
        record.levelno == logging.WARNING
        and "Failed to send push notification" in record.message
        for record in caplog.records
    )

    recent = await db_context.message_history.get_recent(
        interface_type="web",
        conversation_id=conversation_id,
        limit=10,
        max_age=timedelta(hours=1),
    )
    assert len(recent) == 2
    assert any(
        message.role == "assistant"
        and message.content == "Message should still be saved"
        for message in recent
    )


@pytest.mark.asyncio
async def test_web_chat_sends_push_notification_with_user_message(
    db_engine: AsyncEngine,
) -> None:
    """Test that push notification is sent when user_id is found from recent user message."""
    # Arrange
    conversation_id = "test-conv-with-user"
    user_id = "test-user-123"
    clock = SystemClock()

    # First, save a user message to establish user_id in conversation
    db_context = Database(engine=db_engine)
    await db_context.message_history.add_message(
        message=UserMessage.from_trusted_user(content="Hello, assistant"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=clock.now(),
        user_id=user_id,
    )

    # Create service and mock send_notification
    service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    send_notification_called = False
    send_notification_args = {}

    async def mock_send_notification(
        user_identifier: str,
        title: str,
        body: str,
        db_context: Any,  # noqa: ANN401
        *,
        metadata: Any = None,  # noqa: ANN401
    ) -> None:
        nonlocal send_notification_called, send_notification_args
        send_notification_called = True
        send_notification_args = {
            "user_identifier": user_identifier,
            "title": title,
            "body": body,
        }

    service.send_notification = mock_send_notification  # type: ignore[assignment]

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        notifier=service,
    )

    # Act - send assistant message
    assistant_text = "This is my response"
    result = await chat_interface.send_message(
        conversation_id=conversation_id,
        text=assistant_text,
    )

    # Assert
    assert result is not None
    assert send_notification_called
    assert send_notification_args["user_identifier"] == user_id
    assert send_notification_args["title"] == "New message"
    assert send_notification_args["body"] == assistant_text
