"""Repository for message history storage operations."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.base import BaseRepository


class MessageHistoryRepository(BaseRepository):
    """Repository for managing message history in the database."""

    async def add(self, **kwargs: Any) -> dict[str, Any] | None:
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

        Returns:
            The stored message data including generated internal_id, or None on error
        """
        # Ensure timestamp is timezone-aware
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must be timezone-aware")

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
        }

        # Remove None values except for fields that explicitly allow None
        values = {
            k: v
            for k, v in values.items()
            if v is not None
            or k
            in [
                "content",
                "interface_message_id",
                "turn_id",
                "thread_root_id",
                "tool_calls",
                "reasoning_info",
                "tool_call_id",
                "error_traceback",
                "processing_profile_id",
            ]
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

        stmt = (
            select(message_history_table)
            .where(*conditions)
            .order_by(message_history_table.c.timestamp.asc())
        )

        if limit:
            stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)
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
            List of messages in the thread
        """
        conditions = [message_history_table.c.thread_root_id == thread_root_id]

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
