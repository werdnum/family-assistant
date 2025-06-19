"""Repository for events storage operations."""

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from family_assistant.storage.events import (
    EventActionType,
    EventSourceType,
    event_listeners_table,
    recent_events_table,
)
from family_assistant.storage.repositories.base import BaseRepository


class EventsRepository(BaseRepository):
    """Repository for managing events and rate limiting in the database."""

    async def check_and_update_rate_limit(
        self,
        listener_id: int,
        conversation_id: str,
    ) -> tuple[bool, str | None]:
        """
        Check rate limit and update counter atomically.

        Args:
            listener_id: ID of the event listener
            conversation_id: Conversation ID for verification

        Returns:
            Tuple of (is_allowed, error_message)
        """
        try:
            from datetime import timezone

            now = datetime.now(timezone.utc)

            # Get current listener state
            listener = await self.get_event_listener_by_id(listener_id)
            if not listener:
                return False, "Listener not found"

            # Verify conversation_id matches
            if listener.get("conversation_id") != conversation_id:
                return False, "Listener not found"

            # Check if we need to reset daily counter
            daily_reset_at = listener["daily_reset_at"]
            # Handle SQLite returning naive datetimes
            if daily_reset_at and daily_reset_at.tzinfo is None:
                daily_reset_at = daily_reset_at.replace(tzinfo=timezone.utc)

            if not daily_reset_at or now > daily_reset_at:
                # Reset counter for new day
                tomorrow = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
                stmt = (
                    update(event_listeners_table)
                    .where(event_listeners_table.c.id == listener_id)
                    .values(
                        daily_executions=1,
                        daily_reset_at=tomorrow,
                        last_execution_at=now,
                    )
                )
                await self._db.execute_with_retry(stmt)
                return True, None

            # Check if under limit
            if listener["daily_executions"] >= 5:
                return (
                    False,
                    f"Daily limit exceeded ({listener['daily_executions']} triggers today)",
                )

            # Increment counter
            stmt = (
                update(event_listeners_table)
                .where(event_listeners_table.c.id == listener_id)
                .values(
                    daily_executions=event_listeners_table.c.daily_executions + 1,
                    last_execution_at=now,
                )
            )
            await self._db.execute_with_retry(stmt)

            return True, None

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in check_and_update_rate_limit({listener_id}): {e}",
                exc_info=True,
            )
            # On error, allow execution but log it
            return True, None

    async def create_event_listener(
        self,
        name: str,
        source_id: str,
        match_conditions: dict,
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
        action_type: EventActionType = EventActionType.wake_llm,
        action_config: dict | None = None,
        one_time: bool = False,
        enabled: bool = True,
    ) -> int:
        """
        Create a new event listener.

        Args:
            name: Listener name
            source_id: Type of event source
            match_conditions: Conditions to match for triggering
            conversation_id: Conversation ID this listener belongs to
            interface_type: Interface type (telegram, web, email)
            description: Optional description
            action_type: Type of action to trigger (wake_llm or script)
            action_config: Configuration for the action
            one_time: If true, listener is disabled after first trigger
            enabled: Whether the listener is enabled

        Returns:
            ID of the created listener
        """
        from datetime import timezone

        from sqlalchemy.exc import IntegrityError

        try:
            stmt = (
                insert(event_listeners_table)
                .values(
                    name=name,
                    description=description,
                    source_id=source_id,
                    match_conditions=match_conditions,
                    action_type=action_type,
                    action_config=action_config,
                    conversation_id=conversation_id,
                    interface_type=interface_type,
                    one_time=one_time,
                    enabled=enabled,
                    created_at=datetime.now(timezone.utc),
                    daily_executions=0,
                )
                .returning(event_listeners_table.c.id)
            )

            result = await self._db.execute_with_retry(stmt)
            listener_id = result.scalar_one()

            self._logger.info(
                f"Created event listener '{name}' (ID: {listener_id}) for conversation {conversation_id}"
            )
            return listener_id

        except IntegrityError as e:
            if "uq_name_conversation" in str(e):
                self._logger.error(
                    f"Event listener with name '{name}' already exists for conversation {conversation_id}"
                )
                raise ValueError(
                    f"An event listener named '{name}' already exists in this conversation"
                ) from e
            raise
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in create_event_listener: {e}", exc_info=True
            )
            raise

    async def get_event_listeners(
        self,
        conversation_id: str,
        source_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get event listeners for a conversation with optional filters.

        Args:
            conversation_id: Conversation ID
            source_id: Filter by event source ID
            enabled: Filter by enabled status

        Returns:
            List of event listener dictionaries
        """
        # Start with base query filtered by conversation_id
        stmt = select(event_listeners_table).where(
            event_listeners_table.c.conversation_id == conversation_id
        )

        # Apply optional filters
        if source_id is not None:
            stmt = stmt.where(event_listeners_table.c.source_id == source_id)
        if enabled is not None:
            stmt = stmt.where(event_listeners_table.c.enabled == enabled)

        # Order by creation date, newest first
        stmt = stmt.order_by(event_listeners_table.c.created_at.desc())

        rows = await self._db.fetch_all(stmt)

        # Convert rows to dicts
        listeners = []
        for row in rows:
            listener = dict(row)
            listeners.append(listener)

        return listeners

    async def get_event_listener_by_id(
        self, listener_id: int, conversation_id: str | None = None
    ) -> dict[str, Any] | None:
        """
        Get a specific listener, optionally ensuring it belongs to the conversation.

        Args:
            listener_id: ID of the listener
            conversation_id: Optional conversation ID for verification

        Returns:
            Listener dict or None if not found
        """
        if conversation_id:
            stmt = select(event_listeners_table).where(
                (event_listeners_table.c.id == listener_id)
                & (event_listeners_table.c.conversation_id == conversation_id)
            )
        else:
            stmt = select(event_listeners_table).where(
                event_listeners_table.c.id == listener_id
            )

        row = await self._db.fetch_one(stmt)
        if not row:
            return None

        # Convert to dict
        listener = dict(row)
        return listener

    async def update_event_listener_enabled(
        self,
        listener_id: int,
        conversation_id: str,
        enabled: bool,
    ) -> bool:
        """
        Toggle listener enabled status.

        Args:
            listener_id: ID of the listener
            conversation_id: Conversation ID for verification
            enabled: New enabled status

        Returns:
            True if updated, False if not found
        """
        stmt = (
            update(event_listeners_table)
            .where(
                (event_listeners_table.c.id == listener_id)
                & (event_listeners_table.c.conversation_id == conversation_id)
            )
            .values(enabled=enabled)
        )

        result = await self._db.execute_with_retry(stmt)
        updated_count = result.rowcount  # type: ignore[attr-defined]

        if updated_count > 0:
            status = "enabled" if enabled else "disabled"
            self._logger.info(f"Updated event listener {listener_id} to {status}")
            return True
        else:
            self._logger.warning(
                f"Event listener {listener_id} not found for conversation {conversation_id}"
            )
            return False

    async def delete_event_listener(
        self,
        listener_id: int,
        conversation_id: str,
    ) -> bool:
        """
        Delete a listener.

        Args:
            listener_id: ID of the listener
            conversation_id: Conversation ID for verification

        Returns:
            True if deleted, False if not found
        """
        from sqlalchemy import delete

        # First get the listener name for logging
        listener = await self.get_event_listener_by_id(listener_id, conversation_id)
        if not listener:
            self._logger.warning(
                f"Event listener {listener_id} not found for conversation {conversation_id}"
            )
            return False

        stmt = delete(event_listeners_table).where(
            (event_listeners_table.c.id == listener_id)
            & (event_listeners_table.c.conversation_id == conversation_id)
        )

        result = await self._db.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]

        if deleted_count > 0:
            self._logger.info(
                f"Deleted event listener '{listener['name']}' (ID: {listener_id}) "
                f"for conversation {conversation_id}"
            )
            return True
        else:
            # This shouldn't happen since we checked existence above
            self._logger.error(
                f"Failed to delete event listener {listener_id} - deletion returned 0 rows"
            )
            return False

    async def record_event(
        self,
        source_type: EventSourceType,
        metadata: dict[str, Any],
    ) -> int:
        """
        Record a new event.

        Args:
            source_type: Type of event source
            metadata: Event metadata

        Returns:
            ID of the created event
        """
        stmt = (
            insert(recent_events_table)
            .values(
                source_type=source_type,
                metadata=metadata,
                created_at=datetime.utcnow(),
            )
            .returning(recent_events_table.c.id)
        )

        result = await self._db.execute_with_retry(stmt)
        row = result.one()  # type: ignore[attr-defined]
        event_id = row[0]

        self._logger.debug(f"Recorded {source_type.value} event with ID {event_id}")
        return event_id

    async def get_recent_events(
        self,
        source_type: EventSourceType | None = None,
        hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Get recent events.

        Args:
            source_type: Filter by event source type
            hours: How many hours back to look
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        query = select(recent_events_table).where(
            recent_events_table.c.created_at >= cutoff
        )

        if source_type is not None:
            query = query.where(recent_events_table.c.source_type == source_type)

        query = query.order_by(recent_events_table.c.created_at.desc()).limit(limit)

        rows = await self._db.fetch_all(query)
        return [self._process_event_row(row) for row in rows]

    async def store_event(
        self,
        source_id: str,
        event_data: dict[str, Any],
        triggered_listener_ids: list[int] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Store an event in the recent_events table.

        Args:
            source_id: Event source ID
            event_data: Event data to store
            triggered_listener_ids: IDs of listeners that were triggered
            timestamp: Event timestamp (defaults to now)
        """
        import time
        from datetime import timezone

        try:
            if timestamp is None:
                timestamp = datetime.now(timezone.utc)

            # Generate unique event ID
            event_id = f"{source_id}:{int(time.time() * 1000000)}"

            stmt = insert(recent_events_table).values(
                event_id=event_id,
                source_id=source_id,
                event_data=event_data,
                triggered_listener_ids=triggered_listener_ids,
                timestamp=timestamp,
                created_at=datetime.now(timezone.utc),
            )

            await self._db.execute_with_retry(stmt)

        except SQLAlchemyError as e:
            self._logger.error(f"Database error in store_event: {e}", exc_info=True)
            # Don't raise - event storage failures shouldn't break event processing

    async def query_recent_events(
        self,
        source_id: str | None = None,
        hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query recent events with optional filters.

        Args:
            source_id: Filter by event source
            hours: How many hours back to look
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        from datetime import timezone

        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)

            stmt = select(recent_events_table).where(
                recent_events_table.c.timestamp >= cutoff_time
            )

            if source_id is not None:
                stmt = stmt.where(recent_events_table.c.source_id == source_id)

            # Order by timestamp descending and apply limit
            stmt = stmt.order_by(recent_events_table.c.timestamp.desc()).limit(limit)

            rows = await self._db.fetch_all(stmt)

            # Convert rows to dicts
            events = []
            for row in rows:
                event = dict(row)
                events.append(event)

            return events

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in query_recent_events: {e}", exc_info=True
            )
            raise

    async def cleanup_old_events(
        self,
        retention_hours: int = 48,
    ) -> int:
        """
        Clean up events older than retention period.

        Args:
            retention_hours: Hours to retain events (default: 48)

        Returns:
            Number of deleted events
        """
        from datetime import timezone

        from sqlalchemy import delete

        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=retention_hours)

            stmt = delete(recent_events_table).where(
                recent_events_table.c.created_at < cutoff_time
            )

            result = await self._db.execute_with_retry(stmt)
            deleted_count = result.rowcount  # type: ignore[attr-defined]

            self._logger.info(
                f"Cleaned up {deleted_count} events older than {retention_hours} hours"
            )
            return deleted_count

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in cleanup_old_events: {e}", exc_info=True
            )
            raise

    def _process_listener_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Process a listener row from the database."""
        listener = dict(row)

        # Handle JSON fields that might be stored as strings
        if isinstance(listener.get("event_filter"), str):
            try:
                listener["event_filter"] = json.loads(listener["event_filter"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse event_filter JSON for listener {listener.get('id')}"
                )
                listener["event_filter"] = {}

        if isinstance(listener.get("action_config"), str):
            try:
                listener["action_config"] = json.loads(listener["action_config"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse action_config JSON for listener {listener.get('id')}"
                )
                listener["action_config"] = {}

        return listener

    def _process_event_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Process an event row from the database."""
        event = dict(row)

        # Handle JSON metadata that might be stored as string
        if isinstance(event.get("metadata"), str):
            try:
                event["metadata"] = json.loads(event["metadata"])
            except json.JSONDecodeError:
                self._logger.warning(
                    f"Failed to parse metadata JSON for event {event.get('id')}"
                )
                event["metadata"] = {}

        return event
