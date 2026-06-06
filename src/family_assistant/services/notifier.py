"""Protocol and value types describing a user notification channel."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from family_assistant.storage.context import DatabaseContext

# APNs notification categories the iOS client registers actions/handling for.
CONFIRMATION_CATEGORY = "FAMILY_ASSISTANT_CONFIRMATION"
MESSAGE_CATEGORY = "FAMILY_ASSISTANT_MESSAGE"


class NotificationMetadata(BaseModel):
    """Structured metadata attached to a notification for interactive clients.

    ``category`` maps to the APNs ``aps.category`` (and is mirrored into Web Push data) so the
    iOS client can attach action buttons. The remaining fields are delivered as custom payload
    keys (APNs ``userInfo`` / Web Push ``data``) so taps can deep-link instead of falling back to
    the default view.
    """

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    request_id: str | None = None
    conversation_id: str | None = None
    path: str | None = None
    url: str | None = None


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
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        """Deliver a notification to all of the user's registrations on this channel."""
        ...
