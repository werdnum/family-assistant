"""Fan-out dispatcher for user notifications across delivery channels.

Combines the browser Web Push channel (:class:`PushNotificationService`) and the iOS APNs channel
(:class:`APNsService`) behind a single interface so callers dispatch one logical notification that
reaches every channel a user is subscribed to. The interface mirrors the individual services
(``enabled`` and ``send_notification(user_identifier, title, body, db_context)``) so it is a
drop-in wherever a single push service was previously used.
"""

import asyncio
import logging

from family_assistant.services.apns import APNsService
from family_assistant.services.push_notification import PushNotificationService
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """Dispatches notifications to all configured channels for a user."""

    def __init__(
        self,
        *,
        web_push: PushNotificationService | None = None,
        apns: APNsService | None = None,
    ) -> None:
        """Initialize the dispatcher.

        Args:
            web_push: Optional Web Push (PWA) notification service.
            apns: Optional iOS APNs notification service.
        """
        self._web_push = web_push
        self._apns = apns

    @property
    def enabled(self) -> bool:
        """True if at least one underlying channel is enabled."""
        return bool(
            (self._web_push is not None and self._web_push.enabled)
            or (self._apns is not None and self._apns.enabled)
        )

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
    ) -> None:
        """Send a notification to the user across every enabled channel.

        Each channel is isolated: a failure in one does not prevent the others from delivering.
        """
        channels = []
        if self._web_push is not None and self._web_push.enabled:
            channels.append(("web_push", self._web_push))
        if self._apns is not None and self._apns.enabled:
            channels.append(("apns", self._apns))

        if not channels:
            return

        results = await asyncio.gather(
            *(
                service.send_notification(user_identifier, title, body, db_context)
                for _, service in channels
            ),
            return_exceptions=True,
        )

        for (name, _), result in zip(channels, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Notification channel %s failed for user %s: %s",
                    name,
                    user_identifier,
                    result,
                    exc_info=result,
                )
