"""
Web ChatInterface implementation for delivering messages via Server-Sent Events.
"""

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface
from family_assistant.storage.context import get_db_context
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.push_notification import (
        PushNotificationService,
    )

logger = logging.getLogger(__name__)


class WebChatInterface(ChatInterface):
    """
    ChatInterface implementation for web UI.

    Unlike TelegramChatInterface which sends messages via the Telegram API,
    WebChatInterface saves messages to the database. The SSE notification
    mechanism (via MessageNotifier) is triggered automatically by the database
    on_commit hook, which delivers the message to connected web clients.
    """

    def __init__(
        self,
        database_engine: "AsyncEngine",
        push_notification_service: "PushNotificationService | None" = None,
    ) -> None:
        """
        Initialize the WebChatInterface.

        Args:
            database_engine: SQLAlchemy async engine for database operations
            push_notification_service: Optional push notification service for sending notifications
        """
        self.database_engine = database_engine
        self.push_notification_service = push_notification_service

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        """
        Sends a message to the web UI by saving it to the database.

        The message will be automatically delivered to connected web clients
        via SSE (Server-Sent Events) through the MessageNotifier on_commit hook.

        Args:
            conversation_id: The web conversation UUID
            text: The message text to send
            parse_mode: Unused for web (kept for protocol compatibility)
            reply_to_interface_id: Optional message ID to reply to
            attachment_ids: Optional list of attachment IDs

        Returns:
            The internal_id of the saved message as a string, or None if saving failed
        """
        try:
            clock = SystemClock()

            # Save message to database - SSE notification happens automatically
            async with get_db_context(engine=self.database_engine) as db_context:
                # Prepare attachment metadata if provided
                attachments = None
                if attachment_ids:
                    attachments = [
                        {
                            "type": "attachment_reference",
                            "attachment_id": attachment_id,
                        }
                        for attachment_id in attachment_ids
                    ]

                saved_message = await db_context.message_history.add(
                    interface_type="web",
                    conversation_id=conversation_id,
                    interface_message_id=None,  # Web messages don't have external IDs
                    turn_id=None,  # Not part of a processing turn
                    thread_root_id=None,  # Standalone message
                    timestamp=clock.now(),
                    role="assistant",
                    content=text,
                    tool_calls=None,
                    reasoning_info=None,
                    error_traceback=None,
                    tool_call_id=None,
                    processing_profile_id=None,
                    attachments=attachments,
                )

                # Send push notification if enabled
                if (
                    saved_message
                    and self.push_notification_service
                    and self.push_notification_service.enabled
                ):
                    try:
                        # Get user_id from saved message or find from recent user messages
                        user_id = saved_message.get("user_id")
                        if not user_id:
                            # Fallback: query recent messages to find a user message
                            # (assistant messages don't have user_id, so we look for user messages)
                            recent = await db_context.message_history.get_recent_with_metadata(
                                interface_type="web",
                                conversation_id=conversation_id,
                                limit=10,
                                max_age=timedelta(days=365),
                            )
                            # Find the most recent user message with a user_id
                            for message in recent:
                                if message.get("role") == "user" and message.get(
                                    "user_id"
                                ):
                                    user_id = message["user_id"]
                                    break

                        if user_id:
                            await self.push_notification_service.send_notification(
                                user_identifier=user_id,
                                title="New message",
                                body=text[:100],  # Truncate long messages
                                db_context=db_context,
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to send push notification: {e}", exc_info=True
                        )

            if saved_message:
                internal_id = saved_message.get("internal_id")
                logger.info(
                    f"WebChatInterface: Saved message to conversation {conversation_id}, "
                    f"internal_id={internal_id}. SSE notification will be sent automatically."
                )
                return str(internal_id) if internal_id else None

            logger.error(
                f"WebChatInterface: Failed to save message to conversation {conversation_id}"
            )
            return None

        except Exception as e:
            logger.error(
                f"WebChatInterface: Error sending message to {conversation_id}: {e}",
                exc_info=True,
            )
            return None
