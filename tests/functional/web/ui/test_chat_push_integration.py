"""Integration tests for WebChatInterface push notification functionality."""

import logging
from datetime import timedelta
from typing import Any, NoReturn

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.push_notification import PushNotificationService
from family_assistant.storage.context import DatabaseContext
from family_assistant.utils.clock import SystemClock
from family_assistant.web.web_chat_interface import WebChatInterface

logger = logging.getLogger(__name__)

# Test constants
TEST_PRIVATE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_CONTACT_EMAIL = "test@example.com"


@pytest.mark.asyncio
async def test_web_chat_interface_initialized_with_push_service(
    db_engine: AsyncEngine,
) -> None:
    """Test that WebChatInterface can be initialized with push notification service."""
    # Arrange
    service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    # Act
    chat_interface = WebChatInterface(
        database_engine=db_engine,
        push_notification_service=service,
    )

    # Assert
    assert chat_interface.push_notification_service is service
    assert chat_interface.push_notification_service is not None
    assert chat_interface.push_notification_service.enabled


@pytest.mark.asyncio
async def test_web_chat_accepts_none_push_service(
    db_engine: AsyncEngine,
) -> None:
    """Test that WebChatInterface accepts None for push service."""
    # Act
    chat_interface = WebChatInterface(
        database_engine=db_engine,
        push_notification_service=None,
    )

    # Assert
    assert chat_interface.push_notification_service is None


@pytest.mark.asyncio
async def test_web_chat_message_saved_successfully(
    db_engine: AsyncEngine,
) -> None:
    """Test that WebChatInterface saves messages successfully regardless of push service."""
    # Arrange
    conversation_id = "test-conv-123"
    message_text = "Test message"
    service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        push_notification_service=service,
    )

    # Act
    result = await chat_interface.send_message(
        conversation_id=conversation_id,
        text=message_text,
    )

    # Assert - message should be saved even if push service doesn't find user_id
    assert result is not None

    # Verify message is in database
    async with DatabaseContext(engine=db_engine) as db_context:
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
    # Arrange - service with no keys (disabled)
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
        push_notification_service=disabled_service,
    )

    # Act
    await chat_interface.send_message(
        conversation_id="test-disabled",
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

    # Create service that will fail
    failing_service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    async def failing_send(  # pylint: disable=broad-exception-raised
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> NoReturn:
        raise Exception("Push service is down!")

    failing_service.send_notification = failing_send  # type: ignore[assignment]

    chat_interface = WebChatInterface(
        database_engine=db_engine,
        push_notification_service=failing_service,
    )

    # Act
    with caplog.at_level(logging.WARNING):
        result = await chat_interface.send_message(
            conversation_id=conversation_id,
            text="Message should still be saved",
        )

    # Assert
    assert result is not None  # Message was saved despite push failure

    # Verify message in database - message saved even without user_id
    async with DatabaseContext(engine=db_engine) as db_context:
        recent = await db_context.message_history.get_recent(
            interface_type="web",
            conversation_id=conversation_id,
            limit=10,
            max_age=timedelta(hours=1),
        )
        assert len(recent) == 1
        assert recent[0].content == "Message should still be saved"


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
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.message_history.add(
            interface_type="web",
            conversation_id=conversation_id,
            interface_message_id=None,
            turn_id=None,
            thread_root_id=None,
            timestamp=clock.now(),
            role="user",
            content="Hello, assistant",
            tool_calls=None,
            reasoning_info=None,
            error_traceback=None,
            tool_call_id=None,
            processing_profile_id=None,
            attachments=None,
            user_id=user_id,
        )

    # Create service and mock send_notification
    service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY,
        vapid_contact_email=TEST_CONTACT_EMAIL,
    )

    send_notification_called = False
    send_notification_args = {}

    async def mock_send_notification(  # noqa: ANN401
        user_identifier: str,
        title: str,
        body: str,
        db_context: Any,  # noqa: ANN401
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
        push_notification_service=service,
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


@pytest.mark.asyncio
async def test_web_chat_without_push_service(
    db_engine: AsyncEngine,
) -> None:
    """Test that WebChatInterface works correctly without push notification service."""
    # Arrange
    chat_interface = WebChatInterface(
        database_engine=db_engine,
        push_notification_service=None,
    )

    # Act
    result = await chat_interface.send_message(
        conversation_id="no-service",
        text="This works without push service",
    )

    # Assert
    assert result is not None

    # Verify message is saved
    async with DatabaseContext(engine=db_engine) as db_context:
        recent = await db_context.message_history.get_recent(
            interface_type="web",
            conversation_id="no-service",
            limit=10,
            max_age=timedelta(hours=1),
        )
        assert len(recent) == 1
