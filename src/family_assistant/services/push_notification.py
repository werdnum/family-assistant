"""Push notification service for sending web push notifications."""

import asyncio
import json
import logging

from pywebpush import WebPushException, webpush

from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class PushNotificationService:
    """Service for sending push notifications to subscribed users."""

    def __init__(
        self,
        vapid_private_key: str | None,
        vapid_contact_email: str | None,
    ) -> None:
        """Initialize the push notification service.

        Args:
            vapid_private_key: VAPID private key for signing push messages.
            vapid_contact_email: Admin contact email for VAPID 'sub' claim.
        """
        self.vapid_private_key = vapid_private_key
        self.vapid_claims: dict[str, str | int] = {}
        if vapid_contact_email:
            # Normalize to ensure only a single 'mailto:' prefix
            if vapid_contact_email.lower().startswith("mailto:"):
                sub_claim = vapid_contact_email
            else:
                sub_claim = f"mailto:{vapid_contact_email}"
            self.vapid_claims = {"sub": sub_claim}

        self.enabled = bool(vapid_private_key and vapid_contact_email)

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
    ) -> None:
        """Send push notification to all subscriptions for a user.

        Args:
            user_identifier: The user identifier.
            title: Notification title.
            body: Notification body text.
            db_context: Database context for accessing subscriptions.
        """
        if not self.enabled:
            logger.debug(
                "Push notifications disabled - VAPID private key or contact email not configured"
            )
            return

        subscriptions = await db_context.push_subscriptions.get_by_user(user_identifier)
        if not subscriptions:
            return

        payload = json.dumps({"title": title, "body": body})

        for sub in subscriptions:
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=sub.subscription_json,
                    data=payload,
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims=self.vapid_claims,
                )
                logger.info(f"Sent push notification to user {user_identifier}")
            except WebPushException as e:
                # The pywebpush library automatically handles JSON responses
                # and raises exceptions with response objects.
                if e.response and e.response.status_code == 410:
                    logger.info(
                        f"Subscription for user {user_identifier} is stale (410 Gone). Deleting."
                    )
                    await db_context.push_subscriptions.delete(sub.id)
                else:
                    logger.warning(
                        f"Failed to send push notification to user {user_identifier}: {e}",
                    )
            except Exception as e:
                logger.error(
                    f"An unexpected error occurred while sending push notification to user {user_identifier}: {e}",
                    exc_info=True,
                )
