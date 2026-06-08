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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.messages import (
    ContentPartDict,
    MessageAttachmentMetadata,
)
from family_assistant.services.confirmation_service import (
    DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
    DURABLE_CONFIRMATION_STATUS_POLL_SECONDS,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationNotFoundError,
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
from family_assistant.web.confirmation_manager import web_confirmation_manager
from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    StreamEvent,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService
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


logger = logging.getLogger(__name__)


# How long the producer waits after turn_ended for any subscriber to ack
# before falling back to the disconnect push. In-process consumers normally
# ack within milliseconds; this generous window absorbs network jitter for
# real clients without delaying push for genuinely disconnected ones too
# long.
DEFAULT_ACK_GRACE_SECONDS = 2.0


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
        """Confirmation callback that publishes tool_confirmation_request /
        tool_confirmation_result events through the hub.

        The durable confirmation record (DB row + waiter registry) lives the
        same as in the original chat_api implementation; only the user-facing
        event delivery changes.
        """
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

        expires_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
        durable_request = await confirmation_service.create_request(
            target_user_id=user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=call_id,
            source_message_internal_id=source_message_internal_id,
            confirmation_prompt=confirmation_prompt,
            expires_at=expires_at,
        )
        request_id = durable_request["id"]
        execution_future = confirmation_result_waiters.register(request_id)

        async def get_durable_status() -> str | None:
            try:
                refreshed = await confirmation_service.get_for_user(
                    request_id=request_id, user_id=user_id
                )
            except ConfirmationNotFoundError:
                return "missing"
            except ConfirmationAuthorizationError:
                return "unauthorized"
            except ConfirmationError:
                return "error"
            return refreshed["status"]

        async def wait_for_execution_result() -> ConfirmationOutcome:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(execution_future),
                    timeout=DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
                )
            except TimeoutError:
                return ConfirmationOutcome(
                    kind="failed",
                    result=(
                        f"Error executing approved tool '{tool_name}': "
                        "background execution did not complete in time."
                    ),
                )

        async def publish_result(*, approved: bool) -> None:
            await hub.publish(
                conversation_id,
                "tool_confirmation_result",
                turn_id=turn_id,
                payload={"request_id": request_id, "approved": approved},
            )

        try:
            decision_future = await web_confirmation_manager.request_confirmation(
                request_id=request_id,
                conversation_id=conversation_id,
                interface_type=interface_type,
                tool_name=tool_name,
                tool_args=tool_args,
                confirmation_prompt=confirmation_prompt,
                timeout_seconds=timeout_seconds,
            )

            await hub.publish(
                conversation_id,
                "tool_confirmation_request",
                turn_id=turn_id,
                payload={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_call_id": call_id,
                    "confirmation_prompt": confirmation_prompt,
                    "timeout_seconds": timeout_seconds,
                    "args": tool_args,
                },
            )

            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                done, _pending = await asyncio.wait(
                    {decision_future, execution_future},
                    timeout=min(DURABLE_CONFIRMATION_STATUS_POLL_SECONDS, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if decision_future in done:
                    decision_outcome = decision_future.result()
                    if decision_outcome.kind == "timed_out":
                        await confirmation_service.mark_expired(now=datetime.now(UTC))
                    await publish_result(approved=decision_outcome.kind == "approved")
                    if decision_outcome.kind != "approved":
                        return decision_outcome
                    web_confirmation_manager.remove_confirmation(request_id)
                    return await wait_for_execution_result()
                if execution_future in done:
                    execution_outcome = execution_future.result()
                    await publish_result(
                        approved=execution_outcome.kind in {"completed", "failed"}
                    )
                    return execution_outcome

                durable_status = await get_durable_status()
                if durable_status == "approved":
                    await publish_result(approved=True)
                    web_confirmation_manager.remove_confirmation(request_id)
                    return await wait_for_execution_result()
                if durable_status == "rejected":
                    await publish_result(approved=False)
                    return ConfirmationOutcome(kind="rejected")
                if durable_status in {"expired", "missing", "unauthorized", "error"}:
                    await publish_result(approved=False)
                    return ConfirmationOutcome(
                        kind="failed",
                        result="Confirmation request could not be resolved.",
                    )

            final_status = await get_durable_status()
            if final_status == "approved":
                await publish_result(approved=True)
                web_confirmation_manager.remove_confirmation(request_id)
                return await wait_for_execution_result()
            if final_status == "rejected":
                await publish_result(approved=False)
                return ConfirmationOutcome(kind="rejected")
            if final_status in {"missing", "unauthorized", "error"}:
                await publish_result(approved=False)
                return ConfirmationOutcome(
                    kind="failed",
                    result="Confirmation request could not be resolved.",
                )
            await confirmation_service.mark_expired(now=datetime.now(UTC))
            await publish_result(approved=False)
            return ConfirmationOutcome(kind="timed_out")
        finally:
            web_confirmation_manager.remove_confirmation(request_id)
            confirmation_result_waiters.unregister(request_id, execution_future)

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
                            "attachment_id": attachment.get("attachment_id"),
                            "url": attachment.get("content_url"),
                            "content_url": attachment.get("content_url"),
                            "mime_type": attachment.get("mime_type"),
                            "description": attachment.get("description"),
                            "size": attachment.get("size"),
                        },
                    )

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
                turn_id=turn_id,
            ):
                await _publish_llm_event(
                    hub=hub,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    event=event,
                    latex_normalizer=latex_normalizer,
                    final_reply_parts=final_reply_parts,
                    db_context=stream_db_context,
                    attachment_registry=attachment_registry,
                )

            trailing = latex_normalizer.flush()
            if trailing:
                await hub.publish(
                    conversation_id,
                    "text",
                    turn_id=turn_id,
                    payload={"content": trailing},
                )

            await hub.end_turn(conversation_id, turn_id=turn_id, status="complete")

            delivered = await hub.wait_for_delivery(
                conversation_id, turn_id, timeout=ack_grace_seconds
            )
            if not delivered:
                await _notify_disconnected_reply(
                    stream_db_context,
                    web_chat_interface,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    reply_text="".join(final_reply_parts).strip(),
                )
    except Exception as exc:
        logger.error(
            "Turn producer failed for conv=%s turn=%s: %s",
            conversation_id,
            turn_id,
            exc,
            exc_info=True,
        )
        # Best-effort: surface to subscribers via turn_ended(status=failed) so
        # they don't hang waiting for an end event that never comes.
        try:
            await hub.end_turn(
                conversation_id,
                turn_id=turn_id,
                status="failed",
                error=str(exc),
            )
        except Exception:
            logger.exception(
                "Failed to publish turn_ended(failed) for conv=%s turn=%s",
                conversation_id,
                turn_id,
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
) -> None:
    """Translate a single LLMStreamEvent into hub publishes.

    LaTeX normalization runs producer-side so every subscriber sees identical
    bytes regardless of when they joined.
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
                        "attachment_id": attachment_id,
                        "url": attachment_info.content_url,
                        "content_url": attachment_info.content_url,
                        "mime_type": attachment_info.mime_type,
                        "description": attachment_info.description,
                        "size": attachment_info.size,
                    },
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
