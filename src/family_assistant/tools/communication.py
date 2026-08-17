"""Communication and messaging tools.

This module contains tools for sending messages to users and retrieving
conversation history.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from family_assistant.interfaces import ChatDeliveryError
from family_assistant.llm.messages import AssistantMessage, MessageReasoningInfo
from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.security.taint import (
    TurnTaintState,
    merge_taint_state_into_tracker,
)
from family_assistant.storage.vector_search import (
    MetadataFilter,
    VectorSearchQuery,
    query_vector_store,
)
from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.embeddings import EmbeddingGenerator
    from family_assistant.storage.repositories.message_history import (
        MessageHistoryQuery,
    )
    from family_assistant.storage.types import MessageHistoryRow
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
COMMUNICATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "get_message_history",
            "description": (
                "Structured and semantic lookup against past conversation history. Use structured mode for exact filters like dates, roles, tools, attachments, or errors; semantic mode for fuzzy recall; and hybrid mode when both are useful. "
                "Returns compact message summaries (each with a stable message_id, timestamp, role, content, and optional neighboring context), or an empty list when nothing matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional natural language or keyword text to search for.",
                    },
                    "search_mode": {
                        "type": "string",
                        "enum": ["structured", "semantic", "hybrid"],
                        "description": "Use structured for exact filters, semantic for fuzzy recall, or hybrid for both.",
                        "default": "structured",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional conversation filter. Narrows results to one conversation.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": [
                            "same_user",
                            "current_conversation",
                            "all_accessible",
                        ],
                        "description": "Search scope. Defaults to same_user when user identity is available. all_accessible is denied unless policy enables it.",
                        "default": "same_user",
                    },
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["user", "assistant", "tool", "system", "error"],
                        },
                        "description": "Optional role filters.",
                    },
                    "tool_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tool names to find in tool responses or assistant tool calls.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Optional inclusive ISO datetime lower bound.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Optional inclusive ISO datetime upper bound.",
                    },
                    "has_attachments": {
                        "type": "boolean",
                        "description": "Optional filter for messages with or without attachments.",
                    },
                    "has_error": {
                        "type": "boolean",
                        "description": "Optional filter for error messages or tool/message rows with tracebacks.",
                    },
                    "processing_profile_id": {
                        "type": "string",
                        "description": "Optional processing profile filter. Omit to use the current profile. Use '*' only when explicitly broadening profile scope is appropriate.",
                    },
                    "subconversation_id": {
                        "type": "string",
                        "description": "Optional subconversation filter. Omit to use the current subconversation/main conversation. Use '*' only when explicitly broadening subconversation scope is appropriate.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional maximum number of results. Default is 20, maximum is 100.",
                        "default": 20,
                    },
                    "include_context": {
                        "type": "integer",
                        "description": "Optional number of neighboring messages per side to include for each result. Default is 0, maximum is 10.",
                        "default": 0,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": (
                "Sends a textual message to another known user. Use this tool ONLY when explicitly requested to message a specific person (e.g., 'Tell Alice...'). "
                "Do NOT use this tool for reminders or notifications unless specifically asked to notify another person. "
                "For normal reminders, simply write the text in your response and it will be delivered to the current user automatically. "
                "You MUST use the recipient's Chat ID as the target, which is provided in the 'Known users' section of the `<turn_context>` block at the end of the conversation. "
                "Optionally, you can include attachments with the message.\n\n"
                "The target must be an existing conversation that an authorized user has already used to talk to the assistant; invented or guessed IDs are rejected.\n\n"
                "Returns: A string indicating the result. "
                "On success, returns 'Message sent successfully to user with Chat ID [chat_id].'. "
                "If message is sent but history recording fails, returns 'Message sent to user with Chat ID [chat_id], but failed to record in history.'. "
                "On error, returns 'Error: Could not send message to Chat ID [chat_id]. Details: [error details]', 'Error: Chat interface not available.',"
                " 'Error: Chat ID [chat_id] is not a known conversation with an authorized user of this assistant.',"
                " or 'Error: Cannot use send_message_to_user tool to send a message to the user you are already replying to. The user will receive your final response directly in this conversation.'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_chat_id": {
                        "type": "string",
                        "description": "The conversation ID of the recipient. For Telegram conversations, this is the numeric Chat ID. For web conversations, this is a UUID. The ID must be from a known conversation provided in the system context, and must belong to an authorized user who has already talked to the assistant.",
                    },
                    "message_content": {
                        "type": "string",
                        "description": "The content of the message to send to the user.",
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of attachment UUIDs to send along with the message. These must be accessible in the current conversation.",
                    },
                },
                "required": ["target_chat_id", "message_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_attachment_info",
            "description": (
                "Retrieve metadata information about a specific attachment by its UUID. "
                "This includes details like file size, MIME type, description, source, and creation time.\n\n"
                "Returns: A JSON string containing attachment metadata including attachment_id, mime_type, "
                "description, size, source_type, source_id, conversation_id, created_at, and any additional metadata. "
                "On error, returns 'Error: [error details]'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "string",
                        "description": "The UUID of the attachment to retrieve information about.",
                    },
                },
                "required": ["attachment_id"],
            },
        },
    },
]


# Tool Implementations
async def get_message_history_tool(
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator | None = None,
    query: str | None = None,
    search_mode: Literal["structured", "semantic", "hybrid"] = "structured",
    conversation_id: str | None = None,
    scope: Literal["same_user", "current_conversation", "all_accessible"] = "same_user",
    roles: list[str] | None = None,
    tool_names: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    has_attachments: bool | None = None,
    has_error: bool | None = None,
    processing_profile_id: str | None = None,
    subconversation_id: str | None = None,
    limit: int = 20,
    include_context: int = 0,
) -> ToolResult:
    """
    Query persisted message history with structured filters and optional semantic search.

    Args:
        exec_context: The execution context containing chat_id and db_context.
        embedding_generator: Optional embedding generator for semantic/hybrid search.
        query: Natural language or keyword query.
        search_mode: structured, semantic, or hybrid.
        conversation_id: Optional conversation filter.
        scope: Access scope.
        roles: Optional role filters.
        tool_names: Optional tool name filters.
        start_time: Optional inclusive ISO datetime lower bound.
        end_time: Optional inclusive ISO datetime upper bound.
        has_attachments: Optional attachment presence filter.
        has_error: Optional error presence filter.
        processing_profile_id: Optional profile filter; defaults to current profile.
        subconversation_id: Optional subconversation filter; defaults to current value.
        limit: Maximum number of results.
        include_context: Number of neighboring messages per side to include.

    Returns:
        Structured tool result containing compact message history results.
    """
    from family_assistant.storage.repositories.message_history import (  # noqa: PLC0415
        MessageHistoryAccessDeniedError,
        MessageHistoryQuery,
    )

    db_context = exec_context.db_context
    effective_embedding_generator = (
        embedding_generator or exec_context.embedding_generator
    )
    effective_profile_id = (
        exec_context.processing_profile_id
        if processing_profile_id is None
        else processing_profile_id
    )
    effective_subconversation_id = (
        exec_context.subconversation_id
        if subconversation_id is None
        else subconversation_id
    )
    try:
        history_query = MessageHistoryQuery(
            query=query,
            search_mode=search_mode,
            conversation_id=conversation_id,
            scope=scope,
            roles=tuple(roles or ()),
            tool_names=tuple(tool_names or ()),
            start_time=_parse_optional_datetime(start_time, "start_time"),
            end_time=_parse_optional_datetime(end_time, "end_time"),
            has_attachments=has_attachments,
            has_error=has_error,
            processing_profile_id=effective_profile_id,
            subconversation_id=effective_subconversation_id,
            limit=limit,
            include_context=include_context,
            interface_type=exec_context.interface_type,
            current_conversation_id=exec_context.conversation_id,
            current_user_id=exec_context.user_id,
        )
        rows = await _execute_message_history_query(
            exec_context=exec_context,
            embedding_generator=effective_embedding_generator,
            history_query=history_query,
        )
        returned_rows = await _collect_returned_history_rows(
            exec_context=exec_context,
            rows=rows,
            include_context=include_context,
            history_query=history_query,
        )
        _merge_message_history_taint(exec_context, returned_rows)
        data = {
            "search_mode": search_mode,
            "scope": scope,
            "query": query,
            "result_count": len(rows),
            "results": await db_context.message_history.hydrate_history_results(
                rows,
                include_context=include_context,
                access_query=history_query,
            ),
        }
        return ToolResult(data=data)
    except MessageHistoryAccessDeniedError as exc:
        return ToolResult(
            data={
                "error": "access_denied",
                "message": str(exc),
                "results": [],
            }
        )
    except ValueError as exc:
        return ToolResult(
            data={
                "error": "invalid_request",
                "message": str(exc),
                "results": [],
            }
        )
    except Exception as exc:
        logger.exception("Error executing get_message_history_tool: %s", exc)
        return ToolResult(
            data={
                "error": "query_failed",
                "message": f"Failed to retrieve message history. {exc}",
                "results": [],
            }
        )


async def _execute_message_history_query(
    *,
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator | None,
    history_query: MessageHistoryQuery,
) -> list[MessageHistoryRow]:
    if history_query.search_mode == "structured":
        return await exec_context.db_context.message_history.query_history(
            history_query
        )
    if not history_query.query:
        raise ValueError("query is required for semantic or hybrid message search.")
    semantic_rows = await _semantic_message_history_rows(
        exec_context=exec_context,
        embedding_generator=embedding_generator,
        history_query=history_query,
    )
    if history_query.search_mode == "semantic":
        return semantic_rows

    structured_rows = await exec_context.db_context.message_history.query_history(
        history_query
    )
    rows_by_id = {row["internal_id"]: row for row in semantic_rows}
    for row in structured_rows:
        rows_by_id.setdefault(row["internal_id"], row)
    return list(rows_by_id.values())[: max(min(history_query.limit, 100), 1)]


async def _collect_returned_history_rows(
    *,
    exec_context: ToolExecutionContext,
    rows: list[MessageHistoryRow],
    include_context: int,
    history_query: MessageHistoryQuery,
) -> list[MessageHistoryRow]:
    bounded_context = min(max(include_context, 0), 3)
    if not bounded_context:
        return rows
    returned: list[MessageHistoryRow] = []
    seen_internal_ids: set[int] = set()
    for row in rows:
        context_rows = (
            await exec_context.db_context.message_history.get_context_around_message(
                row,
                per_side=bounded_context,
                access_query=history_query,
            )
        )
        for context_row in context_rows:
            internal_id = context_row["internal_id"]
            if internal_id in seen_internal_ids:
                continue
            seen_internal_ids.add(internal_id)
            returned.append(context_row)
    return returned


def _merge_message_history_taint(
    exec_context: ToolExecutionContext,
    rows: list[MessageHistoryRow],
) -> None:
    if exec_context.taint_tracker is None:
        return
    for row in rows:
        taint_metadata = row.get("taint_metadata")
        if taint_metadata is None:
            continue
        row_state = TurnTaintState.from_metadata(taint_metadata, from_history=True)
        merge_taint_state_into_tracker(
            exec_context.taint_tracker,
            row_state,
            from_history=True,
        )


async def _semantic_message_history_rows(
    *,
    exec_context: ToolExecutionContext,
    embedding_generator: EmbeddingGenerator | None,
    history_query: MessageHistoryQuery,
) -> list[MessageHistoryRow]:
    if embedding_generator is None:
        raise ValueError("Semantic message-history search requires embeddings.")
    embedding_result = await embedding_generator.generate_embeddings([
        history_query.query or ""
    ])
    if not embedding_result.embeddings:
        raise ValueError("Failed to generate embedding for message-history query.")

    source_ids = (
        await exec_context.db_context.message_history.get_index_source_ids_for_query(
            history_query,
            limit=_semantic_search_source_prefilter_limit(),
        )
    )
    if not source_ids:
        return []

    search_query = VectorSearchQuery(
        search_type="hybrid",
        semantic_query=history_query.query,
        keywords=history_query.query,
        embedding_model=embedding_result.model_name,
        source_types=["message_history"],
        source_ids=source_ids,
        embedding_types=["message_turn"],
        metadata_filters=_message_history_metadata_filters(history_query),
        limit=max(min(history_query.limit, 100), 1),
        visibility_grants=exec_context.visibility_grants,
    )
    search_results = await query_vector_store(
        db_context=exec_context.db_context,
        query=search_query,
        query_embedding=embedding_result.embeddings[0],
    )
    turn_ids: list[str] = []
    internal_ids: list[int] = []
    for result in search_results:
        source_id = result.get("source_id")
        if not isinstance(source_id, str):
            continue
        if source_id.startswith("message_turn:"):
            turn_ids.append(source_id.removeprefix("message_turn:"))
        elif source_id.startswith("message_row:"):
            try:
                internal_ids.append(int(source_id.removeprefix("message_row:")))
            except ValueError:
                continue

    return await exec_context.db_context.message_history.get_rows_by_search_references(
        turn_ids=tuple(turn_ids),
        internal_ids=tuple(internal_ids),
        access_query=history_query,
    )


def _semantic_search_source_prefilter_limit() -> int:
    return 5000


def _message_history_metadata_filters(
    history_query: MessageHistoryQuery,
) -> list[MetadataFilter]:
    filters: list[MetadataFilter] = []
    if history_query.scope == "current_conversation":
        conversation_id = history_query.current_conversation_id
        if conversation_id:
            filters.append(MetadataFilter(key="conversation_id", value=conversation_id))
    elif history_query.scope == "same_user":
        conversation_id = history_query.conversation_id
        if conversation_id:
            filters.append(MetadataFilter(key="conversation_id", value=conversation_id))

    if history_query.interface_type:
        filters.append(
            MetadataFilter(key="interface_type", value=history_query.interface_type)
        )
    if (
        history_query.processing_profile_id is not None
        and history_query.processing_profile_id != "*"
    ):
        filters.append(
            MetadataFilter(
                key="processing_profile_id",
                value=history_query.processing_profile_id,
            )
        )
    if (
        history_query.subconversation_id is not None
        and history_query.subconversation_id != "*"
    ):
        filters.append(
            MetadataFilter(
                key="subconversation_id",
                value=history_query.subconversation_id,
            )
        )
    return filters


def _parse_optional_datetime(value: str | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime.") from exc


class UnknownMessageTargetError(ValueError):
    """Raised when a ``send_message_to_user`` target is not a known conversation."""


async def _resolve_send_message_target(
    exec_context: ToolExecutionContext,
    target_chat_id: str,
) -> str:
    """Resolve the interface type of a validated ``send_message_to_user`` target.

    A conversation is a legitimate target only if an authorized user has already
    talked to the assistant in it: every interface refuses to persist user
    messages from identities it cannot authorize, so a conversation carrying a
    user message is one the bot is allowed to reply into. Without this check the
    model can name any conversation identifier it likes -- an arbitrary Telegram
    chat ID, or a UUID belonging to nobody -- which turns the tool into an
    exfiltration channel for injected instructions.

    Returns:
        The interface type the target conversation belongs to.

    Raises:
        UnknownMessageTargetError: If the target is not a conversation an
            authorized user has talked to the assistant in.
    """
    message_history = exec_context.db_context.message_history
    target_interface_type = await message_history.get_interface_type_for_conversation(
        target_chat_id
    )
    owner_ids = (
        await message_history.get_conversation_owner_ids(target_chat_id)
        if target_interface_type is not None
        else set()
    )
    if target_interface_type is None or not owner_ids:
        logger.warning(
            "Rejected send_message_to_user to unknown conversation %s "
            "(interface_type=%s, owners=%d)",
            target_chat_id,
            target_interface_type,
            len(owner_ids),
        )
        raise UnknownMessageTargetError(
            f"Error: Chat ID {target_chat_id} is not a known conversation with an "
            "authorized user of this assistant. Only use IDs from the 'Known users' "
            "section of the `<turn_context>` block, and only for users who have "
            "already messaged the assistant."
        )
    return target_interface_type


async def send_message_to_user_tool(
    exec_context: ToolExecutionContext,
    target_chat_id: str,
    message_content: str,
    attachment_ids: list[str] | None = None,
) -> str:
    """
    Sends a message to another user via their chat interface.

    This tool works across different interfaces (Telegram, Web, etc.) by detecting
    the interface type from message history and routing to the appropriate ChatInterface.

    Args:
        exec_context: The execution context.
        target_chat_id: The conversation ID of the recipient (Chat ID for Telegram, UUID for Web).
        message_content: The text of the message to send.
        attachment_ids: Optional list of attachment UUIDs to send with the message.

    Returns:
        A string indicating success or failure.
    """

    # Convert target_chat_id to string for consistent database queries
    # (JSON deserialization may pass integers for Telegram chat IDs)
    target_chat_id = str(target_chat_id)

    logger.info(
        f"Executing send_message_to_user_tool to chat_id {target_chat_id} with content: '{message_content[:50]}...' and {len(attachment_ids) if attachment_ids else 0} attachment(s)"
    )
    db_context = exec_context.db_context
    # The turn_id from the exec_context is the ID of the turn that *requested* this tool call.
    # This is useful for linking the sent message back to the originating interaction.
    requesting_turn_id = exec_context.turn_id

    # Validate that the user is not trying to send a message to themselves
    current_conversation_id = exec_context.conversation_id
    if target_chat_id == current_conversation_id:
        logger.warning(
            f"Attempt to send message to self: target_chat_id={target_chat_id}, current_conversation_id={current_conversation_id}"
        )
        return (
            "Error: Cannot use send_message_to_user tool to send a message to the user you are "
            "already replying to. The user will receive your final response directly in this conversation."
        )

    # Detect the interface type for the target conversation, rejecting any
    # conversation that no authorized user has ever talked to the bot in.
    try:
        target_interface_type = await _resolve_send_message_target(
            exec_context, target_chat_id
        )
    except UnknownMessageTargetError as exc:
        return str(exc)

    # Get the appropriate ChatInterface for this interface type
    # Try chat_interfaces dict first (new way), fall back to single chat_interface (old way)
    if exec_context.chat_interfaces:
        chat_interface = exec_context.chat_interfaces.get(target_interface_type)
        if not chat_interface:
            logger.error(
                f"No ChatInterface available for interface type {target_interface_type}"
            )
            return f"Error: Chat interface not available for {target_interface_type}."
    else:
        # Fallback to single chat_interface for backward compatibility
        chat_interface = exec_context.chat_interface
        if not chat_interface:
            logger.error(
                "ChatInterface not available in ToolExecutionContext for send_message_to_user_tool."
            )
            return "Error: Chat interface not available."

    # Validate attachment IDs if provided
    validated_attachment_ids: list[str] | None = None
    if attachment_ids:
        validated_attachment_ids = []
        if exec_context.attachment_registry:
            attachment_registry = exec_context.attachment_registry

            for attachment_id in attachment_ids:
                try:
                    # Handle both string IDs and ScriptAttachment objects
                    if hasattr(attachment_id, "get_id"):
                        # It's a ScriptAttachment object, extract the ID
                        actual_attachment_id = (
                            attachment_id.get_id()
                            if isinstance(attachment_id, ScriptAttachment)
                            else str(attachment_id)
                        )
                    else:
                        # It's a string ID
                        actual_attachment_id = attachment_id

                    attachment = await attachment_registry.get_attachment(
                        exec_context.db_context,
                        actual_attachment_id,
                        acting_user_id=exec_context.user_id,
                    )

                    if not attachment:
                        logger.warning(f"Attachment {actual_attachment_id} not found")
                        continue

                    # Always append the string ID to the validated list
                    validated_attachment_ids.append(actual_attachment_id)
                    logger.debug(
                        f"Validated attachment {actual_attachment_id} for sending"
                    )

                except Exception as e:
                    logger.error(f"Error validating attachment {attachment_id}: {e}")
                    continue
        else:
            logger.warning(
                "AttachmentRegistry not available - cannot validate attachment IDs"
            )
            # Still proceed but without attachments
            validated_attachment_ids = None

    # The recorded message is authored by the LLM within the requesting turn, so
    # it carries that turn's taint state. A context without a tracker (should
    # not happen for LLM-driven calls) falls back to an explicit empty state
    # rather than omitting metadata.
    message_taint_metadata = (
        exec_context.taint_tracker.snapshot().to_metadata()
        if exec_context.taint_tracker is not None
        else TurnTaintState.empty().to_metadata()
    )

    try:
        # Use the ChatInterface to send the message.
        # Assuming the target_chat_id is for the same interface type as the current context.
        # The TelegramChatInterface will handle converting target_chat_id to int.
        try:
            sent_message_id_str = await chat_interface.send_message(
                conversation_id=str(target_chat_id),  # Pass as string
                text=message_content,
                attachment_ids=validated_attachment_ids,
                on_behalf_of_user_id=exec_context.user_id,
                taint_metadata=message_taint_metadata,
                # parse_mode can be added if needed, default is plain text
            )
        except ChatDeliveryError as delivery_error:
            logger.error(
                f"Failed to send message to chat_id {target_chat_id} via "
                f"ChatInterface: {delivery_error}"
            )
            return (
                f"Error: Could not send message to Chat ID {target_chat_id} "
                f"({delivery_error})."
            )

        attachment_msg = ""
        if validated_attachment_ids:
            attachment_msg = f" with {len(validated_attachment_ids)} attachment(s)"

        logger.info(
            f"Message sent to chat_id {target_chat_id}{attachment_msg}. Interface Message ID: {sent_message_id_str}"
        )

        # Record the sent message in history for the target user's chat
        # Note: WebChatInterface already saves to history (to trigger SSE notifications),
        # but TelegramChatInterface does not, so we only save for non-web interfaces
        if target_interface_type != "web":
            try:
                await db_context.message_history.add_message(
                    AssistantMessage(
                        content=message_content,
                        taint_metadata=message_taint_metadata,
                    ),
                    interface_type=target_interface_type,  # Use detected interface type
                    conversation_id=target_chat_id,  # History is for the target user's conversation
                    interface_message_id=sent_message_id_str,
                    turn_id=requesting_turn_id,  # Link to the turn that initiated this action
                    timestamp=datetime.now(UTC),
                    reasoning_info=MessageReasoningInfo(
                        source_turn_id=requesting_turn_id,
                        tool_name="send_message_to_user",
                    ),
                    processing_profile_id=getattr(
                        exec_context, "processing_profile_id", None
                    ),
                )
                logger.info(
                    f"Message sent to chat_id {target_chat_id} was recorded in history."
                )
            except Exception as db_err:
                logger.exception(
                    f"Message sent to chat_id {target_chat_id}, but failed to record in history: {db_err}"
                )
                # Still return success for sending, but note the history failure.
                return f"Message sent to user with Chat ID {target_chat_id}{attachment_msg}, but failed to record in history."

        return f"Message sent successfully to user with Chat ID {target_chat_id}{attachment_msg}."

    except Exception as e:
        logger.exception(f"Failed to send message to chat_id {target_chat_id}: {e}")
        return (
            f"Error: Could not send message to Chat ID {target_chat_id}. Details: {e}"
        )


async def get_attachment_info_tool(
    exec_context: ToolExecutionContext,
    attachment_id: str,
) -> str:
    """
    Retrieve metadata information about a specific attachment.

    Args:
        exec_context: The execution context.
        attachment_id: The UUID of the attachment to retrieve information about.

    Returns:
        A JSON string containing attachment metadata or an error message.
    """
    logger.info(f"Executing get_attachment_info_tool for attachment {attachment_id}")

    db_context = exec_context.db_context

    if not exec_context.attachment_registry:
        logger.error("AttachmentRegistry not available in ToolExecutionContext")
        return "Error: Attachment registry not available."

    try:
        attachment_registry = exec_context.attachment_registry

        # Retrieve attachment metadata
        attachment = await attachment_registry.get_attachment(
            db_context, attachment_id, acting_user_id=exec_context.user_id
        )

        if not attachment:
            logger.warning(f"Attachment {attachment_id} not found")
            return f"Error: Attachment with ID {attachment_id} not found."

        # Convert to dictionary and return as JSON string
        metadata_dict = attachment.to_dict()

        return json.dumps(metadata_dict, indent=2)

    except Exception as e:
        logger.exception(f"Error retrieving attachment info for {attachment_id}: {e}")
        return f"Error: Failed to retrieve attachment information. {e}"
