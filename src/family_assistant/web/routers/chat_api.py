import asyncio
import base64
import binascii
import hashlib
import json
import logging
import mimetypes
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from family_assistant.llm import ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    UserMessage,
    attachment_content,
    image_url_content,
    text_content,
)
from family_assistant.processing import DelegatableService, ProcessingService
from family_assistant.processing.types import MidTurnUserInput
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_history_taint,
)
from family_assistant.services.confirmation_service import (
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationNotFoundError,
    ConfirmationService,
    create_durable_confirmation,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.user_identity import (
    UserIdentityResolver,
)
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.conversation_shares import ConversationShare
from family_assistant.storage.types import MessageHistoryRow
from family_assistant.tools import MCPToolsProvider, find_provider_by_type
from family_assistant.tools.infrastructure import ToolDescriptorProvider
from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext
from family_assistant.web.confirmation_manager import web_confirmation_manager
from family_assistant.web.conversation_stream_hub import (
    ConversationStreamHub,
    ConversationTurnRunningError,
    OutOfBufferError,
    StreamEvent,
    TurnAlreadyExistsError,
    TurnRecord,
    TurnStatus,
)
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
    get_processing_service,
    get_user_identity_resolver,
    get_web_chat_interface,
)
from family_assistant.web.models import (
    ChatAttachmentRequest,
    ChatMessageResponse,
    ChatPromptRequest,
    ToolCallResponseItem,
    VoiceSessionRequest,
    VoiceSessionResponse,
)
from family_assistant.web.turn_producer import (
    format_sse_event,
    persist_stopped_reply,
    run_turn_producer,
)
from family_assistant.web.web_mid_turn_controller import WebMidTurnController

if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import (
        AttachmentMetadata,
        AttachmentRegistry,
    )
    from family_assistant.web.web_chat_interface import WebChatInterface


logger = logging.getLogger(__name__)
chat_api_router = APIRouter()

_TOKEN_IDENTITY_SOURCES = {"api_token", "app_token_session"}


def _content_part_for_attachment(
    attachment_id: str, content_url: str, mime_type: str
) -> ContentPartDict:
    if mime_type.startswith("image/"):
        return image_url_content(content_url)
    return attachment_content(attachment_id)


def _user_name_for_chat(current_user: Mapping[str, object]) -> str:
    """Derive a human-friendly name for the authenticated web user.

    The name surfaces in the assistant's system prompt and in stored message
    history, so prefer the explicitly configured user label, then the OIDC
    display name claim, then the canonical user identifier, before falling back
    to a generic label.

    For token-based auth the "name" claim is only a copy of the token owner
    identifier (which identity resolution may already have rewritten to a
    canonical user id), not a real display name, so it is skipped in favour of
    the canonical identifier.
    """
    is_token_auth = (
        current_user.get("identity_source") in _TOKEN_IDENTITY_SOURCES
        or current_user.get("source") in _TOKEN_IDENTITY_SOURCES
    )
    candidate_keys = (
        ("user_label", "user_identifier")
        if is_token_auth
        else ("user_label", "name", "user_identifier")
    )
    for key in candidate_keys:
        value = current_user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "API User"


def _get_confirmation_service(request: Request) -> ConfirmationService:
    service = getattr(request.app.state, "confirmation_service", None)
    if isinstance(service, ConfirmationService):
        return service
    engine = request.app.state.database_engine
    service = ConfirmationService(db=Database(engine))
    request.app.state.confirmation_service = service
    return service


def _get_confirmation_result_waiters(
    request: Request,
) -> ConfirmationResultWaiterRegistry:
    waiters = getattr(request.app.state, "confirmation_result_waiters", None)
    if isinstance(waiters, ConfirmationResultWaiterRegistry):
        return waiters
    waiters = ConfirmationResultWaiterRegistry()
    request.app.state.confirmation_result_waiters = waiters
    return waiters


async def _enrich_persisted_attachments(
    messages: list[MessageHistoryRow],
    *,
    db_context: Database,
    attachment_registry: "AttachmentRegistry",
    acting_user_id: str | None,
) -> None:
    """Fill in the mime type and content URL of persisted attachment metadata.

    History rows record attachments in whatever shape their producer had at write
    time: a tool result keeps the mime type but not always a content URL, and a
    reply delivered through a chat interface records a bare
    ``attachment_reference`` carrying only an id. Clients need the mime type to
    decide whether an attachment is an image they should show inline, and a URL to
    fetch it, so resolve every referenced id against the registry here instead of
    making each client guess. Attachments the caller cannot see are left exactly
    as stored.
    """
    referenced_ids: set[str] = set()
    for message in messages:
        for attachment in message.get("attachments") or []:
            attachment_id = attachment.get("attachment_id")
            if attachment_id:
                referenced_ids.add(attachment_id)
    if not referenced_ids:
        return

    resolved = await attachment_registry.get_attachments(
        db_context, sorted(referenced_ids), acting_user_id=acting_user_id
    )
    missing = referenced_ids - resolved.keys()
    if missing:
        # Expected for attachments that have been cleaned up or that belong to
        # another user, and /messages is polled, so this stays at debug level.
        logger.debug(
            "History attachments not resolvable for this caller, left unenriched: %s",
            ", ".join(sorted(missing)),
        )

    for message in messages:
        for attachment in message.get("attachments") or []:
            attachment_id = attachment.get("attachment_id")
            if attachment_id is None:
                continue
            metadata = resolved.get(attachment_id)
            if metadata is None:
                continue
            content_url = (
                attachment.get("content_url")
                or metadata.content_url
                or f"/api/attachments/{attachment_id}"
            )
            attachment["content_url"] = content_url
            if not attachment.get("url"):
                attachment["url"] = content_url
            if not attachment.get("mime_type"):
                attachment["mime_type"] = metadata.mime_type
            if not attachment.get("description"):
                attachment["description"] = metadata.description
            if attachment.get("size") is None:
                attachment["size"] = metadata.size


async def _process_user_attachments(
    payload: ChatPromptRequest,
    conversation_id: str,
    attachment_registry: "AttachmentRegistry",
    db_context: Database,
    user_id: str,
) -> tuple[list[ContentPartDict], list[MessageAttachmentMetadata] | None]:
    """
    Process user attachments from the request payload.

    Args:
        payload: Chat request with potential attachments
        conversation_id: Conversation ID for attachment association
        attachment_registry: Registry for storing attachments
        db_context: Database context

    Returns:
        Tuple of (trigger_content_parts, trigger_attachments)
    """
    trigger_content_parts: list[ContentPartDict] = [text_content(payload.prompt)]
    trigger_attachments: list[MessageAttachmentMetadata] | None = None

    if payload.attachments:
        trigger_attachments = []
        for attachment in payload.attachments:
            # Handle images, videos, audio, and documents (PDFs)
            attachment_type = attachment.get("type")
            if attachment_type in {"image", "video", "audio", "document"}:
                # Validate that content is present and not empty
                content_data = attachment.get("content")
                if not content_data:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Attachment content is required",
                    )
                if not content_data.strip():
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Attachment content cannot be empty",
                    )
                # Handle attachment content - either URL reference or base64 data
                try:
                    # New flow: Handle URL references to uploaded attachments
                    if content_data.startswith("/api/attachments/"):
                        # Content is a URL reference to an already uploaded attachment
                        # Extract attachment ID from URL like "/api/attachments/12345"
                        attachment_id = content_data.split("/")[-1]

                        # First try to atomically claim unlinked attachment for this conversation
                        attachment_record: (
                            AttachmentMetadata | None
                        ) = await attachment_registry.claim_unlinked_attachment(
                            db_context=db_context,
                            attachment_id=attachment_id,
                            conversation_id=conversation_id,
                            acting_user_id=user_id,
                            required_source_id=user_id,
                        )

                        # If not claimed (already linked), get existing attachment record
                        if not attachment_record:
                            attachment_record = (
                                await attachment_registry.get_attachment(
                                    db_context=db_context,
                                    attachment_id=attachment_id,
                                    acting_user_id=user_id,
                                )
                            )

                        if not attachment_record or not attachment_record.content_url:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail="Attachment not found or missing content URL",
                            )
                        if (
                            attachment_record.source_id != user_id
                            and attachment_record.conversation_id != conversation_id
                        ):
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail="Attachment not found",
                            )

                        trigger_content_parts.append(
                            _content_part_for_attachment(
                                attachment_record.attachment_id,
                                attachment_record.content_url,
                                attachment_record.mime_type,
                            )
                        )

                        # Store attachment metadata for message history
                        trigger_attachments.append({
                            "type": attachment.get("type", "image"),
                            "attachment_id": attachment_record.attachment_id,
                            "url": attachment_record.content_url,
                            "content_url": attachment_record.content_url,
                            "mime_type": attachment_record.mime_type,
                            "description": attachment_record.description,
                            "filename": attachment_record.metadata.get(
                                "original_filename", "unknown"
                            ),
                            "size": attachment_record.size,
                        })

                    else:
                        # Legacy flow: Handle base64 data (for backwards compatibility)
                        if content_data.startswith("data:"):
                            # Extract MIME type and base64 data
                            header, b64_data = content_data.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                            content_bytes = base64.b64decode(b64_data)
                            base_filename = attachment.get(
                                "filename", f"upload_{uuid.uuid4().hex[:8]}"
                            )
                            # Ensure filename has correct extension based on MIME type
                            ext = mimetypes.guess_extension(mime_type) or ""
                            if ext and not base_filename.lower().endswith(ext):
                                filename = f"{base_filename}{ext}"
                            else:
                                filename = base_filename
                        else:
                            # Assume direct base64 content
                            content_bytes = base64.b64decode(content_data)
                            # For security, don't trust client-provided filenames for MIME type
                            # Instead, try to detect from content magic bytes or use safe default
                            base_filename = attachment.get(
                                "filename", f"upload_{uuid.uuid4().hex[:8]}"
                            )

                            # Basic content-based MIME type detection for common image formats
                            # Check magic bytes at the beginning of the content
                            if content_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                                mime_type = "image/png"
                            elif content_bytes.startswith(b"\xff\xd8\xff"):
                                mime_type = "image/jpeg"
                            elif content_bytes.startswith(b"GIF8"):
                                mime_type = "image/gif"
                            elif (
                                content_bytes.startswith(b"RIFF")
                                and b"WEBP" in content_bytes[:12]
                            ):
                                mime_type = "image/webp"
                            elif content_bytes.startswith(b"BM"):
                                mime_type = "image/bmp"
                            else:
                                # Unknown format, use safe generic type
                                mime_type = "application/octet-stream"

                            # Ensure filename has correct extension based on MIME type
                            ext = mimetypes.guess_extension(mime_type) or ""
                            if ext and not base_filename.lower().endswith(ext):
                                filename = f"{base_filename}{ext}"
                            else:
                                filename = base_filename

                        # Store attachment via AttachmentRegistry
                        attachment_record = (
                            await attachment_registry.register_user_attachment(
                                db_context=db_context,
                                content=content_bytes,
                                filename=filename,
                                mime_type=mime_type,
                                conversation_id=conversation_id,
                                message_id=None,  # Will be set when message is stored
                                user_id=user_id,
                                description=attachment.get(
                                    "description", f"User uploaded: {filename}"
                                ),
                            )
                        )

                        if not attachment_record.content_url:
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail="Failed to generate content URL for attachment",
                            )

                        trigger_content_parts.append(
                            _content_part_for_attachment(
                                attachment_record.attachment_id,
                                attachment_record.content_url,
                                attachment_record.mime_type,
                            )
                        )

                        # Store attachment metadata for message history with stable attachment_id
                        trigger_attachments.append({
                            "type": attachment.get("type", "image"),
                            "attachment_id": attachment_record.attachment_id,
                            "url": attachment_record.content_url,
                            "content_url": attachment_record.content_url,
                            "mime_type": attachment_record.mime_type,
                            "description": attachment_record.description,
                            "filename": filename,
                            "size": attachment_record.size,
                        })

                except (ValueError, binascii.Error) as e:
                    # Invalid base64 or data URL format
                    logger.error(f"Invalid attachment content: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid base64 attachment content: {e!s}",
                    ) from e
                except HTTPException:
                    raise
                except Exception as e:
                    logger.exception(f"Error processing user attachment: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to process attachment",
                    ) from e
            else:
                # Dropping it here is why a misclassified attachment looked like a
                # model that ignored the file: the upload succeeded and the turn
                # ran without it. Not an error, because the iOS client's type
                # enum can still produce 'file' for a type it has no case for.
                logger.warning(
                    "Ignoring attachment of unhandled type %r (%s); it will not "
                    "reach the model.",
                    attachment_type,
                    attachment.get("name", "unnamed"),
                )

    return trigger_content_parts, trigger_attachments


class ConversationSummary(BaseModel):
    """Summary of a conversation for listing."""

    conversation_id: str = Field(..., description="Unique conversation identifier")
    last_message: str = Field(..., description="Preview of the last message")
    last_timestamp: datetime = Field(..., description="Timestamp of the last message")
    message_count: int = Field(..., description="Total number of messages")


class ConversationListResponse(BaseModel):
    """Response containing list of conversations."""

    conversations: list[ConversationSummary] = Field(
        ..., description="List of conversation summaries"
    )
    count: int = Field(..., description="Total number of conversations")


class ConversationMessage(BaseModel):
    """A single message in a conversation."""

    internal_id: int = Field(..., description="Internal database ID")
    turn_id: str | None = Field(
        None,
        description=(
            "ID of the turn that produced this message. Groups the rows of a "
            "single turn (user message, assistant reply, tool messages share it). "
            "Nullable: legacy rows and non-turn writes (e.g. a plain note save) "
            "have none. Not unique: one turn owns several rows."
        ),
    )
    role: str = Field(..., description="Message role (user/assistant/system/tool)")
    content: str | list[dict] | None = Field(
        None, description="Message content (string or list for multimodal)"
    )
    timestamp: datetime = Field(..., description="Message timestamp")
    tool_calls: list[dict] | None = Field(None, description="Tool calls if any")
    tool_call_id: str | None = Field(None, description="Tool call ID for tool messages")
    error_traceback: str | None = Field(None, description="Error traceback if any")
    attachments: list[MessageAttachmentMetadata] | None = Field(
        None, description="Attachment metadata if any"
    )
    processing_profile_id: str | None = Field(
        None, description="ID of the processing profile that generated this message"
    )
    reasoning_info: MessageReasoningInfo | None = Field(
        None, description="LLM reasoning/usage information (token counts, model, etc.)"
    )
    metadata: dict | None = Field(None, description="Additional message metadata")


class ConversationMessagesResponse(BaseModel):
    """Response containing messages for a specific conversation."""

    conversation_id: str = Field(..., description="Conversation identifier")
    messages: list[ConversationMessage] = Field(..., description="List of messages")
    count: int = Field(..., description="Number of messages in current batch")
    total_messages: int = Field(
        ..., description="Total number of messages in conversation"
    )
    has_more_before: bool = Field(
        default=False,
        description="Whether there are more messages before the current batch",
    )
    has_more_after: bool = Field(
        default=False,
        description="Whether there are more messages after the current batch",
    )
    latest_user_profile_id: str | None = Field(
        default=None,
        description=(
            "Processing profile of the most recent user message in the whole "
            "conversation (independent of the returned message page). Populated "
            "only when the request sets include_conversation_profile=true. The "
            "client adopts it when reopening a conversation so the follow-up turn "
            "loads the matching profile-partitioned history."
        ),
    )
    active_turns: list["ActiveTurnInfo"] = Field(
        default_factory=list,
        description=(
            "Recently retained turn state for this conversation. Running turns "
            "let the web/iOS UI render an 'assistant is still thinking' "
            "placeholder and resume SSE; completed turns let a reconnecting "
            "client distinguish durable tool-only replies from partial rows."
        ),
    )


class ConversationShareResponse(BaseModel):
    """New active share link for a conversation."""

    share_url: str


class ConversationShareStatusResponse(BaseModel):
    """Whether a conversation currently has an active share."""

    active: bool


class ActiveTurnInfo(BaseModel):
    """Snapshot of retained turn state surfaced via /messages and 410 responses."""

    turn_id: str = Field(..., description="Turn identifier")
    started_at: datetime = Field(..., description="When the turn was registered")
    latest_seq: int = Field(
        ..., description="Highest seq published so far for this turn"
    )
    status: str = Field(
        ...,
        description="Turn status: 'running', 'complete', 'failed', or 'cancelled'",
    )


class ChatTurnRequest(BaseModel):
    """Body for POST /v1/chat/turns.

    The client supplies ``turn_id`` (a fresh UUIDv4) so the request is
    idempotent: retrying the same POST returns the existing turn instead of
    starting a second producer.
    """

    turn_id: str = Field(
        ...,
        description=(
            "Client-supplied UUIDv4 identifying this turn. Retries with the "
            "same turn_id are idempotent."
        ),
    )
    conversation_id: str | None = Field(
        default=None,
        description="Conversation identifier (auto-generated if omitted)",
    )
    prompt: str = Field(..., description="User prompt for this turn")
    profile_id: str | None = Field(
        default=None, description="Processing profile to use"
    )
    interface_type: str | None = Field(
        default=None, description="Originating interface (web, ios, api)"
    )
    attachments: list["ChatAttachmentRequest"] | None = Field(
        default=None, description="User-supplied attachments"
    )


class ChatTurnResponse(BaseModel):
    """Response from POST /v1/chat/turns. The client uses ``first_seq`` as the
    starting cursor for the follow-up GET /stream subscription."""

    turn_id: str = Field(..., description="Turn identifier")
    conversation_id: str = Field(..., description="Conversation identifier")
    first_seq: int = Field(
        ...,
        description=(
            "Sequence number of the turn_started event. Pass this as "
            "?from_seq= when subscribing to the conversation stream."
        ),
    )
    already_complete: bool = Field(
        default=False,
        description=(
            "True when the turn was resolved from the durable record (turn_id "
            "found in the DB but not in the in-memory hub: restart / pruned / "
            "evicted). The turn already finished and is NOT replayable from the "
            "hub, so clients must reload history instead of opening /stream."
        ),
    )
    incomplete: bool = Field(
        default=False,
        description=(
            "Only meaningful with already_complete=True. True when the durable "
            "record has the user prompt but NO assistant reply — the turn was "
            "interrupted (crash/restart) before producing a result. The client "
            "should surface a recovery path rather than silently showing the "
            "prompt alone."
        ),
    )


class AckRequest(BaseModel):
    """Body for POST /v1/chat/ack. The client uses this to tell the server it
    has received events up to ``ack_seq`` so the disconnect-push fallback can
    suppress redundant pushes for already-delivered replies."""

    conversation_id: str
    ack_seq: int


class AckResponse(BaseModel):
    ok: bool = True


class ChatTurnCancelRequest(BaseModel):
    """Body for POST /v1/chat/turns/{turn_id}/cancel."""

    conversation_id: str = Field(
        ..., description="Conversation the turn belongs to (for ownership checks)"
    )


class ChatTurnCancelResponse(BaseModel):
    """Response from the cancel endpoint.

    ``status`` is ``"cancelling"`` when a stop was requested for a running turn;
    the authoritative terminal state (``cancelled``) arrives later via the SSE
    ``turn_ended`` event. For an already-finished turn it echoes the terminal
    status and ``already_complete`` is True (idempotent no-op)."""

    turn_id: str = Field(..., description="Turn identifier")
    conversation_id: str = Field(..., description="Conversation identifier")
    status: str = Field(..., description="'cancelling' or the terminal turn status")
    already_complete: bool = Field(
        default=False, description="True when the turn had already finished"
    )


class ChatTurnSteerRequest(BaseModel):
    """Body for POST /v1/chat/turns/{turn_id}/steer."""

    conversation_id: str = Field(
        ..., description="Conversation the turn belongs to (for ownership checks)"
    )
    prompt: str = Field(
        ..., description="Steering message to inject into the running turn"
    )
    input_id: str | None = Field(
        default=None,
        description=(
            "Client-generated identifier for this submission. The turn's echo of "
            "the message carries it back on the ``user_input`` event, so a client "
            "whose steer response was lost can tell whether the turn consumed "
            "*its* message rather than an identical one from another client."
        ),
    )


class ChatTurnSteerResponse(BaseModel):
    """Response from the steer endpoint."""

    turn_id: str = Field(..., description="Turn identifier")
    conversation_id: str = Field(..., description="Conversation identifier")
    accepted: bool = Field(
        ..., description="True once the steering message was queued for injection"
    )
    queued_after_seq: int = Field(
        ...,
        description=(
            "Seq of the conversation's most recent event when the steer was "
            "queued (-1 if none). The turn's echo of this message is published "
            "later, so it carries a strictly greater seq — a client replaying "
            "the turn uses this to tell the echo from identical earlier input."
        ),
    )


# ----------------------------------------------------------------------- #
# Resumable streaming helpers
# ----------------------------------------------------------------------- #


def _get_hub(request: Request) -> ConversationStreamHub:
    """Return the per-app ConversationStreamHub, creating one if needed.

    The hub holds in-flight turn state and producer task strong references.
    It replaces the older anonymous ``background_chat_tasks`` set.
    """
    hub = getattr(request.app.state, "conversation_stream_hub", None)
    if isinstance(hub, ConversationStreamHub):
        return hub
    hub = ConversationStreamHub()
    request.app.state.conversation_stream_hub = hub
    return hub


def _running_turn_conflict(
    conversation_id: str, rejected_turn_id: str, running_turn: TurnRecord
) -> HTTPException:
    """Build the 409 that refuses a rival turn and names the running one.

    Raised from three places — the early check in ``POST /turns``, the
    authoritative one inside ``start_turn``, and the reservation taken by
    ``POST /send_message`` — so the client sees one shape regardless of where
    the rival lost, and regardless of which endpoint holds the conversation.

    A running turn started by ``/send_message`` carries no mid-turn controller,
    so steering it answers 409 and the client falls back to holding the prompt.
    That is the intended outcome: a non-streaming turn is short-lived and has no
    event stream to carry a steer echo, so its rivals wait rather than steer.
    """
    logger.info(
        "Rejecting turn %s: conversation %s already has running turn %s.",
        rejected_turn_id,
        conversation_id,
        running_turn.turn_id,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": (
                "This conversation already has a running turn. Steer that "
                "turn instead of starting a new one."
            ),
            "active_turn_id": running_turn.turn_id,
            # Where that turn's events start in the hub buffer, so a client
            # that lost its stream resubscribes to the running turn alone
            # rather than replaying the whole conversation from seq 0.
            "active_turn_first_seq": running_turn.first_seq,
        },
    )


def _duplicate_turn_conflict(
    conversation_id: str, turn_id: str, existing_turn: TurnRecord
) -> HTTPException:
    """Build the 409 that refuses a ``turn_id`` this process already finished.

    ``POST /send_message`` is idempotent on ``turn_id`` via the persisted reply,
    so a retry of a turn that produced one never reaches here. What does is a
    retry of a turn that ended WITHOUT a reply (it failed, or it was a streaming
    turn of the same id that failed): re-driving it under the same id would
    collide with the finished record, so the client is told to retry under a new
    one rather than being handed a misleading "a turn is running" conflict.
    """
    logger.info(
        "Rejecting send_message turn %s in conversation %s: turn id already used "
        "(status=%s).",
        turn_id,
        conversation_id,
        existing_turn.status,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": (
                "This turn id has already been used and produced no reply. "
                "Retry with a new turn id."
            ),
            "turn_id": turn_id,
            "turn_status": existing_turn.status,
        },
    )


@dataclass(slots=True)
class _NonStreamingTurnReservation:
    """Mutable outcome handle for a ``/send_message`` hub reservation.

    The turn is assumed to have failed until the endpoint says otherwise, so an
    exception (or a return path that never reached the reply) ends the hub turn
    with a terminal ``failed`` status rather than leaving it wedged at
    ``running`` and blocking the conversation.
    """

    turn: TurnRecord
    status: TurnStatus = "failed"
    error: str | None = "An internal error occurred."

    def mark_complete(self) -> None:
        """Record that the turn produced a reply."""
        self.status = "complete"
        self.error = None


@asynccontextmanager
async def _reserve_non_streaming_turn(
    hub: ConversationStreamHub,
    conversation_id: str,
    *,
    turn_id: str,
    user_id: str,
) -> AsyncIterator[_NonStreamingTurnReservation]:
    """Hold the one-turn-per-conversation reservation across a non-streaming send.

    ``POST /send_message`` drives a full LLM loop over the same history as the
    streaming path, so it must take the same reservation: two loops on one
    conversation interleave their writes, and a turn that rebuilds history while
    another's tool call is in flight answers that call with the "abandoned"
    placeholder even though the real result is about to be written.

    The record is created before any of the turn's work, ended with a terminal
    status in a ``finally`` (so a failure or a client disconnect mid-turn
    releases it), and then discarded — it exists only as the reservation, and
    keeping it would burn its ``turn_id`` for retries.
    """
    try:
        turn = await hub.start_turn(
            conversation_id,
            turn_id=turn_id,
            user_id=user_id,
            started_at=datetime.now(UTC),
            reject_if_running=True,
        )
    except ConversationTurnRunningError as exc:
        raise _running_turn_conflict(conversation_id, turn_id, exc.turn) from exc
    except TurnAlreadyExistsError as exc:
        if exc.turn.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
        if exc.turn.status == "running":
            # A concurrent request carrying the same turn_id is still driving it.
            raise _running_turn_conflict(conversation_id, turn_id, exc.turn) from exc
        raise _duplicate_turn_conflict(conversation_id, turn_id, exc.turn) from exc

    reservation = _NonStreamingTurnReservation(turn=turn)
    try:
        yield reservation
    except asyncio.CancelledError:
        # The client hung up (an App Intent timing out, say). The turn stopped
        # where it stood; say so rather than reporting a failure.
        reservation.status = "cancelled"
        reservation.error = None
        raise
    finally:
        # ``discard_turn`` sits in its own ``finally``: if ``end_turn`` is
        # interrupted (it can await a contended lock while this task is being
        # cancelled), the record must still be released or the conversation
        # would refuse every later turn.
        try:
            await hub.end_turn(
                conversation_id,
                turn_id=turn_id,
                status=reservation.status,
                error=reservation.error,
            )
        finally:
            await hub.discard_turn(conversation_id, turn_id)


# Lifecycle/control frames that an ``event_types`` allow-list must never filter
# out: they are how the client knows when to stop, reload, or reconnect.
_ALWAYS_EMITTED_EVENT_TYPES = frozenset({"turn_ended", "heartbeat", "stream_dropped"})


def _parse_event_types(event_types: str | None) -> frozenset[str] | None:
    """Parse the comma-separated ``event_types`` query param into a set.

    Returns ``None`` (no filtering) when unset or effectively empty; otherwise a
    frozenset of the requested event-type names.
    """
    if event_types is None:
        return None
    requested = {part.strip() for part in event_types.split(",") if part.strip()}
    return frozenset(requested) if requested else None


def _should_emit(event_type: str, allowed_event_types: frozenset[str] | None) -> bool:
    """Whether an event of ``event_type`` passes the ``event_types`` filter."""
    if allowed_event_types is None:
        return True
    return (
        event_type in allowed_event_types or event_type in _ALWAYS_EMITTED_EVENT_TYPES
    )


async def _existing_send_message_response(
    db_context: Database,
    conversation_id: str,
    turn_id: str,
) -> ChatMessageResponse | None:
    """Return the persisted reply for a previously-handled ``turn_id``, if any.

    ``/send_message`` is idempotent on ``turn_id`` (durable fallback, mirroring
    ``/turns``): a retried request reads the already-persisted assistant reply
    instead of re-driving the LLM and double-persisting. Returns ``None`` when no
    assistant reply exists yet for the turn (so the caller drives it fresh).
    """
    messages = await db_context.message_history.get_by_turn_id(turn_id)
    assistant_message = next(
        (
            msg
            for msg in reversed(messages)
            if isinstance(msg, AssistantMessage) and msg.content
        ),
        None,
    )
    if assistant_message is None or not isinstance(assistant_message.content, str):
        return None

    tool_calls_response: list[ToolCallResponseItem] | None = None
    if assistant_message.tool_calls:
        tool_calls_response = []
        for tc in assistant_message.tool_calls:
            arguments = tc.function.arguments
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            tool_calls_response.append({
                "id": tc.id,
                "type": tc.type,
                "function": {"name": tc.function.name, "arguments": arguments},
            })

    return ChatMessageResponse(
        reply=assistant_message.content,
        conversation_id=conversation_id,
        turn_id=turn_id,
        attachments=None,
        tool_calls=tool_calls_response,
        already_complete=True,
    )


def _get_shutdown_event(request: Request) -> asyncio.Event:
    """Return the app-wide shutdown event, creating an unset one if absent.

    ``assistant.py`` installs the real event on ``app.state.shutdown_event`` and
    sets it on SIGTERM; the SSE generator races ``queue.get()`` against it so a
    follow stream closes promptly instead of heartbeating forever during a
    graceful shutdown. Tests that build a bare app get an inert event.
    """
    shutdown_event = getattr(request.app.state, "shutdown_event", None)
    if isinstance(shutdown_event, asyncio.Event):
        return shutdown_event
    shutdown_event = asyncio.Event()
    request.app.state.shutdown_event = shutdown_event
    return shutdown_event


_DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _get_heartbeat_interval(request: Request) -> float:
    """Return the SSE heartbeat interval in seconds for the stream endpoints.

    Defaults to 30s. ``assistant.py`` may install a different value on
    ``app.state.stream_heartbeat_interval_seconds`` (it is also the seam tests
    use to drive the heartbeat path deterministically with a short interval
    instead of waiting the production cadence). The value is the ``asyncio.wait``
    timeout that fires a ``heartbeat`` frame when no real event arrives, so it
    doubles as the shutdown-event poll cadence.
    """
    interval = getattr(request.app.state, "stream_heartbeat_interval_seconds", None)
    if isinstance(interval, (int, float)) and interval > 0:
        return float(interval)
    return _DEFAULT_STREAM_HEARTBEAT_INTERVAL_SECONDS


def _notify_heartbeat_observer(request: Request) -> None:
    """Invoke the heartbeat-emission observer seam, if installed.

    ``app.state.stream_heartbeat_observer`` is a zero-argument callable invoked
    every time a stream endpoint emits a ``heartbeat`` frame; absent in
    production. Buffered ASGI test transports cannot observe frames before the
    stream closes, so tests install a counter here and wait on actual emissions
    instead of sleeping a wall-clock multiple of the interval.
    """
    observer = getattr(request.app.state, "stream_heartbeat_observer", None)
    if observer is not None:
        observer()


def _serialize_active_turn(turn: TurnRecord) -> ActiveTurnInfo:
    return ActiveTurnInfo(
        turn_id=turn.turn_id,
        started_at=turn.started_at,
        latest_seq=turn.latest_seq,
        status=turn.status,
    )


def _owned_turns(
    hub: ConversationStreamHub, conversation_id: str, user_id: str
) -> list[ActiveTurnInfo]:
    """Return retained turn state for every turn the user owns."""
    return [
        _serialize_active_turn(turn)
        for turn in hub.active_turns(conversation_id)
        if turn.user_id == user_id
    ]


def _canonicalize_owner_id(
    resolver: UserIdentityResolver | None, raw_owner_id: str
) -> str:
    """Map a stored ``user_id`` to its canonical application user id.

    ``get_conversation_owner_ids`` returns the raw ``user_id`` persisted on each
    user message. Most interfaces already store the canonical id, but a Telegram
    conversation may be stored under the numeric Telegram user id, which must map
    to the same canonical id the web/API session resolves to so a user's own
    Telegram conversation stays visible and openable. Unresolvable ids are
    returned unchanged so distinct unknown owners stay distinct (rather than
    collapsing together and hiding a genuine multi-owner conversation).
    """
    if resolver is None:
        return raw_owner_id
    return resolver.canonicalize_owner_id(raw_owner_id)


def _canonical_owners(
    resolver: UserIdentityResolver | None, owners: set[str]
) -> set[str]:
    """Canonicalize a set of stored owner ids (see ``_canonicalize_owner_id``)."""
    return {_canonicalize_owner_id(resolver, owner) for owner in owners}


def _caller_is_sole_canonical_owner(
    resolver: UserIdentityResolver | None,
    owners: set[str],
    caller_user_id: str,
) -> bool:
    """Return whether ``caller_user_id`` is the single canonical owner.

    An empty owner set (brand-new / empty conversation) counts as owned: the id
    is an unguessable UUID and an empty read leaks nothing. A conversation with
    more than one distinct canonical owner (e.g. a Telegram group) is NOT solely
    owned and cannot be streamed through the single-user hub.
    """
    canonical_owners = _canonical_owners(resolver, owners)
    if not canonical_owners:
        return True
    return canonical_owners == {_canonicalize_owner_id(resolver, caller_user_id)}


async def _ensure_user_owns_conversation(
    request: Request,
    current_user: Mapping[str, object],
    conversation_id: str,
    *,
    allow_new: bool,
) -> str:
    """Verify ``current_user`` may act on ``conversation_id``. Returns the
    authoritative user_id. 404 (not 403) on mismatch so the API doesn't leak
    the existence of other users' conversations. This ownership check is the
    *only* authorization boundary for the stream — the hub does no per-subscriber
    filtering — so it also enforces the hub's single-user invariant.

    Ownership is identity-aware: stored owner ids are canonicalized via the
    ``UserIdentityResolver`` before comparison, so the same human acting through
    web, API and Telegram resolves to one canonical owner. The predicate is the
    same across every endpoint (create, read, subscribe, ack): the caller must
    be the conversation's **sole** canonical owner. A genuine multi-canonical-
    owner conversation (a Telegram group) is refused everywhere, since the hub
    fans out every event to every subscriber with no per-user isolation. This
    resolves the create/subscribe asymmetry where a turn could be started on a
    conversation that then could not be watched or acked.

    ``allow_new`` governs only the hub fast path: endpoints that *create* into
    the conversation (``POST /turns``, ``POST /send_message``) and the
    point-in-time ``GET /messages`` read may short-circuit on an in-memory hub
    turn the caller owns. Brand-new / empty conversations are allowed for every
    endpoint regardless of ``allow_new`` (the always-on live-update stream
    attaches to the user's own freshly-created conversation before any message).
    """
    raw_user_id = current_user.get("user_identifier")
    if not isinstance(raw_user_id, str) or not raw_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    resolver = get_user_identity_resolver(request)

    # Fast path (create/read only): if the hub already has a turn for this
    # conversation owned by the caller, they own it — no DB hit needed. The
    # subscribe/ack path needs the full owner set for the sole-owner check, so
    # it always queries below.
    hub = _get_hub(request)
    caller_owns_hub_turn = any(
        turn.user_id == raw_user_id for turn in hub.active_turns(conversation_id)
    )
    if allow_new and caller_owns_hub_turn:
        return raw_user_id

    # Persisted owners across ALL interface types.
    db_context = Database(request.app.state.database_engine)
    owners = await db_context.message_history.get_conversation_owner_ids(
        conversation_id
    )
    if not owners:
        # Brand-new / empty conversation: allowed for everyone, including the
        # subscribe path. The always-on live-update stream attaches to the
        # user's own freshly-created conversation BEFORE any message is sent, so
        # this must not 404. Conversation ids are unguessable UUIDs, so an empty
        # subscribe is not a meaningful way to wait on someone else's future
        # conversation.
        return raw_user_id
    if not _caller_is_sole_canonical_owner(resolver, owners, raw_user_id):
        # The caller is not the conversation's sole canonical owner: either they
        # are not an owner at all, or it is a genuine multi-owner conversation
        # which fans out every event to every subscriber with no per-user
        # isolation. (Empty conversations are handled above.)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return raw_user_id


async def _ensure_user_owns_persisted_conversation(
    request: Request,
    current_user: Mapping[str, object],
    conversation_id: str,
) -> str:
    """Return the owner id for a non-empty conversation owned by the caller."""
    user_id = await _ensure_user_owns_conversation(
        request, current_user, conversation_id, allow_new=False
    )
    db_context = Database(request.app.state.database_engine)
    if not await db_context.message_history.get_conversation_owner_ids(conversation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return user_id


def _share_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _get_active_conversation_share(
    request: Request,
    db_context: Database,
    token: str,
) -> ConversationShare:
    """Resolve an active token without revealing why an invalid share failed."""
    if len(token) != 43:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    share = await db_context.conversation_shares.get_by_token_hash(
        _share_token_hash(token)
    )
    if share is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    owners = await db_context.message_history.get_conversation_owner_ids(
        share.conversation_id
    )
    resolver = get_user_identity_resolver(request)
    if not owners or not _caller_is_sole_canonical_owner(
        resolver, owners, share.owner_user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return share


# ----------------------------------------------------------------------- #
# Resumable streaming endpoints (M0)
# ----------------------------------------------------------------------- #


@chat_api_router.post("/v1/chat/turns")
async def api_chat_create_turn(
    payload: ChatTurnRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    default_processing_service: Annotated[
        ProcessingService, Depends(get_processing_service)
    ],
    web_chat_interface: Annotated["WebChatInterface", Depends(get_web_chat_interface)],
) -> ChatTurnResponse:
    """Kick off a new chat turn.

    Idempotent on ``payload.turn_id``. If a turn with the same id already
    exists for this conversation, the existing identity is returned and no
    new producer is started.
    """
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    interface_type = payload.interface_type or "api"
    user_name = _user_name_for_chat(current_user)
    hub = _get_hub(request)

    # Enforce ownership before persisting anything: a brand-new conversation is
    # allowed through, but posting into a conversation that already belongs to
    # another user is rejected (404, not 403). This must run before the user
    # message is written, otherwise an attacker could "self-add" to a victim's
    # conversation and then pass the owner check on subsequent calls.
    user_id = await _ensure_user_owns_conversation(
        request, current_user, conversation_id, allow_new=True
    )

    # Idempotency short-circuit: if we already have this turn registered,
    # return its identity instead of starting a second producer.
    existing = hub.get_turn(conversation_id, payload.turn_id)
    if existing is not None:
        if existing.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return ChatTurnResponse(
            turn_id=existing.turn_id,
            conversation_id=conversation_id,
            first_seq=existing.first_seq,
        )

    # Durable idempotency: the hub is in-memory, so a turn_id retried after a
    # backend restart (or after the completed turn was pruned from the hub) is
    # invisible to the fast-path above. Without this the producer would persist
    # a SECOND user message under the same turn_id and re-drive the LLM. Consult
    # the database — the user message is the durable record of "this turn was
    # already started" — and return the existing identity instead of starting a
    # duplicate producer.
    idem_db = Database(request.app.state.database_engine)
    existing_user_row = await idem_db.message_history.get_user_row_by_turn_id(
        payload.turn_id
    )
    # The user row is now written before the producer runs (so a pre-start
    # Stop keeps the prompt durable), so its mere existence no longer implies
    # the turn produced a reply. Check for a TERMINAL assistant row to tell a
    # finished turn (reload shows the reply) from one interrupted by a
    # crash/restart mid-turn — including one that crashed after an
    # intermediate tool-calling row but before its final reply (those rows
    # carry tool_calls and are not terminal). The client surfaces a recovery
    # path for an interrupted turn instead of silently showing the prompt.
    turn_has_terminal_reply = (
        await idem_db.message_history.has_terminal_reply_for_turn(payload.turn_id)
        if existing_user_row is not None
        else False
    )
    if existing_user_row is not None:
        if (
            existing_user_row.get("conversation_id") != conversation_id
            or existing_user_row.get("user_id") != user_id
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return ChatTurnResponse(
            turn_id=payload.turn_id,
            conversation_id=conversation_id,
            first_seq=0,
            already_complete=True,
            incomplete=not turn_has_terminal_reply,
        )

    # One turn at a time per conversation. A second turn started while the first
    # is mid-tool overlaps two LLM loops on one history: they interleave their
    # writes, and the new turn replays a tool call whose result the running turn
    # has not written yet. Clients reach here by mistake, not by intent — the
    # composer means to STEER a running turn, and falls back to a plain send only
    # when it has lost track of the turn (e.g. across an app suspend). Hand back
    # the turn id it lost so it can steer that instead of starting a rival turn.
    #
    # This is the early, cheap rejection: it spares the attachment upload work
    # below in the common case. It is NOT the guarantee — the setup between here
    # and ``start_turn`` awaits, so a rival POST can pass this check too. The
    # authoritative check is ``reject_if_running`` on ``start_turn``, which runs
    # under the same lock as the registration; both raise the same 409.
    # Any running turn blocks, not just one whose raw user_id matches: ownership
    # was already settled above (sole canonical owner), and one person reaching
    # the conversation through two linked raw identities would otherwise slip a
    # rival turn past this.
    running_turn = next(
        (
            turn
            for turn in hub.active_turns(conversation_id)
            if turn.status == "running"
        ),
        None,
    )
    if running_turn is not None:
        raise _running_turn_conflict(conversation_id, payload.turn_id, running_turn)

    # Resolve processing service profile.
    selected_processing_service: ProcessingService = default_processing_service
    if payload.profile_id:
        registry = getattr(request.app.state, "processing_services", {})
        candidate = registry.get(payload.profile_id)
        if candidate and candidate.kind == "remote":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Profile '{payload.profile_id}' is remote-only and cannot "
                    "be used for direct chat."
                ),
            )
        if candidate:
            selected_processing_service = candidate

    # Process attachments (uses a short-lived DB context just for the upload
    # bookkeeping; the producer task gets its own context for streaming).
    trigger_content_parts: list[ContentPartDict] = [text_content(payload.prompt)]
    trigger_attachments: list[MessageAttachmentMetadata] | None = None
    if payload.attachments:
        attachment_registry = await get_attachment_registry(request)
        setup_db = Database(request.app.state.database_engine)
        # Reuse the existing helper from this module; it expects a
        # ChatPromptRequest-shaped payload.
        shim_payload = ChatPromptRequest(
            prompt=payload.prompt,
            conversation_id=conversation_id,
            profile_id=payload.profile_id,
            interface_type=interface_type,
            attachments=payload.attachments,
        )
        (
            trigger_content_parts,
            trigger_attachments,
        ) = await _process_user_attachments(
            shim_payload,
            conversation_id,
            attachment_registry,
            setup_db,
            user_id,
        )

    # Fetch the attachment registry without raising: the producer only needs
    # it to resolve attachment metadata for attach_to_response tool calls, so
    # a turn with no such tool calls works fine without one.
    attachment_registry = getattr(request.app.state, "attachment_registry", None)
    confirmation_service = _get_confirmation_service(request)
    confirmation_result_waiters = _get_confirmation_result_waiters(request)

    # Cooperative interrupt/steer handle for this turn. Stored on the TurnRecord
    # (atomically, inside start_turn) and passed to the producer so the
    # cancel/steer endpoints can reach the running LLM loop.
    mid_turn_controller = WebMidTurnController()

    # Register the turn synchronously and publish turn_started into the hub
    # buffer BEFORE the producer task starts. This closes the start-of-turn
    # race: a follow-up GET /stream?from_seq=0 will always see turn_started.
    try:
        turn = await hub.start_turn(
            conversation_id,
            turn_id=payload.turn_id,
            user_id=user_id,
            started_at=datetime.now(UTC),
            mid_turn_controller=mid_turn_controller,
            reject_if_running=True,
        )
    except ConversationTurnRunningError as exc:
        # A rival turn was admitted while this request did its setup (attachment
        # processing awaits above). The hub refused registration under its lock,
        # so exactly one of the two racing kickoffs proceeds.
        raise _running_turn_conflict(
            conversation_id, payload.turn_id, exc.turn
        ) from exc
    except TurnAlreadyExistsError as exc:
        # Lost a race with another concurrent POST: treat it as idempotent. The
        # loser returns here WITHOUT inserting the user message (the winner does
        # that below), so concurrent retries can't double-insert the prompt.
        if exc.turn.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
        return ChatTurnResponse(
            turn_id=exc.turn.turn_id,
            conversation_id=conversation_id,
            first_seq=exc.turn.first_seq,
        )

    # Persist the user message durably now — serialized by start_turn (only the
    # winner reaches here) and BEFORE the cancellable producer task is launched,
    # so a Stop that cancels the producer before its coroutine runs still leaves
    # the prompt durable. The producer's _prepare_turn_messages_for_llm is
    # idempotent on turn_id, so it reuses this row instead of inserting a
    # duplicate. ``payload.prompt`` matches what the producer would store (the
    # first text part of the trigger content).
    try:
        user_msg_db = Database(request.app.state.database_engine)
        # Read the pre-turn history and context taint BEFORE the prompt is
        # committed. Anything failing after that write strands the prompt: the
        # retry carries the same turn_id, matches the durable idempotency branch
        # above, and returns already_complete instead of running the turn.
        # Taint-wise this ordering is also the conservative one — the prompt
        # carries empty taint, so including it could only push an older (and
        # possibly tainted) row out of the history window.
        history_limit, history_max_age = (
            selected_processing_service.context_preparer.get_history_limits(
                interface_type
            )
        )
        initial_history_messages = await user_msg_db.message_history.get_recent(
            interface_type=interface_type,
            conversation_id=conversation_id,
            limit=history_limit,
            max_age=history_max_age,
            processing_profile_id=(selected_processing_service.service_config.id),
            subconversation_id=None,
            current_time=selected_processing_service.clock.now(),
        )
        initial_history_taint_metadata = merge_history_taint(
            initial_history_messages
        ).to_metadata()
        initial_context_taint_state = TurnTaintState.empty()
        # Gated exactly as the turn itself gates the context (see
        # ProcessingService._prepare_turn_messages_for_llm): a profile that never
        # receives the aggregated context was never exposed to its taint, and
        # stamping it here anyway would make a web turn dirtier than the same
        # profile's Telegram turn.
        if selected_processing_service.service_config.include_aggregated_context:
            for source in await selected_processing_service.context_preparer.aggregate_context_taint_sources():
                initial_context_taint_state = initial_context_taint_state.add_source(
                    source
                )
        initial_context_taint_metadata = initial_context_taint_state.to_metadata()
        initial_live_taint_state = TurnTaintState.from_metadata(
            initial_history_taint_metadata
        )
        for source in initial_context_taint_state.sources:
            initial_live_taint_state = initial_live_taint_state.add_source(source)
        initial_live_taint_metadata = initial_live_taint_state.to_metadata()

        await user_msg_db.message_history.add_message(
            UserMessage(
                content=payload.prompt,
                taint_metadata=TurnTaintState.empty().to_metadata(),
            ),
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=f"temp_{payload.turn_id}",
            turn_id=payload.turn_id,
            timestamp=datetime.now(UTC),
            user_id=user_id,
            attachments=trigger_attachments,
            processing_profile_id=selected_processing_service.service_config.id,
        )
    except Exception:
        # The turn is registered in the hub but no producer task exists yet (and
        # thus no done-callback safety net), so without ending it here the
        # TurnRecord would wedge at 'running'. End it, then propagate.
        await hub.end_turn(
            conversation_id,
            turn_id=payload.turn_id,
            status="failed",
            error="An internal error occurred.",
        )
        raise

    # Now that the user message is committed, ping the owner's activity stream so
    # the conversation surfaces (or bumps) in their list. Done here rather than in
    # hub.start_turn because the list endpoint only lists persisted messages — a
    # ping before this commit would have a client refetch a list that doesn't yet
    # include the conversation (and clobber any optimistic row).
    await hub.publish_activity(
        conversation_id,
        user_id=user_id,
        reason="turn_started",
    )

    # If the producer task is cancelled before its coroutine ever runs (a Stop
    # in the window before its first slice), it never persists the stopped
    # assistant marker. The hub's safety net invokes this so a refresh still
    # shows the stopped turn rather than a prompt with no reply.
    async def _persist_orphan_stopped_reply() -> None:
        await persist_stopped_reply(
            request.app.state.database_engine,
            interface_type=interface_type,
            conversation_id=conversation_id,
            turn_id=payload.turn_id,
            user_id=user_id,
            reply_text="",
            processing_profile_id=selected_processing_service.service_config.id,
            initial_history_taint_metadata=initial_history_taint_metadata,
            initial_context_taint_metadata=initial_context_taint_metadata,
            live_taint_metadata=initial_live_taint_metadata,
        )

    producer_task = asyncio.create_task(
        run_turn_producer(
            app_state=request.app.state,
            hub=hub,
            processing_service=selected_processing_service,
            web_chat_interface=web_chat_interface,
            confirmation_service=confirmation_service,
            confirmation_result_waiters=confirmation_result_waiters,
            attachment_registry=attachment_registry,
            conversation_id=conversation_id,
            turn_id=payload.turn_id,
            user_id=user_id,
            user_name=user_name,
            interface_type=interface_type,
            trigger_content_parts=trigger_content_parts,
            trigger_attachments=trigger_attachments,
            initial_history_taint_metadata=initial_history_taint_metadata,
            initial_context_taint_metadata=initial_context_taint_metadata,
            mid_turn_input_provider=mid_turn_controller,
        ),
        name=f"chat-turn:{conversation_id}:{payload.turn_id}",
    )
    hub.attach_producer_task(
        conversation_id,
        payload.turn_id,
        producer_task,
        on_orphan_cancel=_persist_orphan_stopped_reply,
    )

    return ChatTurnResponse(
        turn_id=payload.turn_id,
        conversation_id=conversation_id,
        first_seq=turn.first_seq,
    )


@chat_api_router.get(
    "/v1/chat/conversations/{conversation_id}/stream", response_model=None
)
async def api_chat_conversation_stream(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    from_seq: int = 0,
    ack_seq: int = -1,
    follow: bool = False,
    event_types: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Subscribe to a conversation's resumable event stream from ``from_seq``.

    * Replays buffered events ``>= from_seq`` then tails live ones.
    * On buffer underrun (``from_seq`` below the oldest cached seq) returns
      410 Gone with ``active_turns`` populated, so the client can render a
      "still thinking" placeholder while falling back to history reload.
    * ``ack_seq`` lets a reconnecting client tell the server it already
      received events up to that seq; the hub uses this to suppress redundant
      disconnect-push notifications.
    * ``event_types`` is an optional comma-separated allow-list (e.g.
      ``event_types=message,turn_ended``). When set, only events whose type is
      in the set are emitted — EXCEPT the lifecycle/control frames
      ``turn_ended``, ``heartbeat`` and ``stream_dropped``, which are always
      emitted so the client still knows when to stop / reload / reconnect. The
      filter applies to both replayed and live events. Follow clients that only
      reload history use this to skip the token firehose.
    * ``follow`` controls lifetime:
        - ``false`` (default): "watch my reply / resume" mode. Drain the
          buffer and stream any in-flight turn through its ``turn_ended``,
          then close once no turn is still running. This is the dominant flow
          (send-then-watch, refresh-mid-turn resume) and terminates cleanly.
        - ``true``: always-on live-updates mode. Stay open with periodic
          heartbeats so the connection also surfaces turns that start later
          (e.g. a reply triggered from another device). Clients that prefer
          a reconnect loop can keep using ``follow=false`` and re-subscribe.
    """
    user_id = await _ensure_user_owns_conversation(
        request, current_user, conversation_id, allow_new=False
    )
    hub = _get_hub(request)
    allowed_event_types = _parse_event_types(event_types)
    shutdown_event = _get_shutdown_event(request)
    heartbeat_interval = _get_heartbeat_interval(request)

    try:
        handle = await hub.subscribe(
            conversation_id, from_seq=from_seq, ack_seq=ack_seq
        )
    except OutOfBufferError as exc:
        return JSONResponse(
            status_code=status.HTTP_410_GONE,
            content={
                "reason": "out_of_buffer",
                "requested_from_seq": exc.requested_from_seq,
                "min_available_seq": exc.min_available_seq,
                "active_turns": [
                    info.model_dump(mode="json")
                    for info in _owned_turns(hub, conversation_id, user_id)
                ],
            },
        )

    def _has_running_turn() -> bool:
        return any(
            turn.user_id == user_id and turn.status == "running"
            for turn in hub.active_turns(conversation_id)
        )

    def _drain_queue() -> list[StreamEvent]:
        """Pop everything currently sitting in the live queue, non-blocking.

        ``subscribe`` atomically splits events into the replay snapshot and the
        live queue, and ``end_turn`` flips the turn non-running while enqueuing
        ``turn_ended``. A turn that ends in the window between the snapshot and
        the running-turn check leaves its tail + ``turn_ended`` ONLY in the
        queue, so a non-follow early return must flush the queue first or it
        silently drops the reply's tail and the turn_ended frame.
        """
        drained: list[StreamEvent] = []
        while True:
            try:
                drained.append(handle.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    async def event_generator() -> AsyncGenerator[str]:
        # NOTE: writing an event to this SSE socket is NOT proof the client read
        # and handled it. Delivery (which suppresses the disconnect push) is only
        # recorded on an *explicit* client ack — the ``ack_seq`` query param on
        # (re)subscribe or ``POST /v1/chat/ack`` after the client processes
        # turn_ended — never here on send.
        try:
            # Flush the response head immediately with an initial heartbeat. An idle
            # ``follow=true`` stream's first real byte is otherwise the 30s heartbeat,
            # and the production front door (Envoy) does not forward the response
            # headers to the client until it receives that first upstream byte. iOS
            # ``URLSession.bytes`` resolves on headers, so without an immediate byte
            # the resync's stream establishment stalls until it times out (~8s) and
            # the connection indicator is stuck degraded. A ``heartbeat`` frame (not a
            # bare ``:`` comment) is used deliberately: the iOS SSE parser dispatches a
            # default ``message`` event for a comment-only frame, which would trigger a
            # spurious history/list reload on every reconnect, whereas ``heartbeat`` is
            # an explicit no-op control frame every client already ignores.
            yield "event: heartbeat\ndata: {}\n\n"
            # Replay the snapshot first; then tail live events from the queue.
            for replayed in handle.replayed_events:
                if _should_emit(replayed.type, allowed_event_types):
                    yield format_sse_event(replayed)

            # In non-follow mode, if nothing is running after the replay there
            # is nothing left to watch. Drain anything that landed in the queue
            # in the snapshot/check window (a turn that just ended leaves its
            # tail + turn_ended there) before closing, or those events are lost.
            if not follow and not _has_running_turn():
                for drained in _drain_queue():
                    if _should_emit(drained.type, allowed_event_types):
                        yield format_sse_event(drained)
                return

            while True:
                queue_get = asyncio.ensure_future(handle.queue.get())
                shutdown_wait = asyncio.ensure_future(shutdown_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {queue_get, shutdown_wait},
                        timeout=heartbeat_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not queue_get.done():
                        queue_get.cancel()
                    shutdown_wait.cancel()

                if shutdown_event.is_set():
                    # Graceful SIGTERM: close promptly so the server can drain.
                    # The client reconnects (or reloads history) once the backend
                    # is back, rather than holding a heartbeat stream open forever.
                    yield (
                        'event: stream_dropped\ndata: {"reason": "server_shutdown"}\n\n'
                    )
                    return

                if queue_get not in done:
                    # Heartbeat timeout. If the hub dropped this subscriber (its
                    # queue overflowed), tell the client to reconnect instead of
                    # silently heartbeating into a discarded subscription.
                    if not hub.is_subscribed(conversation_id, handle.queue):
                        yield (
                            "event: stream_dropped\n"
                            'data: {"reason": "queue_overflow"}\n\n'
                        )
                        return
                    _notify_heartbeat_observer(request)
                    yield "event: heartbeat\ndata: {}\n\n"
                    if not follow and not _has_running_turn():
                        for drained in _drain_queue():
                            if _should_emit(drained.type, allowed_event_types):
                                yield format_sse_event(drained)
                        return
                    continue

                event = queue_get.result()
                if _should_emit(event.type, allowed_event_types):
                    yield format_sse_event(event)
                if (
                    event.type == "turn_ended"
                    and not follow
                    and not _has_running_turn()
                ):
                    return
        except asyncio.CancelledError:
            raise
        finally:
            # Synchronous + lock-free so it still runs when the ASGI server
            # cancels this generator on client disconnect (an await here would
            # re-raise CancelledError and leak the subscriber queue).
            hub.unsubscribe(conversation_id, handle.queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@chat_api_router.get("/v1/chat/activity/stream", response_model=None)
async def api_chat_activity_stream(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> StreamingResponse:
    """Account-global stream of conversation-list change pings.

    Emits a compact ``conversation_activity`` frame whenever a conversation the
    caller owns gains new visible activity (a turn starts or ends, or a
    delegated/scheduled reply lands). The payload carries no message content —
    only the conversation id, a coarse reason, and a timestamp — so the client
    reacts by re-fetching the authoritative, ownership-filtered conversation
    list (``GET /v1/chat/conversations``). It is the live counterpart to
    pull-to-refresh: it keeps the list fresh for activity happening outside
    whichever thread the user currently has open.

    Always-on: stays open with 30s heartbeats and is resumed by the client on
    disconnect. Scoped to the caller by an exact ``user_identifier`` match; a
    missed or mis-scoped ping is harmless because the authoritative list fetch
    does the real ownership filtering.
    """
    raw_user_id = current_user.get("user_identifier")
    if not isinstance(raw_user_id, str) or not raw_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    hub = _get_hub(request)
    shutdown_event = _get_shutdown_event(request)
    heartbeat_interval = _get_heartbeat_interval(request)
    handle = hub.subscribe_activity(raw_user_id)

    async def event_generator() -> AsyncGenerator[str]:
        try:
            # Flush the response head immediately with an initial heartbeat so a
            # buffering front door forwards the headers to the client without waiting
            # for the first real heartbeat (see the follow-stream endpoint for the
            # full rationale, including why a ``heartbeat`` frame is used rather than a
            # bare ``:`` comment). The activity stream is idle even more often than the
            # follow stream, so this matters here too.
            yield "event: heartbeat\ndata: {}\n\n"
            while True:
                queue_get = asyncio.ensure_future(handle.queue.get())
                shutdown_wait = asyncio.ensure_future(shutdown_event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        {queue_get, shutdown_wait},
                        timeout=heartbeat_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not queue_get.done():
                        queue_get.cancel()
                    shutdown_wait.cancel()

                # Deliver a ready activity event before honoring shutdown so a
                # ping that landed in the same wait cycle as a shutdown signal is
                # not dropped.
                if queue_get not in done:
                    if shutdown_event.is_set():
                        yield (
                            "event: stream_dropped\n"
                            'data: {"reason": "server_shutdown"}\n\n'
                        )
                        return
                    # Heartbeat tick. If the hub dropped this subscriber (its
                    # queue overflowed), tell the client to reconnect instead of
                    # heartbeating into a discarded subscription.
                    if not hub.is_activity_subscribed(handle.queue):
                        yield (
                            "event: stream_dropped\n"
                            'data: {"reason": "queue_overflow"}\n\n'
                        )
                        return
                    _notify_heartbeat_observer(request)
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                activity = queue_get.result()
                payload = json.dumps({
                    "conversation_id": activity.conversation_id,
                    "reason": activity.reason,
                    "timestamp": activity.timestamp.isoformat(),
                })
                yield f"event: conversation_activity\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            # Synchronous + lock-free so it still runs when the ASGI server
            # cancels this generator on client disconnect.
            hub.unsubscribe_activity(handle.queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@chat_api_router.post("/v1/chat/ack")
async def api_chat_ack(
    payload: AckRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> AckResponse:
    """Record a client acknowledgement of received events.

    Used by clients that want explicit push suppression without keeping an
    SSE stream open: send the highest received seq after handling a notify
    push, and the hub will mark any covered turn as delivered.
    """
    await _ensure_user_owns_conversation(
        request, current_user, payload.conversation_id, allow_new=False
    )
    hub = _get_hub(request)
    await hub.ack_conversation(payload.conversation_id, payload.ack_seq)
    return AckResponse(ok=True)


@chat_api_router.post("/v1/chat/turns/{turn_id}/cancel")
async def api_chat_cancel_turn(
    turn_id: str,
    payload: ChatTurnCancelRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> ChatTurnCancelResponse:
    """Stop a running turn (the web "Stop generating" button).

    Graceful-then-hard, mirroring Telegram's ``/interrupt``: request a
    cooperative interrupt (so the loop halts cleanly at its next boundary and
    the turn is marked ``cancelled``), then cancel the producer task to also
    interrupt a long in-flight LLM/tool call. Idempotent: cancelling an
    already-finished turn is a no-op that echoes the terminal status.
    """
    await _ensure_user_owns_conversation(
        request, current_user, payload.conversation_id, allow_new=False
    )
    hub = _get_hub(request)
    turn = hub.get_turn(payload.conversation_id, turn_id)
    if turn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    running = turn.status == "running"

    if running:
        # Request a cooperative interrupt first so the producer resolves the turn
        # to 'cancelled' (not 'failed') when the CancelledError surfaces.
        controller = turn.mid_turn_controller
        if isinstance(controller, WebMidTurnController):
            controller.request_interrupt()
        if turn.task is not None and not turn.task.done():
            turn.task.cancel()

    # Reject any tool confirmations this turn was waiting on. Cancelling the
    # producer task unblocks the in-memory waiter but leaves the durable
    # confirmation request 'pending' — the pending-confirmations UI could still
    # approve it later, enqueueing a state-changing tool with no turn left to
    # receive the result. This runs on the already-finished path too so a retry
    # after a transient failure re-attempts; a failure to fully secure the turn
    # propagates (503) rather than reporting a clean stop.
    # Reject under the turn's OWNER (turn.user_id), not the caller's raw
    # identifier: confirmations were targeted at whoever started the turn, and
    # the same canonical user may be cancelling through a different raw identity
    # (the ownership check above already authorized them).
    await _reject_pending_confirmations_for_turn(
        request,
        turn_id=turn_id,
        user_id=turn.user_id,
    )

    if not running:
        return ChatTurnCancelResponse(
            turn_id=turn_id,
            conversation_id=payload.conversation_id,
            status=turn.status,
            already_complete=True,
        )
    return ChatTurnCancelResponse(
        turn_id=turn_id,
        conversation_id=payload.conversation_id,
        status="cancelling",
    )


async def _reject_pending_confirmations_for_turn(
    request: Request,
    *,
    turn_id: str,
    user_id: str,
) -> None:
    """Reject the cancelled turn's still-pending durable tool confirmations.

    Every confirmation raised within a turn carries that turn's user message as
    its ``source_message_internal_id``, so we reject exactly the confirmations
    for this turn without touching a concurrent turn's. Since Stop's safety
    relies on this, a confirmation we cannot reject (or a failure to even list
    them) raises 503 so the caller retries rather than treating the turn as
    safely stopped. Already-resolved/expired confirmations are not failures.
    """
    confirmation_service = _get_confirmation_service(request)
    try:
        db = Database(request.app.state.database_engine)
        user_row = await db.message_history.get_user_row_by_turn_id(turn_id)
        if user_row is None:
            return
        source_internal_id = user_row["internal_id"]
        pending = await confirmation_service.list_pending_for_user(user_id=user_id)
    except Exception as exc:
        logger.warning(
            "Failed to list pending confirmations for cancelled turn=%s",
            turn_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify the stopped turn's tool confirmations; retry.",
        ) from exc

    unrejected = 0
    for confirmation in pending:
        if confirmation["source_message_internal_id"] != source_internal_id:
            continue
        try:
            await confirmation_service.reject(
                request_id=confirmation["id"],
                rejecting_user_id=user_id,
                rejecting_interface="web",
            )
        except (
            ConfirmationExpiredError,
            ConfirmationAlreadyResolvedError,
            ConfirmationNotFoundError,
        ):
            # Already resolved/expired elsewhere — nothing to reject.
            continue
        except Exception:
            unrejected += 1
            logger.warning(
                "Failed to reject confirmation %s for cancelled turn=%s",
                confirmation["id"],
                turn_id,
                exc_info=True,
            )

    if unrejected:
        # Some state-changing confirmation is still approvable; don't report a
        # clean stop. The producer is already cancelled, so a retry only re-runs
        # this rejection (idempotent) until it succeeds.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reject all of the stopped turn's tool confirmations; retry.",
        )


@chat_api_router.post("/v1/chat/turns/{turn_id}/steer")
async def api_chat_steer_turn(
    turn_id: str,
    payload: ChatTurnSteerRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> ChatTurnSteerResponse:
    """Inject a steering message into a running turn without restarting it.

    The message is queued on the turn's controller; the LLM loop drains it after
    the next tool round and re-feeds it to the model as ``[MID-TURN USER
    UPDATE]`` context. Returns 409 if the turn has already finished or is not
    steerable, so the client can fall back to starting a new turn.
    """
    await _ensure_user_owns_conversation(
        request, current_user, payload.conversation_id, allow_new=False
    )
    hub = _get_hub(request)
    turn = hub.get_turn(payload.conversation_id, turn_id)
    if turn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if payload.input_id is not None and payload.input_id in turn.accepted_steer_inputs:
        # A retry of a submission this turn already accepted, arriving after it
        # finished. Answering 409 would send the client down the resend path and
        # repeat an instruction the turn has already acted on. It gets the floor
        # the original request was told, not the current head: the turn may have
        # published this message's echo since, and a client replaying from the
        # head would start after the very event it is waiting for.
        return ChatTurnSteerResponse(
            turn_id=turn_id,
            conversation_id=payload.conversation_id,
            accepted=True,
            queued_after_seq=turn.accepted_steer_inputs[payload.input_id],
        )
    controller = turn.mid_turn_controller
    if turn.status != "running" or not isinstance(controller, WebMidTurnController):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn is not running; start a new turn instead.",
        )

    # Read the stream head BEFORE queueing, so the floor is conservative: the
    # echo of this message is published strictly after it, while every event
    # already on the stream sits at or below it.
    queued_after_seq = hub.latest_seq(payload.conversation_id)
    queued = await controller.add_input(
        MidTurnUserInput(
            content=payload.prompt,
            user_name=_user_name_for_chat(current_user),
            interface_message_id=payload.input_id,
        )
    )
    if payload.input_id is not None and turn.status == "running":
        # Recorded on the turn, which outlives the controller, so a retry that
        # arrives after the turn ends is still recognised as already delivered.
        # setdefault, not assignment: a retry the controller deduped must keep
        # the floor its first attempt was given.
        #
        # Re-checked after the enqueue, because the producer can finish between
        # the status check above and this point — the message then sits on a
        # controller nobody will drain. Recording it anyway would have a later
        # retry told "delivered" for something that will never be acted on;
        # leaving it unrecorded lets that retry take the 409 that starts a new
        # turn. This narrows the window rather than closing it: a turn can also
        # be inside its final, tool-free iteration, past the drain but not yet
        # ended. The client's un-echoed-steer recovery is the guarantee there,
        # which is why an echo — not this 200 — is what settles a submission.
        turn.accepted_steer_inputs.setdefault(payload.input_id, queued_after_seq)
    if not queued:
        # A retry that raced the turn rather than outliving it: the controller
        # is still live and had already taken this submission. Answering 200
        # without queueing it again is what the client is asking for — it is
        # retrying because the first response was lost, not because it wants to
        # say the same thing twice.
        logger.info(
            "Steer input %s already queued for turn %s; not queueing it again",
            payload.input_id,
            turn_id,
        )
    return ChatTurnSteerResponse(
        turn_id=turn_id,
        conversation_id=payload.conversation_id,
        accepted=True,
        queued_after_seq=queued_after_seq,
    )


ApprovingInterface = Literal["web", "ios", "telegram"]


class ToolConfirmationRequest(BaseModel):
    """Request to confirm or reject a tool execution."""

    request_id: str = Field(..., description="Confirmation request ID")
    approved: bool = Field(..., description="Whether the tool execution is approved")
    conversation_id: str | None = Field(
        None, description="Optional conversation ID for validation"
    )
    approving_interface: ApprovingInterface = Field(
        "web",
        description="Interface that submitted the approval or rejection.",
    )


class ToolConfirmationResponse(BaseModel):
    """Response for tool confirmation request."""

    success: bool = Field(
        ..., description="Whether the confirmation was processed successfully"
    )
    message: str | None = Field(None, description="Optional status message")


class PendingToolConfirmation(BaseModel):
    """Pending durable tool confirmation visible to the current user."""

    request_id: str = Field(..., description="Confirmation request ID")
    tool_name: str = Field(..., description="Tool awaiting approval")
    tool_call_id: str | None = Field(None, description="Associated LLM tool call ID")
    confirmation_prompt: str = Field(..., description="Prompt shown to the user")
    # ast-grep-ignore: no-dict-any - Tool arguments vary per tool and cannot be statically typed
    args: dict[str, Any] = Field(..., description="Tool arguments awaiting approval")
    created_at: datetime = Field(..., description="Request creation timestamp")
    expires_at: datetime = Field(..., description="Request expiration timestamp")
    timeout_seconds: float = Field(
        ..., description="Seconds from creation until expiration"
    )
    time_remaining_seconds: float = Field(
        ..., description="Seconds from response generation until expiration"
    )


class PendingToolConfirmationsResponse(BaseModel):
    """Response containing pending durable tool confirmations."""

    confirmations: list[PendingToolConfirmation] = Field(
        ..., description="Pending confirmations for the current user"
    )


class ToolConfirmationDetail(BaseModel):
    """A single durable tool confirmation, including its current status.

    Unlike :class:`PendingToolConfirmation`, this is returned regardless of status so a client (for
    example the iOS confirmation modal opened from a push notification) can render the request even
    when it has already been approved, rejected, or expired.
    """

    request_id: str = Field(..., description="Confirmation request ID")
    tool_name: str = Field(..., description="Tool awaiting approval")
    tool_call_id: str | None = Field(None, description="Associated LLM tool call ID")
    confirmation_prompt: str = Field(..., description="Prompt shown to the user")
    # ast-grep-ignore: no-dict-any - Tool arguments vary per tool and cannot be statically typed
    args: dict[str, Any] = Field(..., description="Tool arguments awaiting approval")
    status: str = Field(
        ..., description="Current status: pending, approved, rejected, or expired"
    )
    created_at: datetime = Field(..., description="Request creation timestamp")
    expires_at: datetime = Field(..., description="Request expiration timestamp")
    time_remaining_seconds: float = Field(
        ..., description="Seconds from response generation until expiration"
    )


class ServiceProfile(BaseModel):
    """Information about an available service profile."""

    id: str = Field(..., description="Profile identifier")
    description: str = Field(..., description="Profile description")
    llm_model: str | None = Field(None, description="LLM model used by this profile")
    available_tools: list[str] = Field(
        default_factory=list, description="Available tools for this profile"
    )
    enabled_mcp_servers: list[str] = Field(
        default_factory=list, description="Enabled MCP servers"
    )
    delegation_only: bool = Field(
        default=False,
        description="If true, this profile is a remote delegation target and cannot be used for direct chat",
    )


class ProfilesResponse(BaseModel):
    """Response containing available service profiles."""

    profiles: list[ServiceProfile] = Field(
        ..., description="List of available service profiles"
    )
    default_profile_id: str = Field(..., description="ID of the default profile")


@chat_api_router.post("/v1/chat/send_message")  # Path relative to the prefix in api.py
async def api_chat_send_message(
    payload: ChatPromptRequest,
    request: Request,  # To access app.state for config and service registry
    current_user: Annotated[dict, Depends(get_current_user)],
    default_processing_service: Annotated[
        ProcessingService, Depends(get_processing_service)
    ],  # Renamed for clarity
    db_context: Annotated[Database, Depends(get_db)],
    web_chat_interface: Annotated["WebChatInterface", Depends(get_web_chat_interface)],
) -> ChatMessageResponse:
    """
    Receives a user prompt via API, processes it using the specified or default
    ProcessingService, and returns the assistant's reply.
    """
    conversation_id = payload.conversation_id or str(uuid.uuid4())

    # Enforce conversation ownership before processing: a client may not post
    # into a conversation that already belongs to another user (404, not 403).
    user_id = await _ensure_user_owns_conversation(
        request, current_user, conversation_id, allow_new=True
    )

    # turn_id idempotency (minimal): the client may supply a UUID so a retried
    # /send_message returns the already-persisted reply instead of re-driving
    # the LLM and double-persisting. Mirrors /turns — in-memory hub fast path
    # plus durable DB fallback. The hub turn taken below is the reservation, not
    # the idempotency record: it is discarded when the send ends, so a turn that
    # produced a reply is recognised from the database rather than from memory.
    response_turn_id = payload.turn_id or str(uuid.uuid4())
    hub = _get_hub(request)
    if payload.turn_id is not None:
        existing_turn = hub.get_turn(conversation_id, payload.turn_id)
        if existing_turn is not None and existing_turn.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        existing_user_row = await db_context.message_history.get_user_row_by_turn_id(
            payload.turn_id
        )
        if existing_user_row is not None and (
            existing_user_row.get("conversation_id") != conversation_id
            or existing_user_row.get("user_id") != user_id
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        existing_response = await _existing_send_message_response(
            db_context, conversation_id, payload.turn_id
        )
        if existing_response is not None:
            return existing_response

    # Determine which processing service to use
    selected_processing_service = default_processing_service
    profile_id_requested = payload.profile_id

    if profile_id_requested:
        logger.info(
            f"API chat request for profile_id: '{profile_id_requested}'. Conversation ID: {conversation_id}, Prompt: '{payload.prompt[:100]}...'"
        )
        processing_services_registry = getattr(
            request.app.state, "processing_services", {}
        )
        candidate = processing_services_registry.get(profile_id_requested)
        if candidate and candidate.kind == "remote":
            raise HTTPException(
                status_code=400,
                detail=f"Profile '{profile_id_requested}' is a remote delegation-only profile and cannot be used for direct chat.",
            )
        if candidate:
            selected_processing_service = candidate
            logger.info(
                f"Using ProcessingService for profile_id: '{profile_id_requested}'."
            )
        else:
            logger.warning(
                f"Profile_id '{profile_id_requested}' not found in registry. Falling back to default profile: '{default_processing_service.service_config.id}'."
            )
    else:
        logger.info(
            f"API chat request (no profile_id specified). Using default profile: '{default_processing_service.service_config.id}'. Conversation ID: {conversation_id}, Prompt: '{payload.prompt[:100]}...'"
        )

    # One turn at a time per conversation: hold the same hub reservation the
    # streaming path takes, so a non-streaming send (an iOS App Intent, Siri, or
    # an API client) cannot drive a second LLM loop over a history a running turn
    # is still writing to. Rivals in either direction get the same 409.
    async with _reserve_non_streaming_turn(
        hub, conversation_id, turn_id=response_turn_id, user_id=user_id
    ) as reservation:
        # Process user attachments if present
        trigger_content_parts: list[ContentPartDict] = [
            {"type": "text", "text": payload.prompt}  # type: ignore[typeddict-item]  # Runtime dict matches TypedDict structure
        ]
        trigger_attachments: list[MessageAttachmentMetadata] | None = None

        if payload.attachments:
            # Only get attachment registry when we actually have attachments
            attachment_registry = await get_attachment_registry(request)
            (
                trigger_content_parts,
                trigger_attachments,
            ) = await _process_user_attachments(
                payload,
                conversation_id,
                attachment_registry,
                db_context,
                current_user["user_identifier"],
            )

        # Determine interface type - default to "api" if not specified
        interface_type = payload.interface_type or "api"

        # Call the new centralized interaction handler
        # user_name surfaces in the system prompt and message history, so derive it
        # from the authenticated user rather than a generic placeholder.
        user_name_for_api = _user_name_for_chat(current_user)

        # Get chat_interfaces registry from app state for cross-interface messaging
        chat_interfaces = getattr(request.app.state, "chat_interfaces", None)
        confirmation_ui_managers = getattr(
            request.app.state,
            "confirmation_ui_managers",
            None,
        )

        # Non-streaming callers (e.g. iOS App Intents / Siri) cannot wait on a live
        # confirmation channel, so a tool needing approval records a durable pending
        # confirmation the user can approve later from another client (the
        # confirmation service push-notifies them). The deferred tool result tells
        # the model the action is awaiting approval so the reply reflects that.
        api_confirmation_service = _get_confirmation_service(request)

        async def api_confirmation_callback(
            interface_type: str,
            conversation_id: str,
            turn_id: str | None,
            tool_name: str,
            call_id: str,
            # ast-grep-ignore: no-dict-any - Tool arguments vary per tool and cannot be statically typed
            tool_args: dict[str, Any],
            timeout_seconds: float,
            context: ToolExecutionContext,
        ) -> ConfirmationOutcome:
            taint_state_json = (
                context.taint_tracker.snapshot().to_metadata()
                if context.taint_tracker is not None
                else None
            )
            durable_request = await create_durable_confirmation(
                confirmation_service=api_confirmation_service,
                db_context=context.db_context,
                target_user_id=current_user["user_identifier"],
                tool_name=tool_name,
                tool_call_id=call_id,
                tool_args=tool_args,
                confirmation_prompt=(
                    f"Do you want to execute '{tool_name}' with these parameters?"
                ),
                timeout_seconds=timeout_seconds,
                turn_id=turn_id,
                now=datetime.now(UTC),
                processing_profile_id=context.processing_profile_id,
                origin_interface_type=context.interface_type,
                origin_conversation_id=context.conversation_id,
                taint_state_json=taint_state_json,
            )
            return ConfirmationOutcome(
                kind="completed",
                result=(
                    f"I've requested your approval to run '{tool_name}' "
                    f"(request {durable_request['id']}). It hasn't run yet — approve it "
                    "from your pending confirmations to continue."
                ),
            )

        result = await selected_processing_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=interface_type,  # Use the interface_type from request or default "api"
            conversation_id=conversation_id,
            trigger_content_parts=trigger_content_parts,
            trigger_interface_message_id=None,  # API prompts don't have a prior interface ID
            user_name=user_name_for_api,
            user_id=current_user["user_identifier"],
            replied_to_interface_id=None,  # payload.replied_to_message_id is not available on ChatPromptRequest
            chat_interface=web_chat_interface,  # Use WebChatInterface for message delivery
            chat_interfaces=chat_interfaces,  # Pass all registered chat interfaces
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=api_confirmation_callback,
            trigger_attachments=trigger_attachments,  # Pass attachment metadata
            turn_id=response_turn_id,  # Persist under the (idempotency) turn_id
        )

        final_reply_content = result.text_reply
        final_assistant_message_internal_id = result.assistant_message_internal_id
        _final_reasoning_info = result.reasoning_info  # Not used by API response
        error_traceback = result.error_traceback
        _response_attachment_ids = (
            result.attachment_ids
        )  # Not yet included in API response

        if error_traceback:
            logger.error(
                f"Error processing API chat request for Conversation ID {conversation_id}: {error_traceback}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error processing request: {error_traceback if getattr(request.app.state, 'debug_mode', False) else 'An internal error occurred.'}",
            )

        if final_reply_content is None:
            logger.error(
                f"No final assistant reply content found for API chat. Conversation ID: {conversation_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Assistant did not provide a textual reply.",
            )

        # Fetch recent messages to get tool_calls if any
        tool_calls_response = None
        if final_assistant_message_internal_id:
            # Get recent messages from this conversation
            recent_messages = await db_context.message_history.get_recent(
                interface_type=interface_type,
                conversation_id=conversation_id,
                limit=5,  # Get last few messages
                max_age=timedelta(minutes=5),
            )
            # Find the most recent assistant message (repository returns typed LLMMessage objects)
            # Note: Cannot match by internal_id since typed messages don't include database metadata
            # Use the most recent AssistantMessage from the list
            assistant_msg = next(
                (
                    msg
                    for msg in reversed(recent_messages)
                    if isinstance(msg, AssistantMessage) and msg.tool_calls
                ),
                None,
            )
            if assistant_msg and assistant_msg.tool_calls:
                # Convert ToolCallItem objects to dicts for API response
                tool_calls_response = []
                for tc in assistant_msg.tool_calls:
                    if isinstance(tc, ToolCallItem):
                        # Ensure arguments is a JSON string
                        args = tc.function.arguments
                        if not isinstance(args, str):
                            args = json.dumps(args)
                        tool_calls_response.append({
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": args,
                            },
                        })
                    elif isinstance(tc, dict):
                        tool_calls_response.append(tc)

        # A follower of this conversation learns about the reply from the
        # reservation's ``turn_ended``, published as the ``async with`` exits:
        # the web and iOS follow-streams treat it exactly as they treat the
        # content-free ``message`` nudge — refetch history, refresh the
        # conversation list, ack the seq — and ``end_turn`` broadcasts the
        # account-global activity ping alongside it. Publishing a ``message``
        # nudge here as well would have every connected client fetch history and
        # the conversation list twice per send.
        reservation.mark_complete()
        return ChatMessageResponse(
            reply=final_reply_content,  # Back to original field name
            conversation_id=conversation_id,  # Return the used/generated conversation_id
            turn_id=response_turn_id,  # Return the turn_id generated for the response model
            attachments=trigger_attachments,  # Include processed attachments in response
            tool_calls=tool_calls_response,  # Include tool calls if any
        )


@chat_api_router.get("/v1/chat/conversations")
async def get_conversations(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
    limit: int = 20,
    offset: int = 0,
    interface_type: str | None = None,
    conversation_id: str | None = None,
    date_from: str | None = None,  # Expected as YYYY-MM-DD string
    date_to: str | None = None,  # Expected as YYYY-MM-DD string
) -> ConversationListResponse:
    """
    Get a list of chat conversations for the web interface.

    Filtered to conversations the current user owns, using the same identity-
    aware, sole-canonical-owner predicate as ``GET /messages`` and
    ``GET /stream`` (see ``_ensure_user_owns_conversation``). This keeps the
    History list and the per-conversation open consistent: the UI never lists a
    conversation it then 404s on opening.

    Args:
        limit: Maximum number of conversations to return
        offset: Number of conversations to skip for pagination
        interface_type: Filter by interface type (web, telegram, api, email)
        conversation_id: Filter by specific conversation ID
        date_from: Filter conversations with messages after this date (YYYY-MM-DD)
        date_to: Filter conversations with messages before this date (YYYY-MM-DD)

    Returns:
        List of conversation summaries the caller owns, with metadata
    """
    raw_user_id = current_user.get("user_identifier")
    if not isinstance(raw_user_id, str) or not raw_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    resolver = get_user_identity_resolver(request)

    # Parse date strings to datetime objects
    date_from_dt = None
    date_to_dt = None

    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date_from format: '{date_from}'. Expected YYYY-MM-DD format.",
            ) from e

    if date_to:
        try:
            # Set to end of day to include all messages from the target date
            date_to_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=UTC)
            date_to_dt = date_to_dt.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date_to format: '{date_to}'. Expected YYYY-MM-DD format.",
            ) from e

    # Restrict to conversations the caller solely (canonically) owns *in the query
    # itself*, so both the returned page AND ``count`` are ownership-filtered. A
    # Python post-filter would leave ``count`` promising conversations the client
    # can never page through, producing short/empty non-final pages that a client
    # stopping on an empty page treats as the end of the list. The caller's
    # equivalence set is every stored owner id that canonicalizes to its identity:
    # the configured aliases (own id plus Telegram/OIDC ids), widened by
    # canonicalizing every distinct stored owner id so historical un-normalized
    # forms (mixed case, padded ``Name <email>``) attribute exactly as the old
    # canonicalize-then-compare post-filter did.
    owner_user_ids = (
        resolver.owner_ids_canonicalizing_to(raw_user_id)
        if resolver is not None
        else {raw_user_id}
    )
    if resolver is not None:
        stored_owner_ids = (
            await db_context.message_history.list_distinct_user_message_owner_ids()
        )
        owner_user_ids |= {
            stored_id
            for stored_id in stored_owner_ids
            if resolver.canonicalize_owner_id(stored_id) == raw_user_id
        }

    summaries, total = await db_context.message_history.get_conversation_summaries(
        interface_type=interface_type,
        limit=limit,
        offset=offset,
        conversation_id=conversation_id,
        date_from=date_from_dt,
        date_to=date_to_dt,
        include_subconversations=False,
        owner_user_ids=owner_user_ids,
    )

    conversations = [
        ConversationSummary(
            conversation_id=summary["conversation_id"],
            last_message=summary["last_message"],
            last_timestamp=summary["last_timestamp"],
            message_count=summary["message_count"],
        )
        for summary in summaries
    ]

    return ConversationListResponse(
        conversations=conversations,
        count=total,
    )


@chat_api_router.get(
    "/v1/chat/conversations/{conversation_id}/share",
)
async def get_conversation_share_status(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> ConversationShareStatusResponse:
    """Return share status to the authenticated conversation owner."""
    await _ensure_user_owns_persisted_conversation(
        request, current_user, conversation_id
    )
    share = await db_context.conversation_shares.get_by_conversation(conversation_id)
    return ConversationShareStatusResponse(active=share is not None)


@chat_api_router.post(
    "/v1/chat/conversations/{conversation_id}/share",
)
async def create_conversation_share(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> ConversationShareResponse:
    """Rotate and return a read-only share link for the conversation owner."""
    owner_user_id = await _ensure_user_owns_persisted_conversation(
        request, current_user, conversation_id
    )
    token = secrets.token_urlsafe(32)
    await db_context.conversation_shares.rotate(
        conversation_id, owner_user_id, _share_token_hash(token)
    )
    return ConversationShareResponse(share_url=f"/shared/conversations/{token}")


@chat_api_router.delete(
    "/v1/chat/conversations/{conversation_id}/share",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def revoke_conversation_share(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> Response:
    """Revoke the active read-only share as the conversation owner."""
    await _ensure_user_owns_persisted_conversation(
        request, current_user, conversation_id
    )
    await db_context.conversation_shares.revoke(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _serialize_conversation_messages(
    messages: list[MessageHistoryRow],
) -> list[ConversationMessage]:
    """Convert visible history rows to the public conversation message shape."""
    response_messages: list[ConversationMessage] = []
    for msg in messages:
        if not all(key in msg for key in ["internal_id", "role", "timestamp"]):
            continue

        tool_calls_dicts = None
        msg_tool_calls = msg.get("tool_calls")
        if msg_tool_calls:
            tool_calls_dicts = []
            for tool_call in msg_tool_calls:
                if isinstance(tool_call, ToolCallItem):
                    arguments = tool_call.function.arguments
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments)
                    tool_calls_dicts.append({
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": arguments,
                        },
                    })
                elif isinstance(tool_call, dict):
                    tool_calls_dicts.append(tool_call)

        response_messages.append(
            ConversationMessage(
                internal_id=msg["internal_id"],
                turn_id=msg.get("turn_id"),
                role=msg["role"],
                content=msg.get("content"),
                timestamp=msg["timestamp"],
                tool_calls=tool_calls_dicts,
                tool_call_id=msg.get("tool_call_id"),
                error_traceback=msg.get("error_traceback"),
                attachments=msg.get("attachments"),
                processing_profile_id=msg.get("processing_profile_id"),
                reasoning_info=msg.get("reasoning_info"),
                metadata=None,
            )
        )
    return response_messages


@chat_api_router.get("/v1/chat/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
    before: str | None = None,  # ISO timestamp string
    after: str | None = None,  # ISO timestamp string
    limit: int = 50,
    include_conversation_profile: bool = False,
) -> ConversationMessagesResponse:
    """
    Get messages for a specific conversation with timestamp-based pagination.

    Args:
        conversation_id: The conversation identifier
        before: Get messages before this timestamp (ISO format)
        after: Get messages after this timestamp (ISO format)
        limit: Maximum number of messages to return (default: 50, use 0 for all)
        include_conversation_profile: When true, also resolve
            ``latest_user_profile_id`` (the most recent user message's profile
            across the whole conversation, independent of the page limit) so the
            client can adopt it on open. Skipped by default to keep the frequent
            limit=1 active-turn poll cheap.

    Returns:
        Paginated list of messages in the conversation, plus ``active_turns``
        describing any in-flight turns the client can resume.
    """
    # /messages is a point-in-time read: an empty conversation just returns an
    # empty list, so a brand-new conversation id is allowed.
    user_id = await _ensure_user_owns_conversation(
        request, current_user, conversation_id, allow_new=True
    )
    # Parse timestamp parameters
    before_dt = None
    after_dt = None

    try:
        if before:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        if after:
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid timestamp format. Use ISO format (e.g., 2024-01-15T10:30:00Z): {e}",
        ) from e

    # Handle backward compatibility: limit=0 means no limit (get all)
    actual_limit = None if limit == 0 else limit

    # Use new paginated method
    if actual_limit is None:
        # Legacy behavior: get all messages
        history_by_chat = await db_context.message_history.get_all_grouped(
            interface_type=None,
            conversation_id=conversation_id,
            include_subconversations=False,
        )

        # Collect messages from all interfaces for this conversation ID
        messages = []
        for (_interface_type, conv_id), conv_messages in history_by_chat.items():
            if conv_id == conversation_id:
                messages.extend(conv_messages)

        # Sort messages by timestamp to maintain chronological order
        messages.sort(
            key=lambda msg: msg.get("timestamp", datetime.min.replace(tzinfo=UTC))
        )

        has_more_before = False
        has_more_after = False
    else:
        # Use paginated method
        (
            messages,
            has_more_before,
            has_more_after,
        ) = await db_context.message_history.get_conversation_messages_paginated(
            conversation_id=conversation_id,
            before=before_dt,
            after=after_dt,
            limit=actual_limit,
            include_subconversations=False,
        )

    # Persisted attachment metadata is incomplete by design (see the helper), so
    # resolve it before serializing: clients decide from the mime type whether to
    # render an attachment inline as an image.
    await _enrich_persisted_attachments(
        messages,
        db_context=db_context,
        attachment_registry=attachment_registry,
        acting_user_id=user_id,
    )

    response_messages = _serialize_conversation_messages(messages)

    # Get total message count for the conversation
    total_message_count = (
        await db_context.message_history.get_conversation_message_count(
            conversation_id,
            include_subconversations=False,
        )
    )

    hub = _get_hub(request)
    active_turns = _owned_turns(hub, conversation_id, user_id)

    latest_user_profile_id = None
    if include_conversation_profile:
        latest_user_profile_id = (
            await db_context.message_history.get_latest_user_profile_id(
                conversation_id, include_subconversations=False
            )
        )

    return ConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=response_messages,
        count=len(response_messages),
        total_messages=total_message_count,
        has_more_before=has_more_before,
        has_more_after=has_more_after,
        latest_user_profile_id=latest_user_profile_id,
        active_turns=active_turns,
    )


@chat_api_router.get(
    "/v1/shared-conversations/{token}/messages",
)
async def get_shared_conversation_messages(
    token: str,
    request: Request,
    _current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
) -> ConversationMessagesResponse:
    """Return a read-only transcript to an authenticated share-link holder."""
    share = await _get_active_conversation_share(request, db_context, token)
    history_by_chat = await db_context.message_history.get_all_grouped(
        interface_type=None,
        conversation_id=share.conversation_id,
        include_subconversations=False,
    )
    messages = [
        message
        for (
            _interface_type,
            conversation_id,
        ), conversation_messages in history_by_chat.items()
        if conversation_id == share.conversation_id
        for message in conversation_messages
    ]
    messages.sort(
        key=lambda message: message.get("timestamp", datetime.min.replace(tzinfo=UTC))
    )
    await _enrich_persisted_attachments(
        messages,
        db_context=db_context,
        attachment_registry=attachment_registry,
        acting_user_id=share.owner_user_id,
    )
    for message in messages:
        for attachment in message.get("attachments") or []:
            attachment_id = attachment.get("attachment_id")
            if attachment_id:
                shared_url = (
                    f"/api/v1/shared-conversations/{token}/attachments/{attachment_id}"
                )
                attachment["content_url"] = shared_url
                attachment["url"] = shared_url

    response_messages = _serialize_conversation_messages(messages)
    return ConversationMessagesResponse(
        conversation_id=share.conversation_id,
        messages=response_messages,
        count=len(response_messages),
        total_messages=len(response_messages),
        has_more_before=False,
        has_more_after=False,
        latest_user_profile_id=None,
        active_turns=[],
    )


@chat_api_router.get(
    "/v1/shared-conversations/{token}/attachments/{attachment_id}",
    response_class=FileResponse,
)
async def serve_shared_conversation_attachment(
    token: str,
    attachment_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
) -> FileResponse:
    """Serve one attachment scoped to an active authenticated share link."""
    try:
        uuid.UUID(attachment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc

    share = await _get_active_conversation_share(request, db_context, token)
    attachment = await attachment_registry.get_attachment(
        db_context,
        attachment_id,
        acting_user_id=share.owner_user_id,
    )
    if attachment is None or attachment.conversation_id != share.conversation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    file_path = attachment_registry.get_attachment_path(
        attachment_id,
        stored_path=attachment.storage_path,
        source_type=attachment.source_type,
    )
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    background_tasks.add_task(
        attachment_registry.update_access_time_background,
        attachment_id,
        acting_user_id=share.owner_user_id,
    )
    original_filename = attachment.metadata.get("original_filename")
    filename = (
        original_filename
        if isinstance(original_filename, str) and original_filename
        else file_path.name
    )
    return FileResponse(
        path=str(file_path),
        media_type=attachment_registry.get_content_type(file_path),
        filename=filename,
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"{attachment_id}"',
        },
    )


@chat_api_router.get("/v1/debug/test_stream")
async def debug_test_stream() -> StreamingResponse:
    """Simple test endpoint to verify SSE streaming works."""

    async def simple_event_generator() -> AsyncGenerator[str]:
        logger.info("Starting simple stream test")
        for i in range(5):
            logger.info(f"Yielding test event {i}")
            yield f"event: test\ndata: {json.dumps({'message': f'Test event {i}'})}\n\n"
            await asyncio.sleep(0.1)
        logger.info("Yielding end event")
        yield f"event: end\ndata: {json.dumps({'done': True})}\n\n"
        logger.info("Simple stream test completed")

    return StreamingResponse(
        simple_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@chat_api_router.post("/v1/chat/voice-sessions")
async def api_chat_save_voice_session(
    payload: VoiceSessionRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db_context: Annotated[Database, Depends(get_db)],
) -> VoiceSessionResponse:
    """Persist a completed native-voice conversation as its own chat conversation.

    The native voice screen talks directly to Gemini Live; nothing is written to
    chat history during the call. On a clean end, the client posts the accumulated
    input/output transcripts here. We write them as ``web`` messages under a fresh
    conversation id (with the caller's ``user_id`` so the ownership predicate that
    gates the conversation list and reads recognizes them), so the session shows up
    in the conversation list and can be continued in text.
    """
    raw_user_id = current_user.get("user_identifier")
    if not isinstance(raw_user_id, str) or not raw_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    if not payload.turns:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A voice session must contain at least one turn.",
        )

    if payload.conversation_id:
        # A client-supplied id must already belong to the caller (or be unused).
        # Otherwise appending the caller's user messages would make a foreign
        # conversation multi-owner and break the sole-owner predicate that gates
        # the real owner's list/reads. Raises 404 on mismatch, like other paths.
        await _ensure_user_owns_conversation(
            request, current_user, payload.conversation_id, allow_new=True
        )
        conversation_id = payload.conversation_id
    else:
        conversation_id = f"web_conv_{uuid.uuid4().hex}"
    base_time = datetime.now(UTC)

    # One turn id per logical exchange: a user line opens a new turn that the
    # assistant lines following it belong to, mirroring how text chat groups a
    # prompt with its reply. A single id for the whole session would collapse the
    # transcript into one turn in turn-grouped history views.
    turn_id = str(uuid.uuid4())
    saved = 0
    for index, turn in enumerate(payload.turns):
        if turn.role == "user":
            turn_id = str(uuid.uuid4())
            message: UserMessage | AssistantMessage = UserMessage(content=turn.text)
        else:
            # Native voice replies may summarize tool output, but the transcript
            # payload does not carry the session's runtime tracker. Preserve the
            # pre-metadata conservative behavior rather than labeling model- and
            # tool-derived text as trusted.
            message = AssistantMessage(
                content=turn.text,
                taint_metadata=TurnTaintState
                .empty()
                .add_source(
                    TaintSource(
                        source_type=TaintSourceType.TOOL_OUTPUT,
                        source_id=None,
                        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                        labels=frozenset(),
                        reason=(
                            "Native voice assistant transcript may derive from "
                            "tool output without persisted session provenance."
                        ),
                    )
                )
                .to_metadata(),
            )
        # Strictly increasing timestamps keep the transcript ordered when the
        # conversation is read back (history is ordered by timestamp).
        timestamp = base_time + timedelta(milliseconds=index)
        await db_context.message_history.add_message(
            message,
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=timestamp,
            turn_id=turn_id,
            user_id=raw_user_id,
        )
        saved += 1

    return VoiceSessionResponse(conversation_id=conversation_id, message_count=saved)


@chat_api_router.post("/v1/chat/confirm_tool")
async def confirm_tool_execution(
    payload: ToolConfirmationRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> ToolConfirmationResponse:
    """
    Handle confirmation response for a tool execution request.

    This endpoint is called by the frontend when the user approves or rejects
    a tool that requires confirmation.

    Args:
        payload: Confirmation request containing request_id and approval status

    Returns:
        Response indicating whether the confirmation was processed successfully
    """
    confirmation_service = _get_confirmation_service(request)
    confirmation_result_waiters = _get_confirmation_result_waiters(request)
    try:
        if payload.approved:
            if confirmation_result_waiters.is_decision_only(payload.request_id):
                await confirmation_service.approve_without_enqueueing_execution(
                    request_id=payload.request_id,
                    approving_user_id=current_user["user_identifier"],
                    approving_interface=payload.approving_interface,
                )
            else:
                await confirmation_service.approve_and_enqueue_execution(
                    request_id=payload.request_id,
                    approving_user_id=current_user["user_identifier"],
                    approving_interface=payload.approving_interface,
                )
            web_confirmation_manager.resolve_approved(payload.request_id)
            message = "Tool execution approved"
        else:
            await confirmation_service.reject(
                request_id=payload.request_id,
                rejecting_user_id=current_user["user_identifier"],
                rejecting_interface=payload.approving_interface,
            )
            web_confirmation_manager.resolve_rejected(payload.request_id)
            confirmation_result_waiters.resolve_rejected(payload.request_id)
            message = "Tool execution rejected"
        success = True
        logger.info(f"Confirmation {payload.request_id}: {message}")
    except (
        ConfirmationAuthorizationError,
        ConfirmationExpiredError,
        ConfirmationNotFoundError,
        ConfirmationAlreadyResolvedError,
    ) as exc:
        success = False
        message = "Confirmation request not found or already processed"
        logger.warning("Failed to process confirmation %s: %s", payload.request_id, exc)
    except ConfirmationError as exc:
        success = False
        message = "Failed to process confirmation request"
        logger.error("Failed to process confirmation %s: %s", payload.request_id, exc)

    return ToolConfirmationResponse(
        success=success,
        message=message,
    )


@chat_api_router.get("/v1/chat/confirmations/pending")
async def list_pending_tool_confirmations(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> PendingToolConfirmationsResponse:
    """List pending durable tool confirmations for the current user."""
    confirmation_service = _get_confirmation_service(request)
    now = datetime.now(UTC)
    rows = await confirmation_service.list_pending_for_user(
        user_id=current_user["user_identifier"]
    )
    confirmations = [
        PendingToolConfirmation(
            request_id=row["id"],
            tool_name=row["tool_name"],
            tool_call_id=row["tool_call_id"],
            confirmation_prompt=row["confirmation_prompt"],
            args=row["tool_args_json"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            timeout_seconds=(row["expires_at"] - row["created_at"]).total_seconds(),
            time_remaining_seconds=max(0.0, (row["expires_at"] - now).total_seconds()),
        )
        for row in rows
    ]
    return PendingToolConfirmationsResponse(confirmations=confirmations)


@chat_api_router.get("/v1/chat/confirmations/{request_id}")
async def get_tool_confirmation(
    request_id: str,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> ToolConfirmationDetail:
    """Return a single durable tool confirmation owned by the current user.

    Used by clients that open a confirmation directly (for example the iOS modal launched by
    tapping a confirmation push notification) and need the full prompt, arguments, and current
    status. Requests that do not exist or belong to another user return 404 so existence is not
    leaked across users.
    """
    confirmation_service = _get_confirmation_service(request)
    try:
        row = await confirmation_service.get_for_user(
            request_id=request_id,
            user_id=current_user["user_identifier"],
        )
    except (ConfirmationNotFoundError, ConfirmationAuthorizationError) as exc:
        logger.info(
            "Confirmation %s not available to current user: %s", request_id, exc
        )
        raise HTTPException(
            status_code=404, detail="Confirmation request not found"
        ) from exc

    now = datetime.now(UTC)
    # A pending row whose deadline has passed but that the background sweep (mark_expired) has not
    # processed yet must still be reported as expired, so a client does not offer Approve/Reject for
    # a request that confirm_tool would reject. The GET stays read-only; the sweep persists the
    # transition.
    status = row["status"]
    if status == "pending" and row["expires_at"] <= now:
        status = "expired"
    return ToolConfirmationDetail(
        request_id=row["id"],
        tool_name=row["tool_name"],
        tool_call_id=row["tool_call_id"],
        confirmation_prompt=row["confirmation_prompt"],
        args=row["tool_args_json"],
        status=status,
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        time_remaining_seconds=max(0.0, (row["expires_at"] - now).total_seconds()),
    )


@chat_api_router.get("/v1/profiles")
async def get_available_profiles(
    request: Request,
    default_processing_service: Annotated[
        ProcessingService, Depends(get_processing_service)
    ],
) -> ProfilesResponse:
    """
    Get a list of available service profiles for the chat interface.

    Returns information about each profile including ID, description,
    LLM model, and available tools/capabilities.
    """
    # Get processing services registry from app state
    processing_services_registry: dict[str, DelegatableService] = (
        request.app.state.processing_services
        if hasattr(request.app.state, "processing_services")
        else {}
    )

    profiles: list[ServiceProfile] = []

    # Add all profiles from the registry
    for profile_id, service in processing_services_registry.items():
        # Skip remote A2A profiles — they are delegation-only targets
        if service.kind == "remote":
            service_config = service.service_config
            profiles.append(
                ServiceProfile(
                    id=profile_id,
                    description=service_config.description
                    or f"Remote agent: {profile_id}",
                    llm_model=None,
                    available_tools=[],
                    enabled_mcp_servers=[],
                    delegation_only=True,
                )
            )
            continue

        assert isinstance(service, ProcessingService)  # remote profiles handled above
        service_config = service.service_config

        # Extract available tools from tools provider
        available_tools: list[str] = []
        enabled_mcp_servers: list[str] = []

        # Get all available tools for this profile (local + MCP)
        try:
            # This correctly returns only tools allowed by the profile policy.
            defs = await service.tools_provider.get_tool_definitions()
            available_tools = [
                d.get("function", {}).get("name", "unknown") for d in defs
            ]
        except Exception:
            # Log error but continue with other profiles. Catching Exception is necessary
            # here because tool discovery (especially for MCP) involves external processes/network
            # and should not crash the entire profile listing if one provider is flaky.
            logger.exception(
                f"Error fetching tool definitions for profile {profile_id}"
            )

        # Derive enabled MCP servers from the profile's visible tool descriptors.
        descriptor_provider = service.tools_provider
        mcp_servers_derived = False
        if isinstance(descriptor_provider, ToolDescriptorProvider):
            try:
                descriptors = await descriptor_provider.get_tool_descriptors()
                enabled_mcp_servers = sorted({
                    descriptor.mcp_server_id
                    for descriptor in descriptors
                    if descriptor.origin == "mcp"
                    and descriptor.mcp_server_id is not None
                })
                mcp_servers_derived = True
            except Exception:
                logger.exception(
                    "Error fetching tool descriptors for profile %s", profile_id
                )

        if not mcp_servers_derived:
            mcp_provider = find_provider_by_type(
                service.tools_provider, MCPToolsProvider
            )
            if mcp_provider:
                enabled_mcp_servers = list(mcp_provider.server_configs.keys())

        # Get description from service config or generate a fallback
        description = service_config.description
        if not description:
            # Generate a user-friendly description based on profile ID
            if profile_id == "default_assistant":
                description = "General-purpose AI assistant with access to your notes, calendar, and tools"
            elif profile_id == "browser":
                description = "Web browsing assistant with internet search and page interaction capabilities"
            elif profile_id == "research":
                description = "Research specialist using advanced models for deep information gathering"
            elif profile_id == "research_max":
                description = "Research specialist using the Deep Research Max tier for the most comprehensive multi-source investigations"
            elif profile_id == "antigravity":
                description = "Autonomous agent that carries out multi-step tasks in a sandbox with code execution, file access and web search"
            elif profile_id == "event_handler":
                description = (
                    "Automated event handler for script and system integration"
                )
            else:
                description = f"AI assistant profile: {profile_id}"

        profiles.append(
            ServiceProfile(
                id=profile_id,
                description=description,
                llm_model=getattr(service_config, "llm_model", None),
                available_tools=sorted(available_tools),
                enabled_mcp_servers=sorted(enabled_mcp_servers),
            )
        )

    # Sort profiles by ID for consistent ordering
    profiles.sort(key=lambda p: p.id)

    return ProfilesResponse(
        profiles=profiles,
        default_profile_id=default_processing_service.service_config.id,
    )
