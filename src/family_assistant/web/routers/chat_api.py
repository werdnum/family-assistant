import asyncio
import base64
import binascii
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from family_assistant.processing import ProcessingService
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.web.confirmation_manager import web_confirmation_manager
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
    get_processing_service,
)
from family_assistant.web.models import ChatMessageResponse, ChatPromptRequest

if TYPE_CHECKING:
    from family_assistant.services.attachment_registry import AttachmentRegistry

logger = logging.getLogger(__name__)
chat_api_router = APIRouter()


async def _process_user_attachments(
    payload: ChatPromptRequest,
    conversation_id: str,
    attachment_registry: "AttachmentRegistry",
    db_context: DatabaseContext,
    user_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
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
    trigger_content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": payload.prompt}
    ]
    trigger_attachments: list[dict[str, Any]] | None = None

    if payload.attachments:
        trigger_attachments = []
        for attachment in payload.attachments:
            if attachment.get("type") == "image":
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
                    content_data = attachment["content"]

                    # New flow: Handle URL references to uploaded attachments
                    if content_data.startswith("/api/attachments/"):
                        # Content is a URL reference to an already uploaded attachment
                        # Extract attachment ID from URL like "/api/attachments/12345"
                        attachment_id = content_data.split("/")[-1]

                        # First try to atomically claim unlinked attachment for this conversation
                        attachment_record = (
                            await attachment_registry.claim_unlinked_attachment(
                                db_context=db_context,
                                attachment_id=attachment_id,
                                conversation_id=conversation_id,
                                required_source_id=user_id,
                            )
                        )

                        # If not claimed (already linked), get existing attachment record
                        if not attachment_record:
                            attachment_record = (
                                await attachment_registry.get_attachment(
                                    db_context=db_context,
                                    attachment_id=attachment_id,
                                )
                            )

                        if not attachment_record:
                            raise HTTPException(
                                status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Attachment not found: {attachment_id}",
                            )

                        # Add image content for LLM processing using the content_url
                        trigger_content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": attachment_record.content_url},
                        })

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
                            filename = attachment.get(
                                "filename", f"upload_{uuid.uuid4().hex[:8]}"
                            )
                        else:
                            # Assume direct base64 content
                            content_bytes = base64.b64decode(content_data)
                            # For security, don't trust client-provided filenames for MIME type
                            # Instead, try to detect from content magic bytes or use safe default
                            filename = attachment.get(
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

                        # Filename was determined above with fallback

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

                        # Add image content for LLM processing using the content_url
                        trigger_content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": attachment_record.content_url},
                        })

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
                except Exception as e:
                    logger.error(f"Error processing user attachment: {e}")
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
    content: str | None = Field(None, description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")
    tool_calls: list[dict] | None = Field(None, description="Tool calls if any")
    tool_call_id: str | None = Field(None, description="Tool call ID for tool messages")
    error_traceback: str | None = Field(None, description="Error traceback if any")
    attachments: list[dict] | None = Field(
        None, description="Attachment metadata if any"
    )
    processing_profile_id: str | None = Field(
        None, description="ID of the processing profile that generated this message"
    )


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


class ToolConfirmationRequest(BaseModel):
    """Request to confirm or reject a tool execution."""

    request_id: str = Field(..., description="Confirmation request ID")
    approved: bool = Field(..., description="Whether the tool execution is approved")
    conversation_id: str | None = Field(
        None, description="Optional conversation ID for validation"
    )


class ToolConfirmationResponse(BaseModel):
    """Response for tool confirmation request."""

    success: bool = Field(
        ..., description="Whether the confirmation was processed successfully"
    )
    message: str | None = Field(None, description="Optional status message")


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
        if profile_id_requested in processing_services_registry:
            selected_processing_service = processing_services_registry[
                profile_id_requested
            ]
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
    trigger_content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": payload.prompt}
    ]
    trigger_attachments: list[dict[str, Any]] | None = None

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
    # For API, user_name can be generic or derived from auth if implemented
    user_name_for_api = (
        "API User"  # payload.user_name is not available on ChatPromptRequest
    )

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

    result = await selected_processing_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=interface_type,  # Use the interface_type from request or default "api"
        conversation_id=conversation_id,
        trigger_content_parts=trigger_content_parts,
        trigger_interface_message_id=None,  # API prompts don't have a prior interface ID
        user_name=user_name_for_api,
        replied_to_interface_id=None,  # payload.replied_to_message_id is not available on ChatPromptRequest
        chat_interface=None,  # API doesn't use interactive chat elements for confirmation (yet)
        request_confirmation_callback=None,  # No confirmation callback for API (yet)
        trigger_attachments=trigger_attachments,  # Pass attachment metadata
    )

    final_reply_content = result.text_reply
    _final_assistant_message_internal_id = (
        result.assistant_message_internal_id
    )  # Not used by API response
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

    return ChatMessageResponse(
        reply=final_reply_content,  # Back to original field name
        conversation_id=conversation_id,  # Return the used/generated conversation_id
        turn_id=response_turn_id,  # Return the turn_id generated for the response model
        attachments=trigger_attachments,  # Include processed attachments in response
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
            date_from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date_from format: '{date_from}'. Expected YYYY-MM-DD format.",
            ) from e

    if date_to:
        try:
            # Set to end of day to include all messages from the target date
            date_to_dt = datetime.strptime(date_to, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
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
            key=lambda msg: msg.get(
                "timestamp", datetime.min.replace(tzinfo=timezone.utc)
            )
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

        response_messages.append(
            ConversationMessage(
                internal_id=msg["internal_id"],
                role=msg["role"],
                content=msg.get("content"),
                timestamp=msg["timestamp"],
                tool_calls=msg.get("tool_calls"),
                tool_call_id=msg.get("tool_call_id"),
                error_traceback=msg.get("error_traceback"),
                attachments=msg.get("attachments"),
                processing_profile_id=msg.get("processing_profile_id"),
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
    db_context: Annotated[DatabaseContext, Depends(get_db)],
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
        if profile_id_requested in processing_services_registry:
            selected_processing_service = processing_services_registry[
                profile_id_requested
            ]
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
    trigger_content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": payload.prompt}
    ]
    attachment_metadata: list[dict[str, Any]] | None = None

    if payload.attachments:
        # Only get attachment registry when we actually have attachments
        attachment_registry = await get_attachment_registry(request)
        trigger_content_parts, attachment_metadata = await _process_user_attachments(
            payload,
            conversation_id,
            attachment_registry,
            db_context,
            current_user["user_identifier"],
        )

    interface_type = payload.interface_type or "api"
    user_name_for_api = "API User"

    async def event_generator() -> AsyncGenerator[str, None]:
        """Generate SSE formatted events from the processing stream."""

        # Queue for confirmation events
        confirmation_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Get a fresh database context for the stream
        async with get_db_context(
            request.app.state.database_engine
        ) as stream_db_context:
            # Create confirmation callback that queues events
            async def web_confirmation_callback(
                interface_type_cb: str,
                conversation_id_cb: str,
                interface_message_id_cb: str | None,
                tool_name: str,
                tool_call_id: str,
                tool_args: dict[str, Any],
                timeout_seconds: float,
            ) -> bool:
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

                # Create confirmation request
                (
                    request_id,
                    future,
                ) = await web_confirmation_manager.request_confirmation(
                    conversation_id=conversation_id_cb,
                    interface_type=interface_type_cb,
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
                    "tool_call_id": tool_call_id,
                    "confirmation_prompt": confirmation_prompt,
                    "timeout_seconds": timeout_seconds,
                    "args": tool_args,
                })

                # Wait for user response
                try:
                    approved = await future

                    # Queue confirmation result event
                    await confirmation_queue.put({
                        "type": "confirmation_result",
                        "request_id": request_id,
                        "approved": approved,
                    })

                    return approved
                except asyncio.CancelledError:
                    # Handle cancellation
                    return False
                except Exception as e:
                    # Handle any other exceptions
                    logger.error(f"Error waiting for confirmation {request_id}: {e}")
                    return False

            # Create task to process the interaction stream
            async def process_stream() -> None:
                try:
                    async for (
                        event
                    ) in selected_processing_service.handle_chat_interaction_stream(
                        db_context=stream_db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        trigger_content_parts=trigger_content_parts,
                        trigger_interface_message_id=None,
                        user_name=user_name_for_api,
                        replied_to_interface_id=None,
                        chat_interface=None,
                        request_confirmation_callback=web_confirmation_callback,
                        trigger_attachments=attachment_metadata,  # Pass attachment metadata
                    ):
                        if event.type == "error":
                            logger.error(f"Stream event error: {event.error}")
                        # Add events to queue
                        await confirmation_queue.put({
                            "type": "stream_event",
                            "event": event,
                        })

                    # Signal end of stream
                    await confirmation_queue.put({"type": "stream_end"})
                except Exception as e:
                    # Queue error event
                    logger.error(f"Error in process_stream: {e}", exc_info=True)
                    await confirmation_queue.put({"type": "error", "error": str(e)})

            # Emit attachment events for user-uploaded attachments first
            if attachment_metadata:
                for attachment in attachment_metadata:
                    attachment_event_data = {
                        "type": "attachment",
                        "attachment_id": attachment["attachment_id"],
                        "url": attachment["content_url"],
                        "content_url": attachment["content_url"],
                        "mime_type": attachment["mime_type"],
                        "description": attachment["description"],
                        "size": attachment["size"],
                    }
                    yield f"event: attachment\ndata: {json.dumps(attachment_event_data)}\n\n"

            # Start the stream processing task
            stream_task = asyncio.create_task(process_stream())

            try:
                # Process events from queue and yield SSE events
                while True:
                    try:
                        # Get next event from queue with timeout
                        queue_event = await asyncio.wait_for(
                            confirmation_queue.get(), timeout=0.1
                        )
                    except asyncio.TimeoutError:
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
                            # Send text content chunks
                            yield f"event: text\ndata: {json.dumps({'content': event.content})}\n\n"

                        elif event.type == "tool_call":
                            # Convert tool_call to dict for JSON serialization
                            if event.tool_call:
                                tool_call_dict = {
                                    "id": event.tool_call.id,
                                    "function": {
                                        "name": event.tool_call.function.name,
                                        "arguments": event.tool_call.function.arguments,
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
                            # Handle attachment IDs from attach_to_response tool calls
                            if event.metadata and "attachment_ids" in event.metadata:
                                # Get attachment registry to fetch attachment metadata
                                attachment_registry = await get_attachment_registry(
                                    request
                                )

                                for attachment_id in event.metadata["attachment_ids"]:
                                    try:
                                        # Get attachment metadata from the registry
                                        attachment_info = (
                                            await attachment_registry.get_attachment(
                                                stream_db_context, attachment_id
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

                            # Send completion event with optional metadata
                            done_data: dict[str, Any] = {}
                            if event.metadata and event.metadata.get("reasoning_info"):
                                done_data["reasoning_info"] = event.metadata[
                                    "reasoning_info"
                                ]
                            yield f"event: end\ndata: {json.dumps(done_data)}\n\n"

                        elif event.type == "error":
                            # Send error event
                            error_data = {"error": event.error or "An error occurred"}
                            if event.metadata and event.metadata.get("error_id"):
                                error_data["error_id"] = event.metadata["error_id"]
                            yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

                    elif queue_event["type"] == "stream_end":
                        break

                    elif queue_event["type"] == "error":
                        error_id = str(uuid.uuid4())
                        logger.error(
                            f"Streaming error {error_id}: {queue_event['error']}"
                        )
                        yield f"event: error\ndata: {json.dumps({'error': queue_event['error'], 'error_id': error_id})}\n\n"
                        break

            except Exception as e:
                error_id = str(uuid.uuid4())
                logger.error(f"Streaming error {error_id}: {e}", exc_info=True)
                # Send error event to client
                error_msg = "An error occurred while processing your request"
                if getattr(request.app.state, "debug_mode", False):
                    error_msg = str(e)
                yield f"event: error\ndata: {json.dumps({'error': error_msg, 'error_id': error_id})}\n\n"
            finally:
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

    async def simple_event_generator() -> AsyncGenerator[str, None]:
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
    success = await web_confirmation_manager.handle_confirmation_response(
        request_id=payload.request_id,
        approved=payload.approved,
        conversation_id=payload.conversation_id,
    )

    if success:
        message = f"Tool execution {'approved' if payload.approved else 'rejected'}"
        logger.info(f"Confirmation {payload.request_id}: {message}")
    else:
        message = "Confirmation request not found or already processed"
        logger.warning(f"Failed to process confirmation {payload.request_id}")

    return ToolConfirmationResponse(
        success=success,
        message=message,
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
    processing_services_registry = getattr(request.app.state, "processing_services", {})

    profiles = []

    # Add all profiles from the registry
    for profile_id, service in processing_services_registry.items():
        # Get service configuration
        service_config = service.service_config

        # Extract available tools from tools provider
        available_tools = []
        enabled_mcp_servers = []

        if hasattr(service, "tools_provider") and service.tools_provider:
            # Get local tools
            if hasattr(service.tools_provider, "local_tools_provider"):
                local_provider = service.tools_provider.local_tools_provider
                if local_provider and hasattr(local_provider, "available_functions"):
                    available_tools.extend(local_provider.available_functions.keys())

            # Get MCP server tools
            if hasattr(service.tools_provider, "mcp_tools_provider"):
                mcp_provider = service.tools_provider.mcp_tools_provider
                if mcp_provider and hasattr(mcp_provider, "server_configs"):
                    enabled_mcp_servers.extend(mcp_provider.server_configs.keys())

        # Get description from service config or generate a fallback
        description = getattr(service_config, "description", None)
        if not description:
            # Generate a user-friendly description based on profile ID
            if profile_id == "default_assistant":
                description = "General-purpose AI assistant with access to your notes, calendar, and tools"
            elif profile_id == "browser":
                description = "Web browsing assistant with internet search and page interaction capabilities"
            elif profile_id == "research":
                description = "Research specialist using advanced models for deep information gathering"
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
