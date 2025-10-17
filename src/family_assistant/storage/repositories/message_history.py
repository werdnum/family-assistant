"""Repository for message history storage operations."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import functions as func

from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class MessageHistoryRepository(BaseRepository):
    """Repository for managing message history in the database."""

    async def add(self, **kwargs: Any) -> dict[str, Any] | None:  # noqa: ANN401 # Forwards arbitrary message args
        """Alias for add_message for backward compatibility."""
        return await self.add_message(**kwargs)

    async def add_message(
        self,
        # --- New/Renamed Parameters ---
        interface_type: str,
        conversation_id: str,
        interface_message_id: str | None,  # Can be None for agent-generated messages
        turn_id: str | None,
        thread_root_id: int | None,  # Added thread_root_id
        timestamp: datetime,
        role: str,  # 'user', 'assistant', 'system', 'tool', 'error'
        content: str | None,  # Content can be optional now
        # --- Renamed/Added Fields ---
        tool_calls: list[dict[str, Any]] | None = None,  # Renamed from tool_calls_info
        reasoning_info: dict[str, Any] | None = None,  # Added
        # Note: `tool_call_id` is now a separate parameter below for 'tool' role messages
        error_traceback: str | None = None,  # Added
        tool_call_id: (
            str | None
        ) = None,  # Added: ID linking tool response to assistant request
        processing_profile_id: str | None = None,  # Added: Profile ID
        user_id: str | None = None,  # Added: User identifier
        attachments: list[dict[str, Any]] | None = None,  # Attachment metadata
        tool_name: str | None = None,  # Added: Function/tool name for tool messages
        name: str | None = None,  # OpenAI API compatibility (mapped to tool_name)
        provider_metadata: dict[str, Any]
        | None = None,  # Added: Provider-specific metadata for round-trip
    ) -> dict[str, Any] | None:  # Changed to return Optional[Dict]
        """
        Stores a message in the history table.

        Args:
            interface_type: Type of interface (e.g., 'telegram', 'web')
            conversation_id: Unique conversation identifier
            interface_message_id: Interface-specific message ID
            turn_id: UUID linking messages within a turn
            thread_root_id: ID of the first message in the thread
            timestamp: Message timestamp
            role: Message role
            content: Message content
            tool_calls: Tool calls made by assistant
            reasoning_info: LLM reasoning/usage info
            error_traceback: Error traceback if applicable
            tool_call_id: ID linking tool response to request
            processing_profile_id: Processing profile used
            attachments: Attachment metadata list
            tool_name: Function/tool name for tool messages (required for OpenAI API compatibility)
            name: Function/tool name (alias for tool_name, for OpenAI API compatibility).
                  If both 'name' and 'tool_name' are provided, 'tool_name' takes precedence.
                  If only 'name' is provided, it will be mapped to 'tool_name' for storage.
            provider_metadata: Provider-specific metadata for round-trip (e.g., thought signatures)

        Returns:
            The stored message data including generated internal_id, or None on error
        """
        # Ensure timestamp is timezone-aware
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")

        # Handle mapping from 'name' to 'tool_name' for OpenAI API compatibility
        if name is not None and tool_name is None:
            tool_name = name
        elif name is not None and tool_name is not None and name != tool_name:
            # Log warning if both provided but different
            logger.warning(
                f"Both 'name' and 'tool_name' provided with different values: name='{name}', tool_name='{tool_name}'. Using tool_name."
            )

        values = {
            "interface_type": interface_type,
            "conversation_id": conversation_id,
            "interface_message_id": interface_message_id,
            "turn_id": turn_id,
            "thread_root_id": thread_root_id,
            "timestamp": timestamp,
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "reasoning_info": reasoning_info,
            "tool_call_id": tool_call_id,
            "error_traceback": error_traceback,
            "processing_profile_id": processing_profile_id,
            "attachments": attachments,
            "tool_name": tool_name,
            "provider_metadata": provider_metadata,
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
                "attachments",
                "tool_name",
                "provider_metadata",
                "user_id",
            }
        }

        try:
            # Use RETURNING to get the generated internal_id
            if self._db.engine.dialect.name == "postgresql":
                stmt = (
                    insert(message_history_table)
                    .values(**values)
                    .returning(message_history_table.c.internal_id)
                )
                result = await self._db.execute_with_retry(stmt)
                row = result.one()  # type: ignore[attr-defined]
                internal_id = row[0]
            else:
                # SQLite: Insert and then get lastrowid
                stmt = insert(message_history_table).values(**values)
                result = await self._db.execute_with_retry(stmt)
                internal_id = result.lastrowid  # type: ignore[attr-defined]

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

            # Return the complete message data
            return {**values, "internal_id": internal_id}

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
    ) -> list[dict[str, Any]]:
        """
        Retrieves recent message history for a conversation.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            limit: Maximum number of messages to return
            max_age: Maximum age of messages to return
            processing_profile_id: Filter by processing profile

        Returns:
            List of messages in chronological order
        """
        if max_age:
            cutoff = datetime.now(timezone.utc) - max_age
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        conditions = [
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.conversation_id == conversation_id,
            message_history_table.c.timestamp >= cutoff,
        ]

        if processing_profile_id:
            conditions.append(
                message_history_table.c.processing_profile_id == processing_profile_id
            )

        # First, order by timestamp descending to get the most recent messages
        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(message_history_table.c.timestamp.desc())
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)

        # Reverse the results to return them in chronological order (oldest first)
        # This ensures we get the N most recent messages but present them chronologically
        rows.reverse()

        return [self._process_message_row(row) for row in rows]

    async def get_grouped(
        self,
        interface_type: str,
        conversation_id: str,
        hours: int = 24,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieves and groups message history by turn_id.

        Args:
            interface_type: Type of interface
            conversation_id: Conversation identifier
            hours: How many hours back to look
            limit: Maximum number of turns to return

        Returns:
            List of grouped message turns
        """
        # Get recent messages
        messages = await self.get_recent(interface_type, conversation_id, hours)

        # Group messages by turn_id
        grouped_messages = []
        current_turn = None
        current_turn_messages = []

        for msg in messages:
            msg_turn_id = msg.get("turn_id")

            if msg_turn_id != current_turn:
                # Save previous turn if exists
                if current_turn_messages:
                    grouped_messages.append({
                        "turn_id": current_turn,
                        "messages": current_turn_messages,
                    })

                # Start new turn
                current_turn = msg_turn_id
                current_turn_messages = [msg]
            else:
                current_turn_messages.append(msg)

        # Don't forget the last turn
        if current_turn_messages:
            grouped_messages.append({
                "turn_id": current_turn,
                "messages": current_turn_messages,
            })

        # Apply limit if specified
        if limit and len(grouped_messages) > limit:
            grouped_messages = grouped_messages[-limit:]

        return grouped_messages

    async def get_by_interface_id(
        self, interface_type: str, interface_message_id: str
    ) -> dict[str, Any] | None:
        """
        Retrieves a message by its interface-specific ID.

        Args:
            interface_type: Type of interface
            interface_message_id: Interface-specific message ID

        Returns:
            Message data or None if not found
        """
        stmt = select(message_history_table).where(
            message_history_table.c.interface_type == interface_type,
            message_history_table.c.interface_message_id == interface_message_id,
        )

        row = await self._db.fetch_one(stmt)
        return self._process_message_row(row) if row else None

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

    async def get_by_turn_id(self, turn_id: str) -> list[dict[str, Any]]:
        """
        Retrieves all messages for a specific turn.

        Args:
            turn_id: The turn identifier

        Returns:
            List of messages in the turn
        """
        stmt = (
            select(message_history_table)
            .where(message_history_table.c.turn_id == turn_id)
            .order_by(message_history_table.c.timestamp.asc())
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    async def get_by_thread_id(
        self, thread_root_id: int, processing_profile_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Retrieves all messages in a thread.

        Args:
            thread_root_id: The root message ID of the thread
            processing_profile_id: Filter by processing profile

        Returns:
            List of messages in the thread, including the root message
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

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(message_history_table.c.timestamp.asc())
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
    ) -> tuple[list[dict[str, Any]], bool, bool]:
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
        messages = [self._process_message_row(row) for row in rows]

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
    ) -> list[dict[str, Any]]:
        """
        Get messages created after a specific timestamp.

        Used for incremental sync in SSE and catch-up scenarios.

        Args:
            conversation_id: The conversation identifier
            after: Get messages created after this timestamp
            interface_type: Optional filter by interface type
            limit: Maximum number of messages to return (default 100)

        Returns:
            List of messages in chronological order (oldest first)
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
            .order_by(message_history_table.c.timestamp.asc())
            .limit(limit)
        )

        rows = await self._db.fetch_all(stmt)
        return [self._process_message_row(row) for row in rows]

    def _process_message_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """
        Process a message row from the database.

        Args:
            row: Database row

        Returns:
            Processed message dictionary
        """
        msg = dict(row)

        # Handle JSON fields that might be stored as strings
        if isinstance(msg.get("tool_calls"), str):
            try:
                msg["tool_calls"] = json.loads(msg["tool_calls"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse tool_calls JSON for message {msg.get('internal_id')}"
                )
                msg["tool_calls"] = None

        if isinstance(msg.get("reasoning_info"), str):
            try:
                msg["reasoning_info"] = json.loads(msg["reasoning_info"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse reasoning_info JSON for message {msg.get('internal_id')}"
                )
                msg["reasoning_info"] = None

        if isinstance(msg.get("attachments"), str):
            try:
                msg["attachments"] = json.loads(msg["attachments"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse attachments JSON for message {msg.get('internal_id')}"
                )
                msg["attachments"] = None

        if isinstance(msg.get("provider_metadata"), str):
            try:
                msg["provider_metadata"] = json.loads(msg["provider_metadata"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse provider_metadata JSON for message {msg.get('internal_id')}"
                )
                msg["provider_metadata"] = None

        return msg

    async def get_all_grouped(
        self,
        interface_type: str | None = None,
        conversation_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
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

        # Group messages by (interface_type, conversation_id)
        grouped_history: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for row in rows:
            msg = self._process_message_row(row)
            key = (msg["interface_type"], msg["conversation_id"])

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
    ) -> tuple[list[dict[str, Any]], int]:
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
        summaries = []
        for row in summaries_rows:
            summaries.append({
                "conversation_id": row["conversation_id"],
                "last_message": row["content"][:100] if row["content"] else "",
                "last_timestamp": row["timestamp"],
                "message_count": row["message_count"],
                "interface_type": row["interface_type"],  # Include interface_type
            })

        return summaries, total_count
