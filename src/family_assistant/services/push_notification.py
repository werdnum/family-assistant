"""Push notification service for sending web push notifications."""

import logging

from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class PushNotificationService:
    """Service for sending push notifications to subscribed users."""

    def __init__(
        self, vapid_private_key: str | None, vapid_public_key: str | None
    ) -> None:
        """Initialize the push notification service.

        Args:
            vapid_private_key: VAPID private key for signing push messages
            vapid_public_key: VAPID public key for browser registration
        """
        self.vapid_private_key = vapid_private_key
        self.vapid_public_key = vapid_public_key
        self.enabled = bool(vapid_private_key and vapid_public_key)

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
    ) -> None:
        """Send push notification to all subscriptions for a user.

        Args:
            user_identifier: The user identifier
            title: Notification title
            body: Notification body text
            db_context: Database context for accessing subscriptions
        """
        if not self.enabled:
            logger.debug("Push notifications disabled - no VAPID keys configured")
            return

        # TODO: Implement actual push notification sending with py-vapid
        # For now, just log
        subscriptions = await db_context.push_subscriptions.get_by_user(user_identifier)
        logger.info(
            f"Would send notification to {len(subscriptions)} subscriptions for user {user_identifier}: {title}"
        )
