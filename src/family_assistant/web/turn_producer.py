"""Drive a single LLM turn and publish its events to the ConversationStreamHub.

Extracted from chat_api.py so the SSE endpoint can stay focused on routing
and authentication. The producer:

1. Persists the user message (via handle_chat_interaction_stream, which is
   responsible for that side effect).
2. Streams LLMStreamEvent objects from the processing service, translates
   each one into a typed hub StreamEvent, and publishes it to the hub.
3. Runs LaTeX normalization producer-side so every subscriber (original
   sender, resume reconnect, second tab) sees the same byte stream.
4. Bridges tool-confirmation requests through the hub instead of through a
   per-connection queue, so confirmation events survive client disconnects
   the same way as any other event.
5. After ``end_turn``, waits briefly for any subscriber to ack the
   ``turn_ended`` seq. If no ack arrives, fires the disconnect push
   notification (preserves the PR #879 contract).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    MessageAttachmentMetadata,
)
from family_assistant.services.confirmation_service import (
    ConfirmationService,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.telegram.protocols import ConfirmationUIManager
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolArguments,
    ToolExecutionContext,
)
from family_assistant.utils.text_normalization import StreamingLatexNormalizer
from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    StreamEvent,
    TurnStatus,
)
from family_assistant.web.web_confirmation_ui_manager import WebConfirmationUIManager

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService
    from family_assistant.processing.types import MidTurnInputProvider
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.web.web_chat_interface import WebChatInterface


class _AppStateProtocol:
    """Subset of FastAPI app.state attributes the producer reads.

    Used only for static typing; FastAPI's app.state is a free-form
    namespace, so we duck-type at runtime.
    """

    database_engine: "AsyncEngine"
    chat_interfaces: dict[str, "ChatInterface"] | None
    confirmation_ui_managers: dict[str, ConfirmationUIManager] | None
    debug_mode: bool


logger = logging.getLogger(__name__)


# How long the producer waits after turn_ended for any subscriber to ack
# before falling back to the disconnect push. In-process consumers normally
# ack within milliseconds; this generous window absorbs network jitter for
# real clients without delaying push for genuinely disconnected ones too
# long.
DEFAULT_ACK_GRACE_SECONDS = 2.0

# Generic message published to subscribers when a turn fails and the server is
# not in debug mode. The real exception is always logged server-side; only the
# detail surfaced (and retained in the replay buffer) is gated.
GENERIC_TURN_ERROR_MESSAGE = "An internal error occurred."


def format_sse_event(event: StreamEvent) -> str:
    """Serialize a hub StreamEvent into the SSE wire format.

    The seq and turn_id are mirrored into the data payload (in addition to
    the SSE event-name line) so clients that only see the parsed JSON still
    have everything they need to track and resume.
    """
    payload = dict(event.payload)
    payload.setdefault("seq", event.seq)
    if event.turn_id is not None:
        payload.setdefault("turn_id", event.turn_id)
    return f"event: {event.type}\ndata: {json.dumps(payload)}\n\n"


async def run_turn_producer(
    *,
    app_state: "_AppStateProtocol",
    hub: ConversationStreamHub,
    processing_service: "ProcessingService",
    web_chat_interface: "WebChatInterface",
    confirmation_service: ConfirmationService,
    confirmation_result_waiters: ConfirmationResultWaiterRegistry,
    attachment_registry: "AttachmentRegistry | None",
    conversation_id: str,
    turn_id: str,
    user_id: str,
    user_name: str,
    interface_type: str,
    trigger_content_parts: list[ContentPartDict],
    trigger_attachments: list[MessageAttachmentMetadata] | None,
    mid_turn_input_provider: "MidTurnInputProvider | None" = None,
    ack_grace_seconds: float = DEFAULT_ACK_GRACE_SECONDS,
) -> None:
    """Run a single LLM turn end-to-end, publishing events to the hub.

    This is the background task POST /v1/chat/turns kicks off. The hub holds
    a strong reference to it via ``attach_producer_task`` so it survives any
    client disconnect.
    """
    final_reply_parts: list[str] = []
    latex_normalizer = StreamingLatexNormalizer()

    chat_interfaces = getattr(app_state, "chat_interfaces", None)
    confirmation_ui_managers = getattr(app_state, "confirmation_ui_managers", None)

    # The durable web confirmation flow lives in WebConfirmationUIManager so the
    # live streaming turn and background runs (async profile delegation) share a
    # single implementation. This thin callback only resolves the per-turn
    # prompt and source message before delegating.
    web_confirmation_ui_manager = WebConfirmationUIManager(
        confirmation_service=confirmation_service,
        confirmation_result_waiters=confirmation_result_waiters,
        stream_hub=hub,
    )

    async def web_confirmation_callback(
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: ToolArguments,
        timeout_seconds: float,
        context: ToolExecutionContext,
    ) -> ConfirmationOutcome:
        """Resolve the prompt/source message and delegate to the web manager."""
        confirmation_prompt = (
            f"Do you want to execute '{tool_name}' with these parameters?"
        )
        source_message_internal_id: int | None = None
        if turn_id is not None:
            source_row = (
                await context.db_context.message_history.get_user_row_by_turn_id(
                    turn_id
                )
            )
            if source_row is not None:
                source_message_internal_id = source_row["internal_id"]

        return await web_confirmation_ui_manager.request_confirmation(
            conversation_id=conversation_id,
            interface_type=interface_type,
            turn_id=turn_id,
            prompt_text=confirmation_prompt,
            tool_name=tool_name,
            tool_args=tool_args,
            timeout=timeout_seconds,
            target_user_id=user_id,
            tool_call_id=call_id,
            source_message_internal_id=source_message_internal_id,
        )

    try:
        async with get_db_context(app_state.database_engine) as stream_db_context:
            if trigger_attachments:
                for attachment in trigger_attachments:
                    await hub.publish(
                        conversation_id,
                        "attachment",
                        turn_id=turn_id,
                        payload={
                            "type": "attachment",
                            "source": "trigger",
                            "attachment_id": attachment.get("attachment_id"),
                            "content_url": attachment.get("content_url"),
                            "mime_type": attachment.get("mime_type"),
                            "description": attachment.get("description"),
                            "size": attachment.get("size"),
                        },
                    )

            # Track the most recent reasoning_info (token/model usage) emitted
            # on a per-turn `done` event so it can be attached to turn_ended,
            # matching what the old streaming endpoint put on its final event.
            # ast-grep-ignore: no-dict-any - holds the provider's reasoning_info blob (token counts, model id, optional vendor fields) passed through verbatim to turn_ended
            last_reasoning_info: dict[str, Any] | None = None

            async for event in processing_service.handle_chat_interaction_stream(
                db_context=stream_db_context,
                interface_type=interface_type,
                conversation_id=conversation_id,
                trigger_content_parts=trigger_content_parts,
                trigger_interface_message_id=None,
                user_name=user_name,
                user_id=user_id,
                replied_to_interface_id=None,
                chat_interface=web_chat_interface,
                chat_interfaces=chat_interfaces,
                confirmation_ui_managers=confirmation_ui_managers,
                request_confirmation_callback=web_confirmation_callback,
                trigger_attachments=trigger_attachments,
                mid_turn_input_provider=mid_turn_input_provider,
                turn_id=turn_id,
            ):
                reasoning_info = await _publish_llm_event(
                    hub=hub,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    event=event,
                    latex_normalizer=latex_normalizer,
                    final_reply_parts=final_reply_parts,
                    db_context=stream_db_context,
                    attachment_registry=attachment_registry,
                )
                if reasoning_info is not None:
                    last_reasoning_info = reasoning_info

            trailing = latex_normalizer.flush()
            if trailing:
                await hub.publish(
                    conversation_id,
                    "text",
                    turn_id=turn_id,
                    payload={"content": trailing},
                )

        # The streaming transaction has committed here (the `async with` block
        # exited): every message handle_chat_interaction_stream persisted is now
        # visible on other database connections. Publish turn_ended only now, so
        # a subscriber that reloads conversation history on the signal (the live
        # follow stream, another tab/device) can never read the conversation
        # before this turn's reply was committed. end_turn is idempotent, so if
        # the post-completion steps below raise into the except handler, the
        # resulting end_turn(failed) is safely ignored.
        await hub.end_turn(
            conversation_id,
            turn_id=turn_id,
            status="complete",
            reasoning_info=last_reasoning_info,
        )

        delivered = await hub.wait_for_delivery(
            conversation_id, turn_id, timeout=ack_grace_seconds
        )
        if not delivered:
            # The streaming transaction is already closed; the disconnect-push
            # notification is independent follow-up work, so give it its own
            # short-lived context rather than holding the turn's transaction
            # open across the ack grace window.
            async with get_db_context(app_state.database_engine) as notify_db_context:
                await _notify_disconnected_reply(
                    notify_db_context,
                    web_chat_interface,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    reply_text="".join(final_reply_parts).strip(),
                )
    except asyncio.CancelledError:
        # The producer task was cancelled. This is the stop-generation path: the
        # cancel endpoint calls request_interrupt() (so should_interrupt() is
        # True here) and then task.cancel(). A bare loop teardown (no user stop)
        # leaves should_interrupt() False. Either way we must end the turn — a
        # TurnRecord left status='running' wedges the conversation because
        # pruning/eviction skip running turns. A user-requested stop ends as
        # 'cancelled' (not an error); a teardown ends as 'failed'. Re-raise so
        # cancellation still propagates.
        user_requested_stop = (
            mid_turn_input_provider is not None
            and mid_turn_input_provider.should_interrupt()
        )
        await _fail_turn_best_effort(
            hub,
            conversation_id=conversation_id,
            turn_id=turn_id,
            latex_normalizer=latex_normalizer,
            status="cancelled" if user_requested_stop else "failed",
            error="cancelled",
        )
        if user_requested_stop:
            # The hub turn_ended(cancelled) is in-memory only; persist a durable
            # assistant row so a refresh/reconnect (or hub eviction/restart) shows
            # the stopped turn instead of the user prompt with no reply.
            await _persist_stopped_reply(
                app_state.database_engine,
                interface_type=interface_type,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_id=user_id,
                reply_text="".join(final_reply_parts),
            )
        raise
    except Exception as exc:
        logger.error(
            "Turn producer failed for conv=%s turn=%s: %s",
            conversation_id,
            turn_id,
            exc,
            exc_info=True,
        )
        debug_mode = getattr(app_state, "debug_mode", False)
        error_detail = str(exc) if debug_mode else GENERIC_TURN_ERROR_MESSAGE
        await _fail_turn_best_effort(
            hub,
            conversation_id=conversation_id,
            turn_id=turn_id,
            latex_normalizer=latex_normalizer,
            error=error_detail,
        )


async def _fail_turn_best_effort(
    hub: ConversationStreamHub,
    *,
    conversation_id: str,
    turn_id: str,
    latex_normalizer: StreamingLatexNormalizer,
    error: str,
    status: TurnStatus = "failed",
) -> None:
    """End a turn as failed/cancelled, flushing any buffered trailing text first.

    Used by both the exception and cancellation paths so a wedged turn is
    always closed out. ``status`` is ``"failed"`` for genuine errors and
    ``"cancelled"`` for a user-requested stop. Every step is guarded so a
    secondary failure (e.g. the loop tearing down during cancellation) can't
    mask the original cause or leave the turn ``running``.
    """
    try:
        trailing = latex_normalizer.flush()
        if trailing:
            await hub.publish(
                conversation_id,
                "text",
                turn_id=turn_id,
                payload={"content": trailing},
            )
    except Exception:
        logger.exception(
            "Failed to flush trailing text on failed turn for conv=%s turn=%s",
            conversation_id,
            turn_id,
        )
    try:
        await hub.end_turn(
            conversation_id,
            turn_id=turn_id,
            status=status,
            error=error,
        )
    except Exception:
        logger.exception(
            "Failed to publish turn_ended(%s) for conv=%s turn=%s",
            status,
            conversation_id,
            turn_id,
        )


async def _persist_stopped_reply(
    database_engine: "AsyncEngine",
    *,
    interface_type: str,
    conversation_id: str,
    turn_id: str,
    user_id: str,
    reply_text: str,
) -> None:
    """Persist a durable assistant row for a user-stopped turn.

    Mirrors the optimistic 'stopped' bubble: the partial reply if any (what the
    live client already rendered), else a Stopped marker. Uses its own short DB
    context because the streaming transaction is being torn down by the
    cancellation. Best-effort — failing to persist must not mask the stop.
    """
    content = reply_text.strip() or "_Stopped._"
    try:
        async with get_db_context(database_engine) as db_context:
            await db_context.message_history.add_message(
                AssistantMessage(content=content),
                interface_type=interface_type,
                conversation_id=conversation_id,
                timestamp=datetime.now(UTC),
                turn_id=turn_id,
                user_id=user_id,
            )
    except Exception:
        logger.warning(
            "Failed to persist stopped reply for conv=%s turn=%s",
            conversation_id,
            turn_id,
            exc_info=True,
        )


async def _publish_llm_event(
    *,
    hub: ConversationStreamHub,
    conversation_id: str,
    turn_id: str,
    event: LLMStreamEvent,
    latex_normalizer: StreamingLatexNormalizer,
    final_reply_parts: list[str],
    db_context: DatabaseContext,
    attachment_registry: "AttachmentRegistry | None",
    # ast-grep-ignore: no-dict-any - returns the provider's reasoning_info blob (token counts, model id, optional vendor fields) verbatim for the turn_ended payload
) -> dict[str, Any] | None:
    """Translate a single LLMStreamEvent into hub publishes.

    LaTeX normalization runs producer-side so every subscriber sees identical
    bytes regardless of when they joined.

    Returns the ``reasoning_info`` carried on a ``done`` event (token/model
    usage), if any, so the caller can attach it to ``turn_ended``; ``None`` for
    every other event type.
    """
    if event.type == "content":
        if event.content:
            final_reply_parts.append(event.content)
            normalized = latex_normalizer.feed(event.content)
            if normalized:
                await hub.publish(
                    conversation_id,
                    "text",
                    turn_id=turn_id,
                    payload={"content": normalized},
                )
    elif event.type == "tool_call":
        # A new tool round means more turns follow; drop any preamble so the
        # disconnect push only carries the final answer.
        final_reply_parts.clear()
        if event.tool_call:
            arguments = event.tool_call.function.arguments
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            await hub.publish(
                conversation_id,
                "tool_call",
                turn_id=turn_id,
                payload={
                    "tool_call": {
                        "id": event.tool_call.id,
                        "type": event.tool_call.type,
                        "function": {
                            "name": event.tool_call.function.name,
                            "arguments": arguments,
                        },
                    }
                },
            )
    elif event.type == "tool_result":
        # ast-grep-ignore: no-dict-any - tool_result payload aggregates the tool's heterogeneous result string with optional attachment metadata; serialized verbatim to clients
        payload: dict[str, Any] = {
            "tool_call_id": event.tool_call_id,
            "result": event.tool_result,
        }
        if event.metadata and "attachments" in event.metadata:
            payload["attachments"] = event.metadata["attachments"]
        await hub.publish(
            conversation_id, "tool_result", turn_id=turn_id, payload=payload
        )
    elif event.type == "done":
        # Per-agentic-turn done: flush LaTeX so trailing ambiguous bytes don't
        # bleed into the next round's opening tokens.
        trailing = latex_normalizer.flush()
        if trailing:
            await hub.publish(
                conversation_id,
                "text",
                turn_id=turn_id,
                payload={"content": trailing},
            )
        if (
            attachment_registry is not None
            and event.metadata
            and "attachment_ids" in event.metadata
        ):
            for attachment_id in event.metadata["attachment_ids"]:
                try:
                    attachment_info = await attachment_registry.get_attachment(
                        db_context, attachment_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to fetch attachment %s for emit", attachment_id
                    )
                    continue
                if attachment_info is None:
                    logger.warning("Attachment %s not found in registry", attachment_id)
                    continue
                await hub.publish(
                    conversation_id,
                    "attachment",
                    turn_id=turn_id,
                    payload={
                        "type": "attachment",
                        "source": "response",
                        "attachment_id": attachment_id,
                        "content_url": attachment_info.content_url,
                        "mime_type": attachment_info.mime_type,
                        "description": attachment_info.description,
                        "size": attachment_info.size,
                    },
                )
        if event.metadata:
            reasoning_info = event.metadata.get("reasoning_info")
            # MessageReasoningInfo is a TypedDict; widen to a plain dict so the
            # hub stays decoupled from the LLM message types and the payload
            # serializes cleanly on the wire.
            return dict(reasoning_info) if reasoning_info is not None else None
    elif event.type == "user_input":
        # A mid-turn steering message the user sent while this turn was running.
        # The loop already injected it into the LLM context and yielded it here;
        # surfacing it as a hub event lets live viewers (and resume/replay) render
        # the steering message as a user bubble. Persistence is handled by the
        # service save path, so this branch is display-only.
        if event.content:
            await hub.publish(
                conversation_id,
                "user_input",
                turn_id=turn_id,
                payload={"type": "user_input", "content": event.content},
            )
    elif event.type == "error":
        # ast-grep-ignore: no-dict-any - error event payload carries free-form error string plus optional error_id from provider; structured typing belongs to a future error-codes design, not the hub
        error_payload: dict[str, Any] = {"error": event.error or "An error occurred"}
        if event.metadata:
            error_id = event.metadata.get("error_id")
            if error_id:
                error_payload["error_id"] = error_id
        await hub.publish(
            conversation_id, "error", turn_id=turn_id, payload=error_payload
        )


async def _notify_disconnected_reply(
    db_context: DatabaseContext,
    web_chat_interface: "WebChatInterface",
    *,
    interface_type: str,
    conversation_id: str,
    reply_text: str,
) -> None:
    """Deliver a completed assistant reply via push when no subscriber acked.

    Same contract as the original chat_api implementation: skip if there's
    no reply text and no notifier configured. The ack check happens upstream
    (``hub.wait_for_delivery``) so by the time this fires we know the live
    stream did NOT deliver the turn end to anyone listening.
    """
    notifier = getattr(web_chat_interface, "notifier", None)
    if not reply_text or notifier is None:
        return
    try:
        await notify_conversation(
            notifier,
            db_context,
            interface_type=interface_type,
            conversation_id=conversation_id,
            title="New message",
            body=reply_text[:200],
            metadata=NotificationMetadata(
                category=MESSAGE_CATEGORY,
                conversation_id=conversation_id,
            ),
        )
    except Exception:
        logger.warning(
            "Failed to send disconnect push notification for conv=%s",
            conversation_id,
            exc_info=True,
        )
