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
        # The save + notify path is wrapped so a delivery failure (DB write or
        # push notification) surfaces as a failed send (returns None). The hub
        # publish below is deliberately OUTSIDE this guard: it runs AFTER the
        # message is durably committed, so swallowing its failure would make a
        # saved message look like a failed send — callers would then resend or
        # retry an already-approved confirmation, causing duplicate side
        # effects. A publish failure is a programming error; let it propagate.
        # Owner ids of the conversation, resolved while the save transaction is
        # open so the post-commit activity ping can be scoped to them (the
        # account-global activity channel filters subscribers by user_id).
        owner_ids: set[str] = set()
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

                # Resolve owners now (inside the txn) for the post-commit activity
                # ping. This save carries no user_id of its own, so ownership comes
                # from the conversation's existing user messages.
                if saved_message is not None and self.stream_hub is not None:
                    owner_ids = (
                        await db_context.message_history.get_conversation_owner_ids(
                            conversation_id
                        )
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
        except Exception as e:
            logger.error(
                f"WebChatInterface: Error sending message to {conversation_id}: {e}",
                exc_info=True,
            )
            return None

        if saved_message is None:
            logger.error(
                f"WebChatInterface: Failed to save message to conversation {conversation_id}"
            )
            return None

        # Nudge any open follow-stream to reload. The hub stream doesn't carry
        # full message rows, so this is a content-free signal; the web/iOS
        # live-update hooks refetch conversation history on it. This is an
        # in-memory publish: a failure here is a programming error, so let it
        # propagate (fail fast) rather than swallow it — see the note above on
        # why a post-commit publish failure must not look like a failed send.
        #
        # NOTE: this hub tickle replaces the old MessageNotifier on_commit
        # hook, which fired for EVERY message_history write. The hub is only
        # nudged here, on WebChatInterface saves. Messages written by other
        # interfaces (Telegram, email intake) land in their own conversations,
        # which the web UI doesn't surface and whose multi-owner streams the
        # auth layer 404s — so no live-update is owed there. If a future
        # surface lets the web UI watch a conversation that receives writes
        # from a non-web path, that path must publish its own hub tickle.
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
            # Also ping the account-global activity stream so this out-of-band
            # reply (scheduled/reminder callback, tool-initiated message) surfaces
            # and bumps the conversation in the owner's list on a client sitting
            # on another thread — the per-conversation tickle above only reaches a
            # client already following THIS conversation.
            for owner_id in owner_ids:
                await self.stream_hub.publish_activity(
                    conversation_id,
                    user_id=owner_id,
                    reason="message",
                )

        logger.info(
            f"WebChatInterface: Saved message to conversation {conversation_id}, "
            f"internal_id={saved_message}."
        )
        return str(saved_message)
