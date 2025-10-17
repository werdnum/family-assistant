"""Unit tests for the PushNotificationService."""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from pywebpush import WebPushException
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.push_notification import PushNotificationService
from family_assistant.storage.context import DatabaseContext

# Dummy VAPID keys for testing
TEST_PRIVATE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TEST_CONTACT_EMAIL = "tester@example.com"


def test_service_disabled_with_no_key() -> None:
    """Test that the service is disabled when no private key is provided."""
    service = PushNotificationService(
        vapid_private_key=None, vapid_contact_email=TEST_CONTACT_EMAIL
    )
    assert not service.enabled


def test_service_disabled_with_no_email() -> None:
    """Test that the service is disabled when no contact email is provided."""
    service = PushNotificationService(
        vapid_private_key=TEST_PRIVATE_KEY, vapid_contact_email=None
    )
    assert not service.enabled


@pytest.mark.asyncio
async def test_send_notification_disabled(
    db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that send_notification does nothing if the service is disabled."""
    service = PushNotificationService(vapid_private_key=None, vapid_contact_email=None)
    async with DatabaseContext(engine=db_engine) as db_context:
        with caplog.at_level(logging.DEBUG):
            await service.send_notification("user1", "title", "body", db_context)
            assert "Push notifications disabled" in caplog.text


@pytest.mark.asyncio
@patch("family_assistant.services.push_notification.webpush", autospec=True)
async def test_send_notification_success(
    mock_webpush: MagicMock, db_engine: AsyncEngine
) -> None:
    """Test a successful push notification send."""
    sub_json = json.dumps({
        "endpoint": "https://example.com/push",
        "keys": {"p256dh": "key", "auth": "auth_key"},
    })

    async with DatabaseContext(engine=db_engine) as db_context:
        # Create subscription in database
        await db_context.push_subscriptions.add(
            user_identifier="user1",
            subscription_json=json.loads(sub_json),
        )

        service = PushNotificationService(
            vapid_private_key=TEST_PRIVATE_KEY, vapid_contact_email=TEST_CONTACT_EMAIL
        )
        await service.send_notification("user1", "Test Title", "Test Body", db_context)

        mock_webpush.assert_called_once()
        call_args = mock_webpush.call_args
        assert call_args.kwargs["subscription_info"] == json.loads(sub_json)
        assert call_args.kwargs["vapid_private_key"] == TEST_PRIVATE_KEY
        assert call_args.kwargs["vapid_claims"] == {
            "sub": f"mailto:{TEST_CONTACT_EMAIL}"
        }
        sent_payload = json.loads(call_args.kwargs["data"])
        assert sent_payload["title"] == "Test Title"
        assert sent_payload["body"] == "Test Body"


@pytest.mark.asyncio
@patch("family_assistant.services.push_notification.webpush", autospec=True)
async def test_handle_stale_subscription_410_gone(
    mock_webpush: MagicMock, db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that a 410 Gone response deletes the subscription."""
    mock_response = MagicMock()
    mock_response.status_code = 410
    mock_webpush.side_effect = WebPushException("Gone", response=mock_response)

    async with DatabaseContext(engine=db_engine) as db_context:
        # Create subscription in database
        await db_context.push_subscriptions.add(
            user_identifier="user1",
            subscription_json={"endpoint": "stale"},
        )

        service = PushNotificationService(
            vapid_private_key=TEST_PRIVATE_KEY, vapid_contact_email=TEST_CONTACT_EMAIL
        )
        with caplog.at_level(logging.INFO):
            await service.send_notification("user1", "title", "body", db_context)
            assert (
                "Subscription for user user1 is stale (410 Gone). Deleting."
                in caplog.text
            )

        mock_webpush.assert_called_once()

        # Verify subscription was deleted
        subscriptions = await db_context.push_subscriptions.get_by_user("user1")
        assert len(subscriptions) == 0


@pytest.mark.asyncio
@patch("family_assistant.services.push_notification.webpush", autospec=True)
async def test_handle_other_web_push_exception(
    mock_webpush: MagicMock, db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that other WebPushExceptions are logged but do not delete the subscription."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_webpush.side_effect = WebPushException("Not Found", response=mock_response)

    async with DatabaseContext(engine=db_engine) as db_context:
        # Create subscription in database
        await db_context.push_subscriptions.add(
            user_identifier="user1",
            subscription_json={"endpoint": "not-found"},
        )

        service = PushNotificationService(
            vapid_private_key=TEST_PRIVATE_KEY, vapid_contact_email=TEST_CONTACT_EMAIL
        )
        with caplog.at_level(logging.WARNING):
            await service.send_notification("user1", "title", "body", db_context)
            assert "Failed to send push notification" in caplog.text

        mock_webpush.assert_called_once()

        # Verify subscription was NOT deleted
        subscriptions = await db_context.push_subscriptions.get_by_user("user1")
        assert len(subscriptions) == 1
