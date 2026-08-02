"""
Web ChatInterface implementation for delivering messages via Server-Sent Events.
"""

import logging
from typing import TYPE_CHECKING

from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import AssistantMessage, MessageAttachmentMetadata
from family_assistant.security.taint import TaintMetadata, TurnTaintState
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.storage.database import Database
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import Notifier
    from family_assistant.services.user_identity import UserIdentityResolver
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
        identity_resolver: "UserIdentityResolver | None" = None,
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
            identity_resolver: Optional resolver used to canonicalize conversation
                owner ids before scoping the account-global activity ping, so a
                conversation stored under an alias (e.g. a Telegram numeric id)
                still reaches the canonical web/iOS subscriber.
        """
        self.database_engine = database_engine
        self.notifier = notifier
        self.stream_hub = stream_hub
        self.identity_resolver = identity_resolver

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
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
            on_behalf_of_user_id: Acting user for owner-scoped attachment reads.
                The web path stores attachment references (resolved later by the
                owner-scoped HTTP attachment routes) and does not itself read
                attachment content, so this is accepted for protocol parity.
            taint_metadata: Runtime taint state recorded on the persisted row.
                Callers that derive the text from a processing turn pass that
                turn's state; when absent, the row is stored with an explicit
                empty (trusted-baseline) state rather than no metadata, since a
                metadata-less row is escalated to unknown_external at read time
                and would falsely taint the conversation. Deliveries of turn
                output keep their authoritative taint on the turn's own
                history rows.

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
            db_context = Database(engine=self.database_engine)
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
                AssistantMessage(
                    content=text,
                    taint_metadata=(
                        taint_metadata
                        if taint_metadata is not None
                        else TurnTaintState.empty().to_metadata()
                    ),
                ),
                interface_type="web",
                conversation_id=conversation_id,
                timestamp=clock.now(),
                attachments=attachments,
            )

            # Resolve owners now (inside the txn) for the post-commit activity
            # ping. This save carries no user_id of its own, so ownership comes
            # from the conversation's existing user messages.
            if saved_message is not None and self.stream_hub is not None:
                owner_ids = await db_context.message_history.get_conversation_owner_ids(
                    conversation_id
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
            logger.exception(
                f"WebChatInterface: Error sending message to {conversation_id}: {e}"
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
            # client already following THIS conversation. Canonicalize owner ids
            # first: the activity stream subscribes under the caller's canonical
            # id, so a conversation stored under an alias (e.g. a Telegram numeric
            # id) would otherwise ping an id no subscriber matches.
            activity_user_ids = {
                self.identity_resolver.canonicalize_owner_id(owner_id)
                if self.identity_resolver is not None
                else owner_id
                for owner_id in owner_ids
            }
            for user_id in activity_user_ids:
                await self.stream_hub.publish_activity(
                    conversation_id,
                    user_id=user_id,
                    reason="message",
                )

        logger.info(
            f"WebChatInterface: Saved message to conversation {conversation_id}, "
            f"internal_id={saved_message}."
        )
        return str(saved_message)
