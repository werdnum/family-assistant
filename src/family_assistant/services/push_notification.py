"""Push notification service for sending web push notifications."""

import asyncio
import json
import logging

from pywebpush import WebPushException, webpush

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.push_subscription import (
    PushSubscription as PushSubscriptionModel,
)

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

        # Send notifications concurrently to reduce latency for users with multiple subscriptions
        async def send_to_subscription(sub: PushSubscriptionModel) -> int | None:
            """Send notification to a single subscription, returning subscription id if 410."""
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=sub.subscription_json,
                    data=payload,
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims=self.vapid_claims,
                )
                logger.info(f"Sent push notification to user {user_identifier}")
                return None
            except WebPushException as e:
                # The pywebpush library automatically handles JSON responses
                # and raises exceptions with response objects.
                if e.response and e.response.status_code == 410:
                    logger.info(
                        f"Subscription for user {user_identifier} is stale (410 Gone). Deleting."
                    )
                    return sub.id
                else:
                    logger.warning(
                        f"Failed to send push notification to user {user_identifier}: {e}",
                    )
                    return None
            except Exception as e:
                logger.error(
                    f"An unexpected error occurred while sending push notification to user {user_identifier}: {e}",
                    exc_info=True,
                )
                return None

        # Run all sends concurrently
        stale_subscription_ids = await asyncio.gather(
            *(send_to_subscription(sub) for sub in subscriptions)
        )

        # Handle 410 (stale) subscriptions sequentially to avoid concurrent DB writes
        for sub_id in stale_subscription_ids:
            if sub_id is not None:
                await db_context.push_subscriptions.delete(sub_id)
