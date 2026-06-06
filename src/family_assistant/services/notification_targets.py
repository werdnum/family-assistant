"""Helpers for resolving which user a notification should target."""

import logging
from datetime import timedelta

from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)

# Conversations can be long-lived; look back far enough to find the owning user.
_CONVERSATION_LOOKBACK = timedelta(days=365)


async def resolve_conversation_user(
    db_context: DatabaseContext,
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
