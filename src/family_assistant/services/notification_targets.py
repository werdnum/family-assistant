"""Helpers for resolving and notifying the user a notification should target."""

import logging
from datetime import timedelta

from family_assistant.services.notifier import NotificationMetadata, Notifier
from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

# Conversations can be long-lived; look back far enough to find the owning user.
_CONVERSATION_LOOKBACK = timedelta(days=365)


async def resolve_conversation_user(
    db_context: Database,
    *,
    interface_type: str,
    conversation_id: str,
    limit: int = 20,
) -> str | None:
    """Resolve the owning user id for a conversation.

    Notifications originating from background work (assistant replies, task failures, worker
    completions) only carry a conversation context. The owning user is recovered from the most
    recent user message in that conversation that carries a ``user_id``.

    Returns:
        The user identifier, or ``None`` if it cannot be determined.
    """
    recent = await db_context.message_history.get_recent_with_metadata(
        interface_type=interface_type,
        conversation_id=conversation_id,
        limit=limit,
        max_age=_CONVERSATION_LOOKBACK,
    )
    for message in recent:
        if message.get("role") == "user" and message.get("user_id"):
            return message["user_id"]
    return None


async def notify_conversation(
    notifier: Notifier,
    db_context: Database,
    *,
    interface_type: str | None,
    conversation_id: str | None,
    title: str,
    body: str,
    metadata: NotificationMetadata | None = None,
) -> bool:
    """Notify the owner of a conversation, resolving the user from conversation history.

    Returns:
        ``True`` if a notification was dispatched, ``False`` if the channel was disabled or the
        owning user could not be determined.
    """
    if not notifier.enabled or not interface_type or not conversation_id:
        return False

    user_id = await resolve_conversation_user(
        db_context,
        interface_type=interface_type,
        conversation_id=conversation_id,
    )
    if user_id is None:
        return False

    await notifier.send_notification(
        user_identifier=user_id,
        title=title,
        body=body,
        db_context=db_context,
        metadata=metadata,
    )
    return True
