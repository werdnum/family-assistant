"""
Web ChatInterface implementation for delivering messages via Server-Sent Events.
"""

import logging
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import AssistantMessage, MessageAttachmentMetadata
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.storage.context import get_db_context
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import Notifier
    from family_assistant.web.conversation_stream_hub import ConversationStreamHub

logger = logging.getLogger(__name__)


class WebChatInterface(ChatInterface):
    """
    ChatInterface implementation for web UI.

    Unlike TelegramChatInterface which sends messages via the Telegram API,
    WebChatInterface saves messages to the database and, when a notifier is
    configured, delivers a push notification to the conversation owner. It also
    publishes a lightweight ``message`` event to the ConversationStreamHub so
    that clients with an open follow-stream reload — this covers assistant
    messages produced *outside* the ``/turns`` streaming path (scheduled
    callbacks, task-worker flows, cross-interface delegation), which the turn
    producer never publishes for.
    """

    def __init__(
        self,
        database_engine: "AsyncEngine",
        notifier: "Notifier | None" = None,
        stream_hub: "ConversationStreamHub | None" = None,
    ) -> None:
        """
        Initialize the WebChatInterface.

        Args:
            database_engine: SQLAlchemy async engine for database operations
            notifier: Optional notification channel (Web Push, iOS, or a dispatcher fanning out to
                both) used to notify the conversation owner of new assistant replies.
            stream_hub: Optional ConversationStreamHub. When set, a ``message``
                event is published after a successful save so open follow-streams
                reload for messages sent outside the streaming turn path.
        """
        self.database_engine = database_engine
        self.notifier = notifier
        self.stream_hub = stream_hub

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

        Connected clients watching the conversation stream receive live
        updates via the ConversationStreamHub; offline recipients get a push
        notification (when a notifier is configured).

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
                            metadata=NotificationMetadata(
                                category=MESSAGE_CATEGORY,
                                conversation_id=conversation_id,
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to send push notification: {e}", exc_info=True
                        )

            if saved_message is not None:
                # Nudge any open follow-stream to reload. The hub stream doesn't
                # carry full message rows, so this is a content-free signal; the
                # web/iOS live-update hooks refetch conversation history on it.
                # This is an in-memory publish: a failure here is a programming
                # error, so let it propagate (fail fast) rather than swallow it.
                if self.stream_hub is not None:
                    await self.stream_hub.publish(
                        conversation_id,
                        "message",
                        turn_id=None,
                        payload={
                            "conversation_id": conversation_id,
                            "new_messages": True,
                        },
                    )

                logger.info(
                    f"WebChatInterface: Saved message to conversation {conversation_id}, "
                    f"internal_id={saved_message}."
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
