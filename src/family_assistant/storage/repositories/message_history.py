"""Repository for message history storage operations."""

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import functions as func
from sqlalchemy.sql.elements import ColumnElement

from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPart,
    ErrorMessage,
    ImageUrlContentPart,
    LLMMessage,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    MessageWithMetadata,
    ProviderMetadataDict,
    SystemMessage,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.types import ConversationSummaryRow, MessageHistoryRow

logger = logging.getLogger(__name__)

_MAX_ASSISTANT_ROWS_PER_TOOL_EXAMPLE = 20


@dataclass(frozen=True, slots=True)
class ToolHistoryExample:
    """Historical tool call example reconstructed from persisted message history."""

    tool_name: str
    # ast-grep-ignore: no-dict-any - Persisted tool arguments vary by tool and are reconstructed from JSON
    arguments: dict[str, Any]
    result_content: str
    conversation_id: str
    tool_call_id: str


class MessageHistoryRepository(BaseRepository):
    """Repository for managing message history in the database."""

    async def add_message(
        self,
        message: LLMMessage,
        *,
        interface_type: str,
        conversation_id: str,
        timestamp: datetime,
        interface_message_id: str | None = None,
        turn_id: str | None = None,
        thread_root_id: int | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
    ) -> int | None:
        """
        Stores a typed LLMMessage in the history table.

        The message content fields (role, content, tool_calls, etc.) are extracted from the
        LLMMessage object. Metadata fields (interface_type, conversation_id, etc.) are passed
        as keyword arguments.

        Args:
            message: The typed LLMMessage to store.
            interface_type: Type of interface (e.g., 'telegram', 'web')
            conversation_id: Unique conversation identifier
            timestamp: Message timestamp (must be timezone-aware)
            interface_message_id: Interface-specific message ID
            turn_id: UUID linking messages within a turn
            thread_root_id: ID of the first message in the thread
            processing_profile_id: Processing profile used
            subconversation_id: Subconversation ID for delegation
            user_id: User identifier
            reasoning_info: LLM reasoning/usage info

        Returns:
            The stored message data including generated internal_id, or None on error
        """
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")

        # Extract message content fields from the typed LLMMessage
        role = message.role
        content: str | None = None
        tool_calls: list[ToolCallItem] | None = None
        tool_call_id: str | None = None
        tool_name: str | None = None
        error_traceback: str | None = None
        provider_metadata: ProviderMetadataDict | GeminiProviderMetadata | None = None

        raw_content = getattr(message, "content", None)
        if raw_content is None or isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = json.dumps([
                part.model_dump(mode="json") if hasattr(part, "model_dump") else part
                for part in raw_content
            ])
        else:
            content = json.dumps(raw_content)

        if isinstance(message, AssistantMessage):
            tool_calls = message.tool_calls
            provider_metadata = message.provider_metadata
        elif isinstance(message, ToolMessage):
            tool_call_id = message.tool_call_id
            tool_name = message.name
            error_traceback = message.error_traceback
            provider_metadata = message.provider_metadata
            if attachments is None:
                attachments = message.attachments  # type: ignore[assignment]  # ToolAttachmentMetadata (TypedDict) is a dict at runtime
        elif isinstance(message, ErrorMessage):
            error_traceback = message.error_traceback

        return await self._insert_message(
            interface_type=interface_type,
            conversation_id=conversation_id,
            interface_message_id=interface_message_id,
            turn_id=turn_id,
            thread_root_id=thread_root_id,
            timestamp=timestamp,
            role=role,
            content=content,
            tool_calls=tool_calls,
            reasoning_info=reasoning_info,
            error_traceback=error_traceback,
            tool_call_id=tool_call_id,
            processing_profile_id=processing_profile_id,
            subconversation_id=subconversation_id,
            user_id=user_id,
            attachments=attachments,
            tool_name=tool_name,
            provider_metadata=provider_metadata,
        )

    async def _insert_message(
        self,
        interface_type: str,
        conversation_id: str,
        interface_message_id: str | None,
        turn_id: str | None,
        thread_root_id: int | None,
        timestamp: datetime,
        role: str,
        content: str | None,
        tool_calls: list[ToolCallItem] | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        error_traceback: str | None = None,
        tool_call_id: str | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
        tool_name: str | None = None,
        provider_metadata: ProviderMetadataDict | GeminiProviderMetadata | None = None,
    ) -> int | None:
        """
        Internal method that serializes and inserts a message into the database.

        Returns the generated internal_id, or None on error.
        """
        # Serialize tool_calls for JSON storage
        serialized_tool_calls = None
        if tool_calls:
            serialized_tool_calls = []
            for tc in tool_calls:
                if not isinstance(tc, ToolCallItem):
                    raise TypeError(
                        f"tool_calls must contain ToolCallItem objects; got {type(tc)}"
                    )
                if isinstance(tc.provider_metadata, GeminiProviderMetadata):
                    tc_dict = asdict(tc)
                    tc_dict["provider_metadata"] = tc.provider_metadata.to_dict()
                    serialized_tool_calls.append(tc_dict)
                else:
                    tc_dict = asdict(tc)
                    serialized_tool_calls.append(tc_dict)

        serialized_provider_metadata = None
        if provider_metadata:
            if isinstance(provider_metadata, GeminiProviderMetadata):
                serialized_provider_metadata = provider_metadata.to_dict()
            else:
                serialized_provider_metadata = provider_metadata

        values = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "interface_message_id": interface_message_id,
            "turn_id": turn_id,
            "thread_root_id": thread_root_id,
            "timestamp": timestamp,
            "role": role,
            "content": content,
            "tool_calls": serialized_tool_calls,
            "reasoning_info": reasoning_info,
            "tool_call_id": tool_call_id,
            "error_traceback": error_traceback,
            "processing_profile_id": processing_profile_id,
            "subconversation_id": subconversation_id,
            "attachments": attachments,
            "tool_name": tool_name,
            "provider_metadata": serialized_provider_metadata,
            "user_id": user_id,
        }

        # Remove None values except for fields that explicitly allow None
        values = {
            k: v
            for k, v in values.items()
            if v is not None
            or k
            in {
                "content",
                "interface_message_id",
                "turn_id",
                "thread_root_id",
                "tool_calls",
                "reasoning_info",
                "tool_call_id",
                "error_traceback",
                "processing_profile_id",
                "subconversation_id",
                "attachments",
                "tool_name",
                "provider_metadata",
                "user_id",
            }
        }

        try:
            stmt = (
                insert(message_history_table)
                .values(**values)
                .returning(message_history_table.c.internal_id)
            )
            result = await self._db.execute_with_retry(stmt)
            row = result.one()  # type: ignore[attr-defined]
            internal_id = row[0]

            self._logger.info(
                f"Added message to history: role={role}, "
                f"interface={interface_type}, internal_id={internal_id}"
            )

            # Notify listeners after transaction commits (if notifier available)
            if hasattr(self._db, "message_notifier"):
                notifier = getattr(self._db, "message_notifier", None)
                if notifier:
                    conv_id = conversation_id
                    iface_type = interface_type

                    def notify_listeners() -> None:
                        notifier.notify(conv_id, iface_type)

                    self._db.on_commit(notify_listeners)

            return internal_id

        except SQLAlchemyError as e:
            self._logger.error(f"Failed to add message to history: {e}", exc_info=True)
            return None

    async def get_recent(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        current_time: datetime | None = None,
    ) -> list[LLMMessage]:
        """
        Retrieves recent message history for a conversation.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return
            processing_profile_id: Filter by processing profile
            subconversation_id: Filter by subconversation ID
            current_time: Current time for calculating cutoff (defaults to now)

        Returns:
            List of typed LLMMessage objects in chronological order
        """
        now = current_time or datetime.now(UTC)
        cutoff = now - max_age if max_age else now - timedelta(hours=24)

        conditions = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp >= cutoff,
        ]

        if processing_profile_id:
            conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        # Filter by subconversation_id: None means main conversation only (IS NULL)
        if subconversation_id is None:
            conditions.append(message_history_table.c.subconversation_id.is_(None))
        else:
            conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        # First, order by timestamp descending to get the most recent messages
        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)

        # Reverse the results to return them in chronological order (oldest first)
        # This ensures we get the N most recent messages but present them chronologically
        rows.reverse()

        return [self._process_message_row(row) for row in rows]

    async def get_recent_with_metadata(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
    ) -> list[MessageHistoryRow]:
        """
        Retrieves recent messages with database metadata (timestamp, internal_id, etc.).

        This method returns the full database rows including both message content and
        metadata fields. Use this when you need access to timestamps or other metadata.
        For just message content, use get_recent() which returns typed LLMMessage objects.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return

        Returns:
            List of MessageHistoryRow with message content + metadata
        """
        if max_age:
            cutoff = datetime.now(UTC) - max_age
        else:
            cutoff = datetime.now(UTC) - timedelta(hours=24)

        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.interface_type == interface_type,
                message_history_table.c.conversation_id == conversation_id,
                message_history_table.c.timestamp >= cutoff,
            )
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)
        return [cast("MessageHistoryRow", dict(row)) for row in rows]

    async def get_recent_with_typed_metadata(
        self,
        interface_type: str,
        conversation_id: str,
        limit: int | None = None,
        max_age: timedelta | None = None,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
        thread_root_id: int | None = None,
    ) -> list[MessageWithMetadata]:
        """
        Retrieves recent messages with both typed content and database metadata.

        Use this when you need to filter by metadata fields (interface_message_id, timestamp)
        while maintaining type safety. For LLM context, use get_recent() instead.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return
            processing_profile_id: Filter by processing profile
            subconversation_id: Filter by subconversation ID
            thread_root_id: Filter by thread root ID

        Returns:
            List of MessageWithMetadata objects in chronological order
        """
        cutoff = datetime.now(UTC) - max_age if max_age else None

        query = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
        )

        if cutoff:
            query = query.where(message_history_table.c.timestamp >= cutoff)
        if processing_profile_id is not None:
            query = query.where(
                message_history_table.c.processing_profile_id == processing_profile_id
            )
        if subconversation_id is not None:
            query = query.where(
                message_history_table.c.subconversation_id == subconversation_id
            )
        if thread_root_id is not None:
            query = query.where(
                or_(
                    message_history_table.c.thread_root_id == thread_root_id,
                    message_history_table.c.internal_id == thread_root_id,
                )
            )
            self._logger.debug(
                f"Querying thread history for root {thread_root_id} with profile {processing_profile_id}"
            )

        query = query.order_by(
            message_history_table.c.timestamp.desc(),
            message_history_table.c.internal_id.desc(),
        )
        if limit:
            query = query.limit(limit)

        rows = await self._db.fetch_all(query)
        self._logger.debug(f"Fetched {len(rows)} rows for thread history query")
        rows = list(reversed(rows))  # Chronological order

        # Convert rows to MessageWithMetadata objects
        return [
            MessageWithMetadata(
                message=self._process_message_row(row),  # Returns typed LLMMessage
                internal_id=str(row["internal_id"]),
                interface_message_id=row["interface_message_id"],
                timestamp=row["timestamp"],
                conversation_id=row["conversation_id"],
                interface_type=row["interface_type"],
                user_id=row["user_id"],
                turn_id=row["turn_id"],
                thread_root_id=row["thread_root_id"],
            )
            for row in rows
        ]

    async def get_recent_tool_examples(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        subconversation_id: str | None,
        tool_name: str,
        limit: int,
        user_id: str | None = None,
    ) -> list[ToolHistoryExample]:
        """Return recent successful tool examples from the same conversation or user."""
        if limit <= 0:
            return []

        same_conversation_conditions: list[ColumnElement[bool]] = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
        ]
        if subconversation_id is None:
            same_conversation_conditions.append(
                message_history_table.c.subconversation_id.is_(None)
            )
        else:
            same_conversation_conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        examples = await self._fetch_tool_examples_for_scope(
            tool_name=tool_name,
            limit=limit,
            conditions=tuple(same_conversation_conditions),
        )
        if examples or user_id is None:
            return examples

        same_user_conditions: list[ColumnElement[bool]] = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.user_id == user_id,
        ]
        if subconversation_id is None:
            same_user_conditions.append(
                message_history_table.c.subconversation_id.is_(None)
            )
        else:
            same_user_conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        return await self._fetch_tool_examples_for_scope(
            tool_name=tool_name,
            limit=limit,
            conditions=tuple(same_user_conditions),
        )

    async def _fetch_tool_examples_for_scope(
        self,
        *,
        tool_name: str,
        limit: int,
        conditions: tuple[ColumnElement[bool], ...],
    ) -> list[ToolHistoryExample]:
        """Fetch reconstructed tool examples for a specific query scope."""
        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.role == "tool",
                message_history_table.c.tool_name == tool_name,
                message_history_table.c.error_traceback.is_(None),
                *conditions,
            )
            .order_by(
                message_history_table.c.timestamp.desc(),
                message_history_table.c.internal_id.desc(),
            )
            .limit(limit * 4)
        )
        tool_rows = await self._db.fetch_all(stmt)

        examples: list[ToolHistoryExample] = []
        for tool_row in tool_rows:
            tool_message = self._process_message_row_as_dict(tool_row)
            if not self._is_tool_message_usable_for_example(tool_message):
                continue

            tool_call_id = tool_message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            tool_message_internal_id = cast("int", tool_message["internal_id"])
            tool_message_timestamp = cast("datetime", tool_message["timestamp"])

            assistant_conditions: list[ColumnElement[bool]] = [
                message_history_table.c.role == "assistant",
                message_history_table.c.interface_type
                == tool_message["interface_type"],
                message_history_table.c.conversation_id
                == tool_message["conversation_id"],
                message_history_table.c.tool_calls.is_not(None),
                or_(
                    message_history_table.c.timestamp < tool_message_timestamp,
                    (message_history_table.c.timestamp == tool_message_timestamp)
                    & (message_history_table.c.internal_id < tool_message_internal_id),
                ),
            ]
            tool_message_subconversation_id = tool_message.get("subconversation_id")
            if tool_message_subconversation_id is None:
                assistant_conditions.append(
                    message_history_table.c.subconversation_id.is_(None)
                )
            else:
                assistant_conditions.append(
                    message_history_table.c.subconversation_id
                    == tool_message_subconversation_id
                )

            assistant_stmt = (
                select(message_history_table)
                .where(*assistant_conditions)
                .order_by(
                    message_history_table.c.timestamp.desc(),
                    message_history_table.c.internal_id.desc(),
                )
                .limit(_MAX_ASSISTANT_ROWS_PER_TOOL_EXAMPLE)
            )
            assistant_rows = await self._db.fetch_all(assistant_stmt)

            arguments = self._extract_tool_arguments_from_assistant_rows(
                assistant_rows=tuple(dict(row) for row in assistant_rows),
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
            if arguments is None:
                continue

            examples.append(
                ToolHistoryExample(
                    tool_name=tool_name,
                    arguments=arguments,
                    result_content=cast("str", tool_message["content"]),
                    conversation_id=cast("str", tool_message["conversation_id"]),
                    tool_call_id=tool_call_id,
                )
            )
            if len(examples) >= limit:
                break

        return examples

    @staticmethod
    def _is_tool_message_usable_for_example(
        tool_message: Mapping[str, Any],
    ) -> bool:
        """Return whether a persisted tool message is suitable for few-shot prompting."""
        content = tool_message.get("content")
        if not isinstance(content, str) or not content.strip():
            return False
        if content.startswith("Error:"):
            return False

        attachments = tool_message.get("attachments")
        if isinstance(attachments, list) and attachments:
            return False

        return len(content) <= 4000

    def _extract_tool_arguments_from_assistant_rows(
        self,
        *,
        assistant_rows: tuple[Mapping[str, Any], ...],
        tool_call_id: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - Reconstructed tool arguments are dynamic JSON keyed by tool schema
    ) -> dict[str, Any] | None:
        """Find tool arguments for a persisted tool call by scanning assistant tool_calls."""
        for assistant_row in assistant_rows:
            assistant_message = self._process_message_row_as_dict(assistant_row)
            tool_calls = assistant_message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue

            for tool_call in tool_calls:
                if not isinstance(tool_call, ToolCallItem):
                    continue
                if tool_call.id != tool_call_id or tool_call.function.name != tool_name:
                    continue

                raw_arguments = tool_call.function.arguments
                if isinstance(raw_arguments, dict):
                    return cast("dict[str, Any]", raw_arguments)
                if not isinstance(raw_arguments, str):
                    return None

                try:
                    parsed_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed_arguments, dict):
                    return cast("dict[str, Any]", parsed_arguments)
                return None

        return None

    async def get_by_interface_id(
        self,
        interface_type: str,
        interface_message_id: str,
    ) -> LLMMessage | None:
        """
        Retrieves a message by its interface-specific ID.

        Args:
            interface_type: Type of interface
            interface_message_id: Interface-specific message ID

        Returns:
            Typed LLMMessage or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.interface_message_id == interface_message_id,
        )

        row = await self._db.fetch_one(stmt)
        return self._process_message_row(row) if row else None

    async def get_row_by_interface_id(
        self,
        interface_type: str,
        interface_message_id: str,
    ) -> MessageHistoryRow | None:
        """
        Retrieves raw database row by interface-specific ID, including metadata.

        This method returns the full database row including metadata fields like
        internal_id, thread_root_id, and processing_profile_id that are not part
        of the LLMMessage content types.

        Args:
            interface_type: Type of interface
            interface_message_id: Interface-specific message ID

        Returns:
            Dict with all database fields including metadata, or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.interface_message_id == interface_message_id,
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def get_row_by_internal_id(
        self,
        internal_id: int,
    ) -> MessageHistoryRow | None:
        """
        Retrieves raw database row by internal database ID, including metadata.

        Args:
            internal_id: Internal database ID

        Returns:
            Dict with all database fields including metadata, or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.internal_id == internal_id,
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def get_user_row_by_turn_id(
        self,
        turn_id: str,
    ) -> MessageHistoryRow | None:
        """Retrieves the user message row that started a processing turn."""
        stmt = (
            select(message_history_table)
            .where(
                message_history_table.c.turn_id == turn_id,
                message_history_table.c.role == "user",
            )
            .order_by(message_history_table.c.internal_id.asc())
            .limit(1)
        )

        row = await self._db.fetch_one(stmt)
        return cast("MessageHistoryRow", dict(row)) if row else None

    async def get_interface_type_for_conversation(
        self, conversation_id: str
    ) -> str | None:
        """
        Get the interface_type for a conversation by checking message history.

        This is used to detect which interface (telegram, web, etc.) a conversation
        belongs to, enabling cross-interface message routing.

        Args:
            conversation_id: The conversation identifier

        Returns:
            The interface_type string (e.g., "telegram", "web") or None if not found
        """
        stmt = (
            select(message_history_table.c.interface_type)
            .where(message_history_table.c.conversation_id == conversation_id)
            .limit(1)
        )

        row = await self._db.fetch_one(stmt)
        return row["interface_type"] if row else None

    async def get_by_turn_id(self, turn_id: str) -> list[LLMMessage]:
        """
        Retrieves all messages for a specific turn.

        Args:
            turn_id: The turn identifier

        Returns:
            List of typed LLMMessage objects in the turn
        """
        stmt = (
            select(message_history_table)
            .where(message_history_table.c.turn_id == turn_id)
            .order_by(message_history_table.c.timestamp.asc())
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_by_thread_id(
        self,
        thread_root_id: int,
        processing_profile_id: str | None = None,
        subconversation_id: str | None = None,
    ) -> list[LLMMessage]:
        """
        Retrieves all messages in a thread.

        Args:
            thread_root_id: The root message ID of the thread
            processing_profile_id: Filter by processing profile

        Returns:
            List of typed LLMMessage objects in the thread, including the root message
        """
        # Include both the root message itself (where internal_id = thread_root_id)
        # and all child messages (where thread_root_id = thread_root_id)
        conditions = [
            or_(
                message_history_table.c.internal_id == thread_root_id,
                message_history_table.c.thread_root_id == thread_root_id,
            )
        ]

        if processing_profile_id:
            conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        # Filter by subconversation_id to maintain isolation
        if subconversation_id is None:
            conditions.append(message_history_table.c.subconversation_id.is_(None))
        else:
            conditions.append(
                message_history_table.c.subconversation_id == subconversation_id
            )

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def update_interface_id(
        self, internal_id: int, interface_message_id: str
    ) -> None:
        """
        Updates the interface message ID for a message.

        Args:
            internal_id: Internal database ID
            interface_message_id: New interface message ID
        """
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(interface_message_id=interface_message_id)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            self._logger.warning(
                f"No message found with internal_id {internal_id} to update interface ID"
            )

    async def update_error_traceback(
        self, internal_id: int, error_traceback: str
    ) -> None:
        """
        Updates the error traceback for a message.

        Args:
            internal_id: Internal database ID
            error_traceback: Error traceback to store
        """
        stmt = (
            update(message_history_table)
            .where(message_history_table.c.internal_id == internal_id)
            .values(error_traceback=error_traceback)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            self._logger.warning(
                f"No message found with internal_id {internal_id} to update error traceback"
            )

    async def get_conversation_messages_paginated(
        self,
        conversation_id: str,
        before: datetime | None = None,
        after: datetime | None = None,
        limit: int = 50,
    ) -> tuple[list[MessageHistoryRow], bool, bool]:
        """
        Get messages for a conversation with timestamp-based pagination.

        Args:
            conversation_id: The conversation identifier
            before: Get messages before this timestamp (for loading earlier)
            after: Get messages after this timestamp (for loading newer)
            limit: Maximum number of messages to return

        Returns:
            Tuple of (messages, has_more_before, has_more_after)
        """
        conditions = [message_history_table.c.conversation_id == conversation_id]

        # Add timestamp conditions
        if before:
            conditions.append(message_history_table.c.timestamp < before)
            order = message_history_table.c.timestamp.desc()
        elif after:
            conditions.append(message_history_table.c.timestamp > after)
            order = message_history_table.c.timestamp.asc()
        else:
            # Default: most recent messages
            order = message_history_table.c.timestamp.desc()

        # Fetch one extra message to determine if there are more
        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                order, message_history_table.c.internal_id
            )  # Add internal_id for stable sort
            .limit(limit + 1)
        )

        rows = await self._db.fetch_all(stmt)
        # Process rows to dicts with database fields preserved for API
        messages = [self._process_message_row_as_dict(row) for row in rows]

        # Check if we have more messages
        has_more = len(messages) > limit
        if has_more:
            messages = messages[:limit]

        # If we fetched in DESC order (before or default), reverse for chronological display
        if before or not after:
            messages.reverse()

        # Determine has_more_before and has_more_after flags
        if before:
            has_more_before = has_more
            # If we're loading "before", there are newer messages only if we found any messages
            has_more_after = len(messages) > 0
        elif after:
            # Check if there are actually messages before the 'after' timestamp
            check_before_stmt = (
                select(message_history_table.c.internal_id)
                .where(
                    message_history_table.c.conversation_id == conversation_id,
                    message_history_table.c.timestamp < after,
                )
                .limit(1)
            )
            before_rows = await self._db.fetch_all(check_before_stmt)
            has_more_before = len(before_rows) > 0
            has_more_after = has_more
        else:
            # Default case: loading most recent
            has_more_before = has_more
            has_more_after = False

        return messages, has_more_before, has_more_after

    async def get_conversation_message_count(self, conversation_id: str) -> int:
        """
        Get the total number of messages in a conversation.

        Args:
            conversation_id: The conversation identifier

        Returns:
            Total number of messages in the conversation
        """
        stmt = select(
            func.count(message_history_table.c.internal_id).label("count")
        ).where(message_history_table.c.conversation_id == conversation_id)

        row = await self._db.fetch_one(stmt)
        return row["count"] if row else 0

    async def get_messages_after(
        self,
        conversation_id: str,
        after: datetime,
        interface_type: str | None = None,
        limit: int = 100,
    ) -> list[LLMMessage]:
        """
        Get messages created after a specific timestamp.

        Used for incremental sync in SSE and catch-up scenarios.

        Args:
            conversation_id: The conversation identifier
            after: Get messages created after this timestamp
            interface_type: Optional filter by interface type
            limit: Maximum number of messages to return (default 100)

        Returns:
            List of typed LLMMessage objects in chronological order (oldest first)
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp > after,
        ]

        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_messages_after_as_dict(
        self,
        conversation_id: str,
        after: datetime,
        interface_type: str | None = None,
        limit: int = 100,
    ) -> list[MessageHistoryRow]:
        """
        Get messages created after a specific timestamp as dicts with database fields.

        Used for SSE endpoints that need to send database metadata to the frontend.

        Args:
            conversation_id: The conversation identifier
            after: Get messages created after this timestamp
            interface_type: Optional filter by interface type
            limit: Maximum number of messages to return (default 100)

        Returns:
            List of MessageHistoryRow in chronological order (oldest first)
        """
        conditions = [
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp > after,
        ]

        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(
                message_history_table.c.timestamp.asc(),
                message_history_table.c.internal_id.asc(),
            )
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row_as_dict(row) for row in rows]

    def _process_message_row(self, row: Mapping[str, Any]) -> LLMMessage:
        """
        Process a message row from the database with proper type deserialization.

        Deserializes JSON fields and converts provider-specific metadata
        to proper typed objects for type safety. Returns a dict that can be
        used with database fields and converted to LLMMessage when needed.

        Args:
            row: Raw database row (Mapping from SQLAlchemy fetch_all/fetch_one)

        Returns:
            Dictionary with properly deserialized complex types
        """
        # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row, progressively deserialized to typed LLMMessage
        msg: dict[str, Any] = dict(row)

        # Handle tool_calls deserialization: JSON columns return dicts/lists directly
        if isinstance(msg.get("tool_calls"), str):
            try:
                msg["tool_calls"] = json.loads(cast("str", msg["tool_calls"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse tool_calls JSON for message {msg.get('internal_id')}"
                )
                msg["tool_calls"] = None

        # Deserialize provider_metadata within each tool call to typed objects
        if msg.get("tool_calls") and isinstance(msg["tool_calls"], list):
            tool_call_items = []
            for tc_dict in msg["tool_calls"]:
                if isinstance(tc_dict, ToolCallItem):
                    # Already a ToolCallItem, keep it as-is
                    tool_call_items.append(tc_dict)
                elif isinstance(tc_dict, dict):
                    # Deserialize provider_metadata if present
                    provider_metadata = tc_dict.get("provider_metadata")
                    if (
                        isinstance(provider_metadata, dict)
                        and provider_metadata.get("provider") == "google"
                    ):
                        provider_metadata = GeminiProviderMetadata.from_dict(
                            provider_metadata
                        )

                    # Create ToolCallItem with typed provider_metadata
                    tool_call_items.append(
                        ToolCallItem(
                            id=tc_dict["id"],
                            type=tc_dict["type"],
                            function=ToolCallFunction(
                                name=tc_dict["function"]["name"],
                                arguments=tc_dict["function"]["arguments"],
                            ),
                            provider_metadata=provider_metadata,
                        )
                    )
                else:
                    self._logger.warning(
                        f"Unexpected tool_call type: {type(tc_dict)}, skipping"
                    )
            msg["tool_calls"] = tool_call_items

        # Handle reasoning_info deserialization
        if isinstance(msg.get("reasoning_info"), str):
            try:
                msg["reasoning_info"] = json.loads(cast("str", msg["reasoning_info"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse reasoning_info JSON for message {msg.get('internal_id')}"
                )
                msg["reasoning_info"] = None

        # Handle attachments deserialization
        if isinstance(msg.get("attachments"), str):
            try:
                msg["attachments"] = json.loads(cast("str", msg["attachments"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse attachments JSON for message {msg.get('internal_id')}"
                )
                msg["attachments"] = None

        # Handle provider_metadata deserialization at message level
        provider_metadata = msg.get("provider_metadata")
        if provider_metadata:
            if isinstance(provider_metadata, str):
                # String case: parse JSON first
                try:
                    provider_metadata = json.loads(provider_metadata)
                except json.JSONDecodeError:
                    self._logger.warning(
                        f"Failed to parse provider_metadata JSON for message {msg.get('internal_id')}"
                    )
                    provider_metadata = None

            # Convert Google provider metadata to typed object for in-app use
            if (
                isinstance(provider_metadata, dict)
                and provider_metadata.get("provider") == "google"
            ):
                provider_metadata = GeminiProviderMetadata.from_dict(provider_metadata)

            msg["provider_metadata"] = provider_metadata

        # Convert dict to typed LLMMessage
        return self._dict_to_typed_message(msg)

    def _process_message_row_as_dict(self, row: Mapping[str, Any]) -> MessageHistoryRow:
        """
        Process a message row and return as dict with all database fields preserved.

        This method deserializes complex types (tool_calls, provider_metadata) while
        keeping all database fields (internal_id, timestamp, user_id, etc.).
        Use this for API endpoints that need complete message data.

        Args:
            row: Raw database row (Mapping from SQLAlchemy fetch_all/fetch_one)

        Returns:
            Dictionary with deserialized complex types and all database fields
        """
        # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row before casting to MessageHistoryRow
        msg: dict[str, Any] = dict(row)

        # Handle tool_calls deserialization: JSON columns return dicts/lists directly
        if isinstance(msg.get("tool_calls"), str):
            try:
                msg["tool_calls"] = json.loads(cast("str", msg["tool_calls"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse tool_calls JSON for message {msg.get('internal_id')}"
                )
                msg["tool_calls"] = None

        # Deserialize provider_metadata within each tool call to typed objects
        if msg.get("tool_calls") and isinstance(msg["tool_calls"], list):
            tool_call_items = []
            for tc_dict in msg["tool_calls"]:
                if isinstance(tc_dict, ToolCallItem):
                    # Already a ToolCallItem, keep it as-is
                    tool_call_items.append(tc_dict)
                elif isinstance(tc_dict, dict):
                    # Deserialize provider_metadata if present
                    provider_metadata = tc_dict.get("provider_metadata")
                    if (
                        isinstance(provider_metadata, dict)
                        and provider_metadata.get("provider") == "google"
                    ):
                        provider_metadata = GeminiProviderMetadata.from_dict(
                            provider_metadata
                        )

                    # Create ToolCallItem with typed provider_metadata
                    tool_call_items.append(
                        ToolCallItem(
                            id=tc_dict["id"],
                            type=tc_dict["type"],
                            function=ToolCallFunction(
                                name=tc_dict["function"]["name"],
                                arguments=tc_dict["function"]["arguments"],
                            ),
                            provider_metadata=provider_metadata,
                        )
                    )
                else:
                    self._logger.warning(
                        f"Unexpected tool_call type: {type(tc_dict)}, skipping"
                    )
            msg["tool_calls"] = tool_call_items

        # Handle reasoning_info deserialization
        if isinstance(msg.get("reasoning_info"), str):
            try:
                msg["reasoning_info"] = json.loads(cast("str", msg["reasoning_info"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse reasoning_info JSON for message {msg.get('internal_id')}"
                )
                msg["reasoning_info"] = None

        # Handle attachments deserialization
        if isinstance(msg.get("attachments"), str):
            try:
                msg["attachments"] = json.loads(cast("str", msg["attachments"]))
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse attachments JSON for message {msg.get('internal_id')}"
                )
                msg["attachments"] = None

        # Handle provider_metadata deserialization at message level
        provider_metadata = msg.get("provider_metadata")
        if provider_metadata:
            if isinstance(provider_metadata, str):
                # String case: parse JSON first
                try:
                    provider_metadata = json.loads(provider_metadata)
                except json.JSONDecodeError:
                    self._logger.warning(
                        f"Failed to parse provider_metadata JSON for message {msg.get('internal_id')}"
                    )
                    provider_metadata = None

            # Keep message-level provider_metadata as dict for now
            # (it has a different structure than tool-call-level provider_metadata)
            msg["provider_metadata"] = provider_metadata

        return cast("MessageHistoryRow", msg)

    # ast-grep-ignore: no-dict-any - intermediate dict from raw SQLAlchemy row, typed fields accessed by key
    def _dict_to_typed_message(self, msg: dict[str, Any]) -> LLMMessage:
        """
        Convert a processed message dict to a proper typed LLMMessage object.

        The dict should have been processed by _process_message_row to ensure
        proper deserialization of complex types (tool_calls as ToolCallItem objects,
        provider_metadata as typed objects, etc.).

        Args:
            msg: Dictionary with deserialized message data

        Returns:
            Appropriate typed LLMMessage (UserMessage, AssistantMessage, ToolMessage,
            SystemMessage, or ErrorMessage)

        Raises:
            ValueError: If role is unknown
        """
        role = msg.get("role")

        if role == "user":
            text_content_str = msg.get("content") or ""
            attachments = msg.get("attachments")

            # Reconstruct multimodal content from attachments if present
            # Attachments with content_url that are images/video/audio/PDF should be
            # included as content parts for the LLM
            if attachments and isinstance(attachments, list):
                multimodal_attachments = [
                    att
                    for att in attachments
                    if att.get("content_url")
                    and (
                        att.get("type") in {"image", "video", "audio", "document"}
                        or att.get("content_type") == "application/pdf"
                        or att.get("mime_type") == "application/pdf"
                    )
                ]

                if multimodal_attachments:
                    # Build multimodal content: text first, then attachment URLs
                    content_parts: list[ContentPart] = []
                    if text_content_str:
                        content_parts.append(
                            TextContentPart(type="text", text=text_content_str)
                        )
                    for att in multimodal_attachments:
                        content_parts.append(
                            ImageUrlContentPart(
                                type="image_url",
                                image_url={"url": att["content_url"]},
                            )
                        )

                    return UserMessage(content=content_parts)

            # No multimodal attachments - return simple text content
            return UserMessage(content=text_content_str)
        elif role == "assistant":
            return AssistantMessage(
                content=msg.get("content"),
                tool_calls=msg.get("tool_calls"),
                provider_metadata=msg.get("provider_metadata"),
            )
        elif role == "tool":
            return ToolMessage(
                tool_call_id=msg.get("tool_call_id") or "",
                content=msg.get("content") or "",
                name=msg.get("tool_name") or "",
                error_traceback=msg.get("error_traceback"),
            )
        elif role == "system":
            return SystemMessage(
                content=msg.get("content") or "",
            )
        elif role == "error":
            return ErrorMessage(
                content=msg.get("content") or "",
                error_traceback=msg.get("error_traceback"),
            )
        else:
            raise ValueError(f"Unknown message role: {role}")

    async def get_all_grouped(
        self,
        interface_type: str | None = None,
        conversation_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict[tuple[str, str], list[MessageHistoryRow]]:
        """
        Retrieves all message history, grouped by (interface_type, conversation_id) and ordered by timestamp.

        Args:
            interface_type: Filter by interface type
            conversation_id: Filter by conversation ID
            date_from: Filter messages after this date (inclusive)
            date_to: Filter messages before this date (inclusive)

        Returns:
            Dictionary mapping (interface_type, conversation_id) tuples to lists of messages
        """
        # Build query conditions
        conditions = []
        if interface_type:
            conditions.append(message_history_table.c.interface_type == interface_type)
        if conversation_id:
            conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )
        if date_from:
            conditions.append(message_history_table.c.timestamp >= date_from)
        if date_to:
            conditions.append(message_history_table.c.timestamp <= date_to)

        stmt = select(message_history_table)
        if conditions:
            stmt = stmt.where(*conditions)

        stmt = stmt.order_by(
            message_history_table.c.interface_type,
            message_history_table.c.conversation_id,
            message_history_table.c.timestamp,
            message_history_table.c.internal_id,  # For stable chronological order
        )

        rows = await self._db.fetch_all(stmt)

        grouped_history: dict[tuple[str, str], list[MessageHistoryRow]] = {}

        for row in rows:
            msg = self._process_message_row_as_dict(row)
            key = (row["interface_type"], row["conversation_id"])

            if key not in grouped_history:
                grouped_history[key] = []
            grouped_history[key].append(msg)

        return grouped_history

    async def get_conversation_summaries(
        self,
        interface_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
        conversation_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[ConversationSummaryRow], int]:
        """
        Get conversation summaries with pagination, optimized for performance.

        Args:
            interface_type: Filter by interface type (None for all interfaces)
            limit: Maximum number of conversations to return
            offset: Number of conversations to skip for pagination
            conversation_id: Filter by specific conversation ID
            date_from: Filter conversations with messages after this date
            date_to: Filter conversations with messages before this date

        Returns:
            Tuple of (summaries list, total count)
        """
        # Build base conditions
        base_conditions = []
        base_conditions.append(message_history_table.c.role.in_(["user", "assistant"]))
        base_conditions.append(message_history_table.c.content.isnot(None))

        if interface_type:
            base_conditions.append(
                message_history_table.c.interface_type == interface_type
            )

        if conversation_id:
            base_conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )

        if date_from:
            base_conditions.append(message_history_table.c.timestamp >= date_from)

        if date_to:
            base_conditions.append(message_history_table.c.timestamp <= date_to)

        # Subquery to get the latest message id and count per conversation
        # We get the max internal_id within the max timestamp to handle timestamp collisions
        latest_msg_subq = (
            select(
                message_history_table.c.conversation_id,
                func.max(message_history_table.c.timestamp).label("max_timestamp"),
            )
            .where(*base_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Get the max internal_id for messages with the latest timestamp
        latest_id_subq = (
            select(
                message_history_table.c.conversation_id,
                func.max(message_history_table.c.internal_id).label("max_id"),
            )
            .join(
                latest_msg_subq,
                (
                    message_history_table.c.conversation_id
                    == latest_msg_subq.c.conversation_id
                )
                & (
                    message_history_table.c.timestamp == latest_msg_subq.c.max_timestamp
                ),
            )
            .where(*base_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Get message counts per conversation (without content filter)
        count_conditions = []
        count_conditions.append(message_history_table.c.role.in_(["user", "assistant"]))

        if interface_type:
            count_conditions.append(
                message_history_table.c.interface_type == interface_type
            )

        if conversation_id:
            count_conditions.append(
                message_history_table.c.conversation_id == conversation_id
            )

        if date_from:
            count_conditions.append(message_history_table.c.timestamp >= date_from)

        if date_to:
            count_conditions.append(message_history_table.c.timestamp <= date_to)

        msg_count_subq = (
            select(
                message_history_table.c.conversation_id,
                func.count(message_history_table.c.internal_id).label("msg_count"),
            )
            .where(*count_conditions)
            .group_by(message_history_table.c.conversation_id)
            .subquery()
        )

        # Main query to get conversation summaries with the latest message content
        summaries_query = (
            select(
                message_history_table.c.conversation_id,
                message_history_table.c.content,
                message_history_table.c.timestamp,
                message_history_table.c.interface_type,  # Include interface_type in results
                msg_count_subq.c.msg_count.label("message_count"),
            )
            .join(
                latest_id_subq,
                message_history_table.c.internal_id == latest_id_subq.c.max_id,
            )
            .join(
                msg_count_subq,
                message_history_table.c.conversation_id
                == msg_count_subq.c.conversation_id,
            )
            .where(
                message_history_table.c.content.isnot(None),
            )
            .order_by(message_history_table.c.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )

        # Count query - count conversations that have messages with content
        count_subquery = (
            select(message_history_table.c.conversation_id)
            .where(*base_conditions)
            .distinct()
            .subquery()
        )
        count_query = select(func.count().label("count")).select_from(count_subquery)

        # Execute queries
        summaries_rows = await self._db.fetch_all(summaries_query)
        count_row = await self._db.fetch_one(count_query)
        total_count = count_row["count"] if count_row else 0

        # Process results
        summaries: list[ConversationSummaryRow] = []
        for row in summaries_rows:
            summaries.append(
                ConversationSummaryRow(
                    conversation_id=row["conversation_id"],
                    last_message=row["content"][:100] if row["content"] else "",
                    last_timestamp=row["timestamp"],
                    message_count=row["message_count"],
                    interface_type=row["interface_type"],
                )
            )

        return summaries, total_count
