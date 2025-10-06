"""
Web ChatInterface implementation for delivering messages via Server-Sent Events.
"""

import logging
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface
from family_assistant.storage.context import get_db_context
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


class WebChatInterface(ChatInterface):
    """
    ChatInterface implementation for web UI.

    Unlike TelegramChatInterface which sends messages via the Telegram API,
    WebChatInterface saves messages to the database. The SSE notification
    mechanism (via MessageNotifier) is triggered automatically by the database
    on_commit hook, which delivers the message to connected web clients.
    """

    def __init__(self, database_engine: "AsyncEngine") -> None:
        """
        Initialize the WebChatInterface.

        Args:
            database_engine: SQLAlchemy async engine for database operations
        """
        self.database_engine = database_engine

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
