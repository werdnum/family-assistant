"""Protocol describing a user notification channel."""

from typing import Protocol

from family_assistant.storage.context import DatabaseContext


class Notifier(Protocol):
    """A channel that can deliver notifications to a user.

    Implemented by :class:`PushNotificationService` (Web Push), :class:`APNsService` (iOS), and
    :class:`NotificationDispatcher` (fan-out across channels). Consumers depend on this protocol
    rather than a concrete service so notification delivery is an explicit, type-checked contract.
    """

    @property
    def enabled(self) -> bool:
        """Whether this channel is configured and able to deliver notifications."""
        ...

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
    ) -> None:
        """Deliver a notification to all of the user's registrations on this channel."""
        ...
