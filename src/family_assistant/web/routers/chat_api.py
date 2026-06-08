import asyncio
import base64
import binascii
import contextlib
import json
import logging
import mimetypes
import uuid
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from family_assistant.llm import ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    attachment_content,
    image_url_content,
    text_content,
)
from family_assistant.processing import DelegatableService, ProcessingService
from family_assistant.services.confirmation_service import (
    DURABLE_CONFIRMATION_EXECUTION_WAIT_SECONDS,
    DURABLE_CONFIRMATION_STATUS_POLL_SECONDS,
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationError,
    ConfirmationExpiredError,
    ConfirmationNotFoundError,
    ConfirmationService,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import MCPToolsProvider, find_provider_by_type
from family_assistant.tools.infrastructure import ToolDescriptorProvider
from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext
from family_assistant.utils.text_normalization import StreamingLatexNormalizer
from family_assistant.web.confirmation_manager import web_confirmation_manager
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
    get_processing_service,
    get_web_chat_interface,
)
from family_assistant.web.models import ChatMessageResponse, ChatPromptRequest


class MessageDict(TypedDict):
    """Message dict with database fields for SSE delivery."""

    # Required fields
    internal_id: str
    timestamp: datetime
    role: str
    content: str
    conversation_id: str
    interface_type: str
    # Optional fields
    interface_message_id: str | None
    user_id: str | None
    turn_id: str | None
    tool_calls: Any
    tool_call_id: str | None
    # ast-grep-ignore: no-dict-any - Arbitrary JSON metadata from database, structure varies by message type and cannot be statically typed
    metadata: dict[str, Any] | None
    thread_root_id: str | None
    replied_to_id: str | None


if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import (
        AttachmentMetadata,
        AttachmentRegistry,
    )
    from family_assistant.web.web_chat_interface import WebChatInterface


logger = logging.getLogger(__name__)
chat_api_router = APIRouter()


def _get_background_chat_tasks(app: FastAPI) -> set[asyncio.Task[None]]:
    """Return the set tracking detached chat-processing tasks for this app.

    When a streaming client disconnects mid-turn the processing keeps running in
    the background. The resulting task is parked here so it is not garbage
    collected before it finishes and delivers the reply via push notification.
    """
    tasks = getattr(app.state, "background_chat_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.background_chat_tasks = tasks
    return tasks


def _log_detached_stream_result(task: asyncio.Task[None]) -> None:
    """Log unexpected failures from a detached background chat task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Background chat processing task failed after client disconnect: %s",
            exc,
            exc_info=exc,
        )


async def _notify_disconnected_reply(
    db_context: DatabaseContext,
    web_chat_interface: "WebChatInterface",
    *,
    interface_type: str,
    conversation_id: str,
    reply_text: str,
) -> None:
    """Deliver a completed assistant reply via push when the SSE client is gone.

    The streaming path relays the reply over SSE and persists it directly, so it
    never calls ``WebChatInterface.send_message`` (where push delivery lives). A
    client that closed the app would otherwise never learn the turn finished.
    This sends the same message-category push used for other background replies.
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
    except Exception as e:
        logger.warning(
            f"Failed to send disconnect push notification: {e}", exc_info=True
        )


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
    service = ConfirmationService(
        db_context_factory=lambda: get_db_context(request.app.state.database_engine)
    )
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


async def _process_user_attachments(
    payload: ChatPromptRequest,
    conversation_id: str,
    attachment_registry: "AttachmentRegistry",
    db_context: DatabaseContext,
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
                            required_source_id=user_id,
                        )

                        # If not claimed (already linked), get existing attachment record
                        if not attachment_record:
                            attachment_record = (
                                await attachment_registry.get_attachment(
                                    db_context=db_context,
                                    attachment_id=attachment_id,
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
                        detail=f"Invalid base64 attachment content: {str(e)}",
                    ) from e
                except HTTPException:
                    raise
                except Exception as e:
                    logger.error(
                        f"Error processing user attachment: {e}", exc_info=True
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Failed to process attachment",
                    ) from e

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
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    web_chat_interface: Annotated["WebChatInterface", Depends(get_web_chat_interface)],
) -> ChatMessageResponse:
    """
    Receives a user prompt via API, processes it using the specified or default
    ProcessingService, and returns the assistant's reply.
    """
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    # turn_id is generated internally by handle_chat_interaction.
    # We will use a placeholder for the response model if needed, or remove it from response.

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

    # Process user attachments if present
    trigger_content_parts: list[ContentPartDict] = [
        {"type": "text", "text": payload.prompt}  # type: ignore[typeddict-item]  # Runtime dict matches TypedDict structure
    ]
    trigger_attachments: list[MessageAttachmentMetadata] | None = None

    if payload.attachments:
        # Only get attachment registry when we actually have attachments
        attachment_registry = await get_attachment_registry(request)
        trigger_content_parts, trigger_attachments = await _process_user_attachments(
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

    # The `turn_id` will be generated by `handle_chat_interaction`
    # We can retrieve it from the response if needed by the client,
    # but the ChatMessageResponse model currently expects it.
    # Let's assume for now the client might want the turn_id.
    # The `handle_chat_interaction` doesn't return turn_id directly,
    # but it's logged and associated with messages.
    # For the API response, we might need to reconsider if turn_id is essential.
    # The current ChatMessageResponse model includes it.
    # Let's generate it here for the response, though the one used internally will be from handle_chat_interaction.
    # This is a slight divergence; ideally, the one from handle_chat_interaction would be returned.
    # For now, to match the existing response model:
    response_turn_id = (
        str(uuid.uuid4())  # This is for the *response model only*
    )

    # Get chat_interfaces registry from app state for cross-interface messaging
    chat_interfaces = getattr(request.app.state, "chat_interfaces", None)
    confirmation_ui_managers = getattr(
        request.app.state,
        "confirmation_ui_managers",
        None,
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
        request_confirmation_callback=None,  # No confirmation callback for API (yet)
        trigger_attachments=trigger_attachments,  # Pass attachment metadata
    )

    final_reply_content = result.text_reply
    final_assistant_message_internal_id = result.assistant_message_internal_id
    _final_reasoning_info = result.reasoning_info  # Not used by API response
    error_traceback = result.error_traceback
    _response_attachment_ids = result.attachment_ids  # Not yet included in API response

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

    return ChatMessageResponse(
        reply=final_reply_content,  # Back to original field name
        conversation_id=conversation_id,  # Return the used/generated conversation_id
        turn_id=response_turn_id,  # Return the turn_id generated for the response model
        attachments=trigger_attachments,  # Include processed attachments in response
        tool_calls=tool_calls_response,  # Include tool calls if any
    )


@chat_api_router.get("/v1/chat/conversations")
async def get_conversations(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    limit: int = 20,
    offset: int = 0,
    interface_type: str | None = None,
    conversation_id: str | None = None,
    date_from: str | None = None,  # Expected as YYYY-MM-DD string
    date_to: str | None = None,  # Expected as YYYY-MM-DD string
) -> ConversationListResponse:
    """
    Get a list of chat conversations for the web interface.

    Args:
        limit: Maximum number of conversations to return
        offset: Number of conversations to skip for pagination
        interface_type: Filter by interface type (web, telegram, api, email)
        conversation_id: Filter by specific conversation ID
        date_from: Filter conversations with messages after this date (YYYY-MM-DD)
        date_to: Filter conversations with messages before this date (YYYY-MM-DD)

    Returns:
        List of conversation summaries with metadata
    """
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

    # Use optimized query for conversation summaries with all filters
    summaries, total = await db_context.message_history.get_conversation_summaries(
        interface_type=interface_type,
        limit=limit,
        offset=offset,
        conversation_id=conversation_id,
        date_from=date_from_dt,
        date_to=date_to_dt,
    )

    # Convert to response format
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


@chat_api_router.get("/v1/chat/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    attachment_registry: Annotated[
        "AttachmentRegistry", Depends(get_attachment_registry)
    ],
    before: str | None = None,  # ISO timestamp string
    after: str | None = None,  # ISO timestamp string
    limit: int = 50,
) -> ConversationMessagesResponse:
    """
    Get messages for a specific conversation with timestamp-based pagination.

    Args:
        conversation_id: The conversation identifier
        before: Get messages before this timestamp (ISO format)
        after: Get messages after this timestamp (ISO format)
        limit: Maximum number of messages to return (default: 50, use 0 for all)

    Returns:
        Paginated list of messages in the conversation
    """
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
            interface_type=None, conversation_id=conversation_id
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
        )

    # Convert to response format
    response_messages = []
    for msg in messages:
        # Skip messages with missing required fields
        if not all(key in msg for key in ["internal_id", "role", "timestamp"]):
            continue

        # Convert tool_calls from ToolCallItem objects to dicts for Pydantic
        tool_calls_dicts = None
        msg_tool_calls = msg.get("tool_calls")
        if msg_tool_calls:
            tool_calls_dicts = []
            for tc in msg_tool_calls:
                if isinstance(tc, ToolCallItem):
                    # Convert ToolCallItem to dict
                    # Ensure arguments is always a JSON string
                    args = tc.function.arguments
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    tc_dict = {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": args,
                        },
                    }
                    # Note: provider_metadata is not included in API response
                    tool_calls_dicts.append(tc_dict)
                elif isinstance(tc, dict):
                    # Already a dict, use as-is
                    tool_calls_dicts.append(tc)

        response_messages.append(
            ConversationMessage(
                internal_id=msg["internal_id"],
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

    # Get total message count for the conversation
    total_message_count = (
        await db_context.message_history.get_conversation_message_count(conversation_id)
    )

    return ConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=response_messages,
        count=len(response_messages),
        total_messages=total_message_count,
        has_more_before=has_more_before,
        has_more_after=has_more_after,
    )


@chat_api_router.post("/v1/chat/send_message_stream")
async def api_chat_send_message_stream(
    payload: ChatPromptRequest,
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    default_processing_service: Annotated[
        ProcessingService, Depends(get_processing_service)
    ],
    web_chat_interface: Annotated["WebChatInterface", Depends(get_web_chat_interface)],
) -> StreamingResponse:
    """
    Stream chat responses using Server-Sent Events format.

    This endpoint accepts the same payload as the non-streaming endpoint but
    returns a stream of events as the response is generated, including:
    - Text chunks as they're generated
    - Tool calls as they're initiated
    - Tool results as they complete
    - Error events if something goes wrong
    """
    conversation_id = payload.conversation_id or str(uuid.uuid4())

    # Determine which processing service to use (same logic as non-streaming endpoint)
    selected_processing_service = default_processing_service
    profile_id_requested = payload.profile_id

    if profile_id_requested:
        logger.info(
            f"API streaming chat request for profile_id: '{profile_id_requested}'. "
            f"Conversation ID: {conversation_id}, Prompt: '{payload.prompt[:100]}...'"
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
                f"Profile_id '{profile_id_requested}' not found in registry. "
                f"Falling back to default profile: '{default_processing_service.service_config.id}'."
            )
    else:
        logger.info(
            f"API streaming chat request (no profile_id specified). "
            f"Using default profile: '{default_processing_service.service_config.id}'. "
            f"Conversation ID: {conversation_id}, Prompt: '{payload.prompt[:100]}...'"
        )

    # Process user attachments if present
    trigger_content_parts: list[ContentPartDict] = [
        {"type": "text", "text": payload.prompt}  # type: ignore[typeddict-item]  # Runtime dict matches TypedDict structure
    ]
    attachment_metadata: list[MessageAttachmentMetadata] | None = None

    if payload.attachments:
        # Only get attachment registry when we actually have attachments
        attachment_registry = await get_attachment_registry(request)
        async with get_db_context(request.app.state.database_engine) as db_context:
            (
                trigger_content_parts,
                attachment_metadata,
            ) = await _process_user_attachments(
                payload,
                conversation_id,
                attachment_registry,
                db_context,
                current_user["user_identifier"],
            )

    interface_type = payload.interface_type or "api"
    user_name_for_api = _user_name_for_chat(current_user)

    async def event_generator() -> AsyncGenerator[str]:
        """Generate SSE formatted events from the processing stream."""

        # ast-grep-ignore: no-dict-any - SSE event queue carries heterogeneous event types (stream, confirmation, error)
        confirmation_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Set when the SSE client goes away mid-turn (e.g. the app is closed or
        # backgrounded). When set, processing continues in the background and the
        # final reply is delivered via push notification instead of the now-dead
        # SSE stream.
        client_disconnected = asyncio.Event()

        # Create confirmation callback that queues events
        async def web_confirmation_callback(
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
            """Request confirmation from the user via SSE."""
            # For the web UI, we don't use text renderers like Telegram does.
            # Instead, we pass the tool information directly to the frontend
            # which uses the existing ToolWithConfirmation components to render
            # the tool call visually with proper formatting and details.
            # This provides a better user experience than text-based confirmations.

            # Default confirmation prompt (frontend will render tool details)
            confirmation_prompt = (
                f"Do you want to execute '{tool_name}' with these parameters?"
            )

            source_message_internal_id = None
            if turn_id is not None:
                source_row = (
                    await context.db_context.message_history.get_user_row_by_turn_id(
                        turn_id
                    )
                )
                if source_row is not None:
                    source_message_internal_id = source_row["internal_id"]

            confirmation_service = _get_confirmation_service(request)
            confirmation_result_waiters = _get_confirmation_result_waiters(request)
            expires_at = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
            durable_request = await confirmation_service.create_request(
                target_user_id=current_user["user_identifier"],
                tool_name=tool_name,
                tool_args=tool_args,
                tool_call_id=call_id,
                source_message_internal_id=source_message_internal_id,
                confirmation_prompt=confirmation_prompt,
                expires_at=expires_at,
            )
            request_id = durable_request["id"]
            execution_future = confirmation_result_waiters.register(request_id)

            async def get_durable_request_status() -> str | None:
                try:
                    refreshed_request = await confirmation_service.get_for_user(
                        request_id=request_id,
                        user_id=current_user["user_identifier"],
                    )
                except ConfirmationNotFoundError:
                    logger.warning(
                        "Durable confirmation %s disappeared while waiting",
                        request_id,
                    )
                    return "missing"
                except ConfirmationAuthorizationError:
                    logger.warning(
                        "User lost access to durable confirmation %s while waiting",
                        request_id,
                    )
                    return "unauthorized"
                except ConfirmationError as exc:
                    logger.warning(
                        "Could not read durable confirmation %s while waiting: %s",
                        request_id,
                        exc,
                    )
                    return "error"
                return refreshed_request["status"]

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

                # Queue confirmation request event for client
                await confirmation_queue.put({
                    "type": "confirmation_request",
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_call_id": call_id,
                    "confirmation_prompt": confirmation_prompt,
                    "timeout_seconds": timeout_seconds,
                    "args": tool_args,
                })

                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break

                    done, _ = await asyncio.wait(
                        {decision_future, execution_future},
                        timeout=min(
                            DURABLE_CONFIRMATION_STATUS_POLL_SECONDS,
                            remaining,
                        ),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if decision_future in done:
                        decision_outcome = decision_future.result()
                        if decision_outcome.kind == "timed_out":
                            await confirmation_service.mark_expired(
                                now=datetime.now(UTC)
                            )

                        # Queue confirmation result event
                        await confirmation_queue.put({
                            "type": "confirmation_result",
                            "request_id": request_id,
                            "approved": decision_outcome.kind == "approved",
                        })

                        if decision_outcome.kind != "approved":
                            return decision_outcome

                        web_confirmation_manager.remove_confirmation(request_id)
                        return await wait_for_execution_result()
                    if execution_future in done:
                        execution_outcome = execution_future.result()
                        await confirmation_queue.put({
                            "type": "confirmation_result",
                            "request_id": request_id,
                            "approved": execution_outcome.kind
                            in {"completed", "failed"},
                        })
                        return execution_outcome

                    durable_status = await get_durable_request_status()
                    if durable_status == "approved":
                        await confirmation_queue.put({
                            "type": "confirmation_result",
                            "request_id": request_id,
                            "approved": True,
                        })
                        web_confirmation_manager.remove_confirmation(request_id)
                        return await wait_for_execution_result()
                    if durable_status == "rejected":
                        outcome = ConfirmationOutcome(kind="rejected")
                        await confirmation_queue.put({
                            "type": "confirmation_result",
                            "request_id": request_id,
                            "approved": False,
                        })
                        return outcome
                    if durable_status in {
                        "expired",
                        "missing",
                        "unauthorized",
                        "error",
                    }:
                        outcome = ConfirmationOutcome(
                            kind="failed",
                            result="Confirmation request could not be resolved.",
                        )
                        await confirmation_queue.put({
                            "type": "confirmation_result",
                            "request_id": request_id,
                            "approved": False,
                        })
                        return outcome

                final_status = await get_durable_request_status()
                if final_status == "approved":
                    await confirmation_queue.put({
                        "type": "confirmation_result",
                        "request_id": request_id,
                        "approved": True,
                    })
                    web_confirmation_manager.remove_confirmation(request_id)
                    return await wait_for_execution_result()
                if final_status == "rejected":
                    outcome = ConfirmationOutcome(kind="rejected")
                    await confirmation_queue.put({
                        "type": "confirmation_result",
                        "request_id": request_id,
                        "approved": False,
                    })
                    return outcome
                if final_status in {"missing", "unauthorized", "error"}:
                    outcome = ConfirmationOutcome(
                        kind="failed",
                        result="Confirmation request could not be resolved.",
                    )
                    await confirmation_queue.put({
                        "type": "confirmation_result",
                        "request_id": request_id,
                        "approved": False,
                    })
                    return outcome
                await confirmation_service.mark_expired(now=datetime.now(UTC))
                outcome = ConfirmationOutcome(kind="timed_out")
                await confirmation_queue.put({
                    "type": "confirmation_result",
                    "request_id": request_id,
                    "approved": False,
                })
                return outcome
            finally:
                web_confirmation_manager.remove_confirmation(request_id)
                confirmation_result_waiters.unregister(request_id, execution_future)

        # Create task to process the interaction stream
        # Get chat_interfaces registry from app state for cross-interface messaging
        chat_interfaces = getattr(request.app.state, "chat_interfaces", None)
        confirmation_ui_managers = getattr(
            request.app.state,
            "confirmation_ui_managers",
            None,
        )

        async def process_stream() -> None:
            # Accumulate the final assistant reply text so it can be delivered via
            # push notification if the client disconnects before the turn finishes.
            # Text from earlier agentic turns (preamble before a tool call) is
            # discarded so the notification reflects the final answer.
            final_reply_parts: list[str] = []
            try:
                async with get_db_context(
                    request.app.state.database_engine
                ) as stream_db_context:
                    if hasattr(request.app.state, "message_notifier"):
                        stream_db_context.message_notifier = (
                            request.app.state.message_notifier
                        )
                    async for (
                        event
                    ) in selected_processing_service.handle_chat_interaction_stream(
                        db_context=stream_db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        trigger_content_parts=trigger_content_parts,
                        trigger_interface_message_id=None,
                        user_name=user_name_for_api,
                        user_id=current_user["user_identifier"],
                        replied_to_interface_id=None,
                        chat_interface=web_chat_interface,
                        chat_interfaces=chat_interfaces,
                        confirmation_ui_managers=confirmation_ui_managers,
                        request_confirmation_callback=web_confirmation_callback,
                        trigger_attachments=attachment_metadata,
                    ):
                        if event.type == "content" and event.content:
                            final_reply_parts.append(event.content)
                        elif event.type == "tool_call":
                            # A new tool round means more turns follow; drop any
                            # preamble so only the final answer is notified.
                            final_reply_parts.clear()
                        elif event.type == "error":
                            logger.error(f"Stream event error: {event.error}")
                        # Add events to queue
                        await confirmation_queue.put({
                            "type": "stream_event",
                            "event": event,
                        })

                    # Signal end of stream
                    await confirmation_queue.put({"type": "stream_end"})

                    # If the client disconnected mid-turn, the SSE stream is gone,
                    # so deliver the completed reply as a push notification.
                    if client_disconnected.is_set():
                        await _notify_disconnected_reply(
                            stream_db_context,
                            web_chat_interface,
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            reply_text="".join(final_reply_parts).strip(),
                        )
            except Exception as e:
                # Queue error event
                logger.error(f"Error in process_stream: {e}", exc_info=True)
                await confirmation_queue.put({"type": "error", "error": str(e)})

        # Emit attachment events for user-uploaded attachments first
        if attachment_metadata:
            for attachment in attachment_metadata:
                attachment_event_data = {
                    "type": "attachment",
                    "attachment_id": attachment.get("attachment_id"),
                    "url": attachment.get("content_url"),
                    "content_url": attachment.get("content_url"),
                    "mime_type": attachment.get("mime_type"),
                    "description": attachment.get("description"),
                    "size": attachment.get("size"),
                }
                yield f"event: attachment\ndata: {json.dumps(attachment_event_data)}\n\n"

        # Start the stream processing task
        stream_task = asyncio.create_task(process_stream())

        last_reasoning_info: MessageReasoningInfo | None = None
        send_close_event = True
        latex_normalizer = StreamingLatexNormalizer()

        try:
            # Process events from queue and yield SSE events
            while True:
                try:
                    # Get next event from queue with timeout
                    queue_event = await asyncio.wait_for(
                        confirmation_queue.get(), timeout=0.1
                    )
                except TimeoutError:
                    # Check if stream task is done
                    if stream_task.done():
                        # Check if there are still events in the queue before breaking
                        if confirmation_queue.empty():
                            break
                        else:
                            continue
                    continue

                if queue_event["type"] == "confirmation_request":
                    # Send confirmation request event
                    event_data = {
                        "request_id": queue_event["request_id"],
                        "tool_name": queue_event["tool_name"],
                        "tool_call_id": queue_event["tool_call_id"],
                        "confirmation_prompt": queue_event["confirmation_prompt"],
                        "timeout_seconds": queue_event["timeout_seconds"],
                        "args": queue_event["args"],
                    }
                    yield f"event: tool_confirmation_request\ndata: {json.dumps(event_data)}\n\n"

                elif queue_event["type"] == "confirmation_result":
                    # Send confirmation result event
                    event_data = {
                        "request_id": queue_event["request_id"],
                        "approved": queue_event["approved"],
                    }
                    yield f"event: tool_confirmation_result\ndata: {json.dumps(event_data)}\n\n"

                elif queue_event["type"] == "stream_event":
                    event = queue_event["event"]
                    # Process normal stream events
                    if event.type == "content":
                        # The streaming normalizer buffers the trailing bytes
                        # of each chunk while they might still extend a LaTeX
                        # command, math span, or code span, then flushes the
                        # remainder at end of stream. This keeps live tokens
                        # consistent with the persisted (normalized) message.
                        content = latex_normalizer.feed(event.content or "")
                        if content:
                            yield f"event: text\ndata: {json.dumps({'content': content})}\n\n"

                    elif event.type == "tool_call":
                        # Convert tool_call to dict for JSON serialization
                        if event.tool_call:
                            # Ensure arguments is a JSON string if it isn't already
                            args = event.tool_call.function.arguments
                            if not isinstance(args, str):
                                args = json.dumps(args)

                            tool_call_dict = {
                                "id": event.tool_call.id,
                                "type": event.tool_call.type,  # Include type
                                "function": {
                                    "name": event.tool_call.function.name,
                                    "arguments": args,
                                },
                            }
                            yield f"event: tool_call\ndata: {json.dumps({'tool_call': tool_call_dict})}\n\n"

                    elif event.type == "tool_result":
                        # Include tool_call_id for correlation and attachment metadata if present
                        tool_result_data = {
                            "tool_call_id": event.tool_call_id,
                            "result": event.tool_result,
                        }
                        # Add attachment metadata if present
                        if event.metadata and "attachments" in event.metadata:
                            tool_result_data["attachments"] = event.metadata[
                                "attachments"
                            ]
                        yield f"event: tool_result\ndata: {json.dumps(tool_result_data)}\n\n"

                    elif event.type == "done":
                        # Flush any buffered LaTeX text from this turn so
                        # trailing ambiguous bytes (e.g. ``$``) aren't
                        # merged with the next turn's opening tokens.
                        trailing = latex_normalizer.flush()
                        if trailing:
                            yield f"event: text\ndata: {json.dumps({'content': trailing})}\n\n"
                        # Handle attachment IDs from attach_to_response tool calls
                        if event.metadata and "attachment_ids" in event.metadata:
                            # Get attachment registry to fetch attachment metadata
                            attachment_registry = await get_attachment_registry(request)

                            async with get_db_context(
                                request.app.state.database_engine
                            ) as att_db:
                                for attachment_id in event.metadata["attachment_ids"]:
                                    try:
                                        attachment_info = (
                                            await attachment_registry.get_attachment(
                                                att_db, attachment_id
                                            )
                                        )
                                        if attachment_info:
                                            attachment_event_data = {
                                                "type": "attachment",
                                                "attachment_id": attachment_id,
                                                "url": attachment_info.content_url,
                                                "content_url": attachment_info.content_url,
                                                "mime_type": attachment_info.mime_type,
                                                "description": attachment_info.description,
                                                "size": attachment_info.size,
                                            }
                                            yield f"event: attachment\ndata: {json.dumps(attachment_event_data)}\n\n"
                                        else:
                                            logger.warning(
                                                f"Attachment {attachment_id} not found in registry"
                                            )
                                    except Exception as e:
                                        logger.error(
                                            f"Error emitting attachment event for {attachment_id}: {e}"
                                        )

                        # Store reasoning_info for the final end event.
                        # Don't send event: end here — multiple done events are
                        # emitted (one per agentic turn) and sending end mid-stream
                        # can cause proxies/clients to close the connection early.
                        if event.metadata and event.metadata.get("reasoning_info"):
                            last_reasoning_info = event.metadata["reasoning_info"]

                    elif event.type == "error":
                        # Send error event
                        error_data = {"error": event.error or "An error occurred"}
                        if event.metadata and event.metadata.get("error_id"):
                            error_data["error_id"] = event.metadata["error_id"]
                        yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

                elif queue_event["type"] == "stream_end":
                    # Flush any text the LaTeX normalizer was holding back.
                    trailing = latex_normalizer.flush()
                    if trailing:
                        yield f"event: text\ndata: {json.dumps({'content': trailing})}\n\n"
                    # Send the end event once at the true end of the stream
                    # ast-grep-ignore: no-dict-any - SSE end event payload optionally includes provider-specific reasoning_info
                    done_data: dict[str, Any] = {}
                    if last_reasoning_info:
                        done_data["reasoning_info"] = last_reasoning_info
                    yield f"event: end\ndata: {json.dumps(done_data)}\n\n"
                    break

                elif queue_event["type"] == "error":
                    # Flush any text the LaTeX normalizer was holding back
                    # before reporting the error -- otherwise partial output
                    # generated before the failure would be silently dropped.
                    trailing = latex_normalizer.flush()
                    if trailing:
                        yield f"event: text\ndata: {json.dumps({'content': trailing})}\n\n"
                    error_id = str(uuid.uuid4())
                    logger.error(f"Streaming error {error_id}: {queue_event['error']}")
                    yield f"event: error\ndata: {json.dumps({'error': queue_event['error'], 'error_id': error_id})}\n\n"
                    break

        except asyncio.CancelledError:
            # The SSE client went away (e.g. the app was closed or backgrounded).
            # Mark the turn as disconnected so the background task delivers the
            # reply via push notification, and skip the close event on the now
            # dead connection.
            send_close_event = False
            client_disconnected.set()
            raise
        except Exception as e:
            error_id = str(uuid.uuid4())
            logger.error(f"Streaming error {error_id}: {e}", exc_info=True)
            # Flush buffered normalizer text so partial assistant output
            # isn't lost when the stream fails mid-construct.
            trailing = latex_normalizer.flush()
            if trailing:
                yield f"event: text\ndata: {json.dumps({'content': trailing})}\n\n"
            # Send error event to client
            error_msg = "An error occurred while processing your request"
            if getattr(request.app.state, "debug_mode", False):
                error_msg = str(e)
            yield f"event: error\ndata: {json.dumps({'error': error_msg, 'error_id': error_id})}\n\n"
        finally:
            if not stream_task.done():
                if client_disconnected.is_set():
                    # Detach the processing task so it runs to completion in the
                    # background and delivers the reply via push notification,
                    # instead of being cancelled with the request. Keep a strong
                    # reference so it isn't garbage collected before it finishes.
                    background_tasks = _get_background_chat_tasks(request.app)
                    background_tasks.add(stream_task)
                    stream_task.add_done_callback(background_tasks.discard)
                    stream_task.add_done_callback(_log_detached_stream_result)
                else:
                    stream_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stream_task
            if send_close_event:
                # Send a final close event to ensure client knows stream is done
                yield f"event: close\ndata: {json.dumps({})}\n\n"

    response = StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
            "Access-Control-Allow-Origin": "*",  # CORS support
        },
    )
    return response


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


@chat_api_router.get("/v1/chat/events")
async def live_message_events(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    conversation_id: str,
    interface_type: str = "web",
    after: str | None = None,
) -> StreamingResponse:
    """
    Server-Sent Events endpoint for live message updates.

    Provides real-time notifications when new messages arrive in a conversation.
    Uses a hybrid approach: instant delivery via notification queue with
    periodic polling fallback for resilience.

    Args:
        current_user: Authenticated user (from dependency)
        conversation_id: The conversation to monitor
        interface_type: Interface type filter (default: "web")
        after: Optional ISO timestamp to get only messages after this time

    Returns:
        SSE stream with message update events
    """
    # Parse initial timestamp for catch-up scenario
    after_dt = None
    if after:
        try:
            after_dt = datetime.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid timestamp format. Use ISO format (e.g., 2024-01-15T10:30:00Z): {e}",
            ) from e

    # Get MessageNotifier from app state
    message_notifier = getattr(request.app.state, "message_notifier", None)
    if not message_notifier:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Live updates not configured",
        )

    async def query_new_messages(
        last_check_time: datetime,
    ) -> tuple[list[MessageDict], datetime]:
        """
        Helper to query for new messages.

        Returns:
            Tuple of (message dicts with database fields, updated last_check timestamp)
        """
        async with get_db_context(request.app.state.database_engine) as db_context:
            raw_messages = await db_context.message_history.get_messages_after_as_dict(
                conversation_id=conversation_id,
                after=last_check_time,
                interface_type=interface_type,
            )
        # Type cast: Repository returns dict[str, Any] but we know the structure matches MessageDict
        messages: list[MessageDict] = raw_messages  # type: ignore[assignment]
        # Get the latest timestamp from messages, or use current time if no messages
        if messages:
            latest_timestamp = max(msg["timestamp"] for msg in messages)
            return messages, latest_timestamp
        return messages, last_check_time

    def _create_sse_message(msg: MessageDict) -> str:
        """Create a formatted SSE message string from a message dict with database fields."""
        # Process tool_calls if they are typed objects
        content_tool_calls = None
        if msg.get("tool_calls"):
            content_tool_calls = []
            for tc in msg["tool_calls"]:
                if isinstance(tc, ToolCallItem):
                    # Ensure arguments is a JSON string
                    args = tc.function.arguments
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    content_tool_calls.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": args,
                        },
                    })
                elif isinstance(tc, dict):
                    content_tool_calls.append(tc)

        # Frontend expects: internal_id, timestamp, new_messages, role, content, tool_calls
        data = {
            "internal_id": msg["internal_id"],
            "timestamp": msg["timestamp"].isoformat()
            if isinstance(msg["timestamp"], datetime)
            else msg["timestamp"],
            "conversation_id": msg["conversation_id"],
            "interface_type": msg["interface_type"],
            "new_messages": True,
            "role": msg["role"],
            "content": msg.get("content", ""),
            "tool_calls": content_tool_calls or [],
        }

        return f"event: message\ndata: {json.dumps(data, default=str)}\n\n"

    async def event_generator() -> AsyncGenerator[str]:
        """Generate SSE formatted events for message updates."""
        # Register as a listener
        queue = await message_notifier.register(conversation_id, interface_type)

        # Get shutdown event from app state
        shutdown_event = getattr(request.app.state, "shutdown_event", None)

        try:
            # Send initial heartbeat
            yield f"event: connected\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

            # Initialize last_check timestamp BEFORE any queries to avoid race condition
            last_check = after_dt if after_dt else datetime.now(UTC)
            logger.debug(
                f"SSE stream started for {conversation_id}. last_check={last_check}"
            )

            # If after timestamp provided, send catch-up messages immediately
            if after_dt:
                messages, last_check = await query_new_messages(after_dt)
                for msg in messages:
                    yield _create_sse_message(msg)

            # Main event loop - hybrid approach
            queue_task = None
            shutdown_task = None
            try:
                while True:
                    # Create tasks for message notification and shutdown event
                    queue_task = asyncio.create_task(queue.get())
                    tasks = [queue_task]

                    # Add shutdown event wait if available
                    if shutdown_event:
                        shutdown_task = asyncio.create_task(shutdown_event.wait())
                        tasks.append(shutdown_task)

                    # Wait for first completion with 5 second timeout for heartbeat
                    done, pending = await asyncio.wait(
                        tasks, timeout=5.0, return_when=asyncio.FIRST_COMPLETED
                    )

                    # Cancel pending tasks
                    for task in pending:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

                    # Check if shutdown was triggered
                    if shutdown_event and shutdown_event.is_set():
                        logger.debug(
                            f"SSE stream for conversation {conversation_id} terminating due to shutdown"
                        )
                        break

                    # Check if we got a notification
                    if queue_task in done:
                        # Notification received - query for new messages
                        logger.debug(f"SSE notification received for {conversation_id}")
                        messages, last_check = await query_new_messages(last_check)
                        logger.debug(f"SSE found {len(messages)} new messages")
                        for msg in messages:
                            yield _create_sse_message(msg)
                    else:
                        # Timeout - send heartbeat and poll for messages
                        yield f"event: heartbeat\ndata: {json.dumps({'timestamp': datetime.now(UTC).isoformat()})}\n\n"

                        # Polling fallback - check for any messages since last check
                        messages, last_check = await query_new_messages(last_check)
                        if messages:
                            logger.debug(f"SSE poll found {len(messages)} new messages")
                        for msg in messages:
                            yield _create_sse_message(msg)

            except Exception as e:
                logger.error(f"Error in SSE event loop: {e}", exc_info=True)
                yield f"event: error\ndata: {json.dumps({'error': 'An error occurred'})}\n\n"
            finally:
                # Clean up any remaining tasks
                for task in [queue_task, shutdown_task]:
                    if task and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

        finally:
            # Unregister listener on disconnect
            await message_notifier.unregister(conversation_id, interface_type, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
