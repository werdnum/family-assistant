"""
Web ChatInterface implementation for delivering messages via Server-Sent Events.
"""

import logging
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import AssistantMessage, MessageAttachmentMetadata
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.storage.context import get_db_context
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import Notifier

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
        notifier: "Notifier | None" = None,
    ) -> None:
        """
        Initialize the WebChatInterface.

        Args:
            database_engine: SQLAlchemy async engine for database operations
            notifier: Optional notification channel (Web Push, iOS, or a dispatcher fanning out to
                both) used to notify the conversation owner of new assistant replies.
        """
        self.database_engine = database_engine
        self.notifier = notifier

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
                attachments: list[MessageAttachmentMetadata] | None = None
                if attachment_ids:
                    attachments = [
                        MessageAttachmentMetadata(
                            type="attachment_reference",
                            attachment_id=attachment_id,
                        )
                        for attachment_id in attachment_ids
                    ]

                saved_message = await db_context.message_history.add_message(
                    AssistantMessage(content=text),
                    interface_type="web",
                    conversation_id=conversation_id,
                    timestamp=clock.now(),
                    attachments=attachments,
                )

                # Notify the conversation owner about the new assistant reply.
                if saved_message is not None and self.notifier is not None:
                    try:
                        await notify_conversation(
                            self.notifier,
                            db_context,
                            interface_type="web",
                            conversation_id=conversation_id,
                            title="New message",
                            body=text[:100],  # Truncate long messages
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to send push notification: {e}", exc_info=True
                        )

            if saved_message is not None:
                logger.info(
                    f"WebChatInterface: Saved message to conversation {conversation_id}, "
                    f"internal_id={saved_message}. SSE notification will be sent automatically."
                )
                return str(saved_message)

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
