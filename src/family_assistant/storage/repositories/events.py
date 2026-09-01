"""Repository for events storage operations."""

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, delete, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import functions as func

from family_assistant.security.definition_records import (
    CreationDisposition,
    DefinitionArtifactKind,
    DefinitionGateOutcome,
    GateProvenance,
    definition_record_from_row,
    listener_definition_content,
    merge_retained_definition,
    register_definition_write,
    stamp_definition,
)
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import DatabaseTransaction
from family_assistant.storage.datetime_utils import normalize_datetime
from family_assistant.storage.events import (
    EventActionType,
    EventSourceType,
    event_listeners_table,
    recent_events_table,
)
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.types import (
    ActionConfig,
    EventListenerDict,
    ListenerExecutionStatsDict,
    MatchConditions,
    RecentEventDict,
)


class EventsRepository(BaseRepository):
    """Repository for managing events and rate limiting in the database."""

    def _normalize_event_listener(self, row: Mapping[str, Any]) -> EventListenerDict:
        """
        Normalize event listener row from database.

        Args:
            row: Database row as dictionary

        Returns:
            EventListenerDict with normalized datetime fields
        """
        listener = dict(row)

        # Normalize datetime fields
        listener["created_at"] = normalize_datetime(listener.get("created_at"))
        listener["daily_reset_at"] = normalize_datetime(listener.get("daily_reset_at"))
        listener["last_execution_at"] = normalize_datetime(
            listener.get("last_execution_at")
        )

        return listener  # type: ignore[return-value]

    def _normalize_event(self, row: Mapping[str, Any]) -> RecentEventDict:
        """
        Normalize recent event row from database.

        Args:
            row: Database row as dictionary

        Returns:
            RecentEventDict with normalized datetime fields
        """
        event = dict(row)

        # Normalize datetime fields
        event["timestamp"] = normalize_datetime(event.get("timestamp"))
        event["created_at"] = normalize_datetime(event.get("created_at"))

        return event  # type: ignore[return-value]

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
            return await self._check_and_update_rate_limit(listener_id, conversation_id)
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in check_and_update_rate_limit({listener_id}): {e}"
            )
            # On error, allow execution but log it
            return True, None

    async def _check_and_update_rate_limit(
        self,
        listener_id: int,
        conversation_id: str,
    ) -> tuple[bool, str | None]:
        now = datetime.now(UTC)

        listener = await self.get_event_listener_by_id(listener_id)
        if not listener or listener.get("conversation_id") != conversation_id:
            return False, "Listener not found"

        daily_reset_at = listener["daily_reset_at"]
        if not daily_reset_at or now > daily_reset_at:
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
            await self._db.execute(stmt)
            return True, None

        if listener["daily_executions"] >= 5:
            return (
                False,
                f"Daily limit exceeded ({listener['daily_executions']} triggers today)",
            )

        stmt = (
            update(event_listeners_table)
            .where(event_listeners_table.c.id == listener_id)
            .values(
                daily_executions=event_listeners_table.c.daily_executions + 1,
                last_execution_at=now,
            )
        )
        await self._db.execute(stmt)
        return True, None

    async def create_event_listener(
        self,
        name: str,
        source_id: str,
        match_conditions: MatchConditions,
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
        action_type: str | EventActionType = EventActionType.wake_llm,
        action_config: ActionConfig | None = None,
        condition_script: str | None = None,
        one_time: bool = False,
        enabled: bool = True,
        processing_profile_id: str | None = None,
        created_by_user_id: str | None = None,
        definition_taint_state: TurnTaintState | None = None,
        definition_gate: DefinitionGateOutcome | None = None,
        definition_human_direct: bool = False,
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
            condition_script: Optional Python script for complex matching
            one_time: If true, listener is disabled after first trigger
            enabled: Whether the listener is enabled

        Returns:
            ID of the created listener
        """
        definition_record = stamp_definition(
            content=listener_definition_content(
                name=name,
                description=description,
                source_id=source_id,
                match_conditions=match_conditions,
                action_type=str(getattr(action_type, "value", action_type)),
                action_config=action_config,
                condition_script=condition_script,
            ),
            taint_state=definition_taint_state,
            gate_outcome=definition_gate,
            human_direct=definition_human_direct,
        ).to_dict()
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
                    condition_script=condition_script,
                    conversation_id=conversation_id,
                    interface_type=interface_type,
                    one_time=one_time,
                    enabled=enabled,
                    processing_profile_id=processing_profile_id,
                    created_by_user_id=created_by_user_id,
                    created_at=datetime.now(UTC),
                    daily_executions=0,
                    definition_record=definition_record,
                )
                .returning(event_listeners_table.c.id)
            )

            result = await self._db.execute(stmt)
            listener_id = result.scalar_one()
            register_definition_write(
                definition_gate,
                definition_record,
                kind=DefinitionArtifactKind.EVENT_LISTENER,
                artifact_id=listener_id,
            )

            self._logger.info(
                f"Created event listener '{name}' (ID: {listener_id}) for conversation {conversation_id}"
            )
            return listener_id

        except IntegrityError as e:
            error_msg = str(e).lower()
            # Check for various forms of the unique constraint error
            if "uq_name_conversation" in error_msg or (
                "unique" in error_msg
                and "name" in error_msg
                and "conversation" in error_msg
            ):
                self._logger.error(
                    f"Event listener with name '{name}' already exists for conversation {conversation_id}"
                )
                raise ValueError(
                    f"An event listener named '{name}' already exists in this conversation"
                ) from e
            raise
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in create_event_listener: {e}")
            raise

    async def get_event_listeners(
        self,
        conversation_id: str,
        source_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[EventListenerDict]:
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

        return [self._normalize_event_listener(dict(row)) for row in rows]

    async def attach_definition_verdict(
        self,
        listener_id: int,
        *,
        write_id: str,
        disposition: CreationDisposition,
        gate: GateProvenance,
    ) -> bool:
        """Attach an asynchronously computed verdict to this definition's record.

        Under ``observe`` the reviewer runs off the critical path, so the write
        lands before its verdict exists and the verdict arrives here. The write
        id guards the update, read and write in one transaction: the row must
        still hold the exact write the verdict judged, so a mutation racing the
        review -- an identical rewrite from another turn included -- leaves the
        new content awaiting its own verdict rather than inheriting this one.

        Returns whether the verdict was attached.
        """

        async def body(txn: DatabaseTransaction) -> bool:
            existing = await txn.events.get_event_listener_by_id(listener_id)
            record = definition_record_from_row(
                existing["definition_record"] if existing is not None else None
            )
            if record is None or record.pending_write_id != write_id:
                return False
            await txn.execute(
                update(event_listeners_table)
                .where(event_listeners_table.c.id == listener_id)
                .values(
                    # ast-grep-ignore: no-unstamped-executable-definition-write - verdict attach: derived from the stored record by replace(), so the hash and stamp are unchanged
                    definition_record=replace(
                        record,
                        disposition=disposition,
                        gate=gate,
                        pending_write_id=None,
                    ).to_dict()
                )
            )
            return True

        return await self._db.atomic(body)

    async def get_event_listener_by_id(
        self, listener_id: int, conversation_id: str | None = None
    ) -> EventListenerDict | None:
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

        return self._normalize_event_listener(dict(row))

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

        result = await self._db.execute(stmt)
        updated_count = result.rowcount

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

        result = await self._db.execute(stmt)
        deleted_count = result.rowcount

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

    async def update_event_listener(
        self,
        listener_id: int,
        conversation_id: str,
        name: str,
        description: str | None,
        match_conditions: MatchConditions,
        action_config: ActionConfig | None,
        one_time: bool,
        enabled: bool,
        condition_script: str | None = None,
        processing_profile_id: str | None = None,
        created_by_user_id: str | None = None,
        definition_taint_state: TurnTaintState | None = None,
        definition_gate: DefinitionGateOutcome | None = None,
        definition_human_direct: bool = False,
    ) -> bool:
        """
        Update an event listener.

        Args:
            listener_id: ID of the listener to update
            conversation_id: Conversation ID for verification
            name: New name for the listener
            description: New description (optional)
            match_conditions: New match conditions
            action_config: New action configuration (optional)
            one_time: Whether listener should auto-disable after first trigger
            enabled: Whether the listener is enabled
            condition_script: Optional Python script for complex matching
            processing_profile_id: When set, re-stamp the creating profile so the
                updated script executes under the updating profile's tools.
            created_by_user_id: When set, re-stamp the creating user.

        Returns:
            True if updated successfully, False if not found or unauthorized
        """

        # First verify the listener exists and belongs to the conversation
        existing = await self.get_event_listener_by_id(listener_id, conversation_id)
        if not existing:
            self._logger.warning(
                f"Event listener {listener_id} not found for conversation {conversation_id}"
            )
            return False

        # Prepare update values
        update_values = {
            "name": name,
            "description": description,
            "match_conditions": match_conditions,
            "one_time": one_time,
            "enabled": enabled,
        }

        # Only update action_config if provided
        if action_config is not None:
            update_values["action_config"] = action_config

        # Re-stamp creator provenance when the updating profile/user is supplied,
        # so the updated script executes under the updating profile's tools.
        if processing_profile_id is not None:
            update_values["processing_profile_id"] = processing_profile_id
        if created_by_user_id is not None:
            update_values["created_by_user_id"] = created_by_user_id

        # Always update condition_script (can be None to clear it)
        update_values["condition_script"] = condition_script

        merged_action_config: ActionConfig | None = (
            action_config if action_config is not None else existing["action_config"]
        )
        # Hash the complete post-mutation definition: ``action_config`` is
        # retained from the stored row when the caller omits it, so a record
        # built from the arguments alone would describe content no gate saw.
        retained = merge_retained_definition(
            definition_taint_state
            if definition_taint_state is not None
            else TurnTaintState.empty(),
            stored_record=existing.get("definition_record"),
            retained_content=listener_definition_content(
                name=str(existing["name"]),
                description=existing["description"],
                source_id=str(existing["source_id"]),
                match_conditions=existing["match_conditions"],
                action_type=str(existing["action_type"]),
                action_config=existing["action_config"],
                condition_script=existing["condition_script"],
            ),
        )
        definition_record = stamp_definition(
            content=listener_definition_content(
                name=name,
                description=description,
                source_id=str(existing["source_id"]),
                match_conditions=match_conditions,
                action_type=str(existing["action_type"]),
                action_config=merged_action_config,
                condition_script=condition_script,
            ),
            taint_state=retained.state,
            gate_outcome=definition_gate,
            retains_uncured_content=retained.uncured,
            human_direct=definition_human_direct,
        ).to_dict()
        update_values["definition_record"] = definition_record
        register_definition_write(
            definition_gate,
            definition_record,
            kind=DefinitionArtifactKind.EVENT_LISTENER,
            artifact_id=listener_id,
        )

        # Update the listener
        stmt = (
            update(event_listeners_table)
            .where(
                (event_listeners_table.c.id == listener_id)
                & (event_listeners_table.c.conversation_id == conversation_id)
            )
            .values(**update_values)
        )

        result = await self._db.execute(stmt)
        updated_count = result.rowcount

        if updated_count > 0:
            self._logger.info(
                f"Updated event listener '{name}' (ID: {listener_id}) "
                f"for conversation {conversation_id}"
            )
            return True
        else:
            # This shouldn't happen since we checked existence above
            self._logger.error(
                f"Failed to update event listener {listener_id} - update returned 0 rows"
            )
            return False

    async def record_event(
        self,
        source_type: EventSourceType,
        # ast-grep-ignore: no-dict-any - event metadata is unstructured JSON from external sources
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

        result = await self._db.execute(stmt)
        event_id = result.scalar_one()

        self._logger.debug(f"Recorded {source_type.value} event with ID {event_id}")
        return event_id

    async def get_recent_events(
        self,
        source_type: EventSourceType | None = None,
        hours: int = 24,
        limit: int = 100,
        # ast-grep-ignore: no-dict-any - returns processed rows with parsed JSON metadata field
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
        # ast-grep-ignore: no-dict-any - event_data is unstructured JSON from external sources
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
        try:
            if timestamp is None:
                timestamp = datetime.now(UTC)

            # Generate unique event ID
            event_id = f"{source_id}:{int(time.time() * 1000000)}"

            stmt = insert(recent_events_table).values(
                event_id=event_id,
                source_id=source_id,
                event_data=event_data,
                triggered_listener_ids=triggered_listener_ids,
                timestamp=timestamp,
                created_at=datetime.now(UTC),
            )

            await self._db.execute(stmt)

        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in store_event: {e}")
            # Don't raise - event storage failures shouldn't break event processing

    async def query_recent_events(
        self,
        source_id: str | None = None,
        hours: int = 24,
        limit: int = 100,
    ) -> list[RecentEventDict]:
        """
        Query recent events with optional filters.

        Args:
            source_id: Filter by event source
            hours: How many hours back to look
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        try:
            return await self._query_recent_events(source_id, hours, limit)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in query_recent_events: {e}")
            raise

    async def _query_recent_events(
        self,
        source_id: str | None,
        hours: int,
        limit: int,
    ) -> list[RecentEventDict]:
        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)
        stmt = select(recent_events_table).where(
            recent_events_table.c.timestamp >= cutoff_time
        )
        if source_id is not None:
            stmt = stmt.where(recent_events_table.c.source_id == source_id)
        stmt = stmt.order_by(recent_events_table.c.timestamp.desc()).limit(limit)
        rows = await self._db.fetch_all(stmt)
        return [self._normalize_event(row) for row in rows]

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
        try:
            cutoff_time = datetime.now(UTC) - timedelta(hours=retention_hours)

            stmt = delete(recent_events_table).where(
                recent_events_table.c.created_at < cutoff_time
            )

            result = await self._db.execute(stmt)
            deleted_count = result.rowcount

            self._logger.info(
                f"Cleaned up {deleted_count} events older than {retention_hours} hours"
            )
            return deleted_count

        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in cleanup_old_events: {e}")
            raise

    async def cleanup_completed_one_time_listeners(
        self,
        retention_hours: int = 24,
    ) -> int:
        """
        Delete completed one-time event listeners older than the retention period.

        A "completed" one-time listener is one that has ``one_time=True`` and
        ``enabled=False`` (disabled after firing) with a ``last_execution_at``
        older than the retention period.

        Args:
            retention_hours: Hours to retain completed one-time listeners (default: 24)

        Returns:
            Number of deleted listeners
        """
        try:
            return await self._cleanup_completed_one_time_listeners(retention_hours)
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in cleanup_completed_one_time_listeners: {e}"
            )
            raise

    async def _cleanup_completed_one_time_listeners(self, retention_hours: int) -> int:
        cutoff_time = datetime.now(UTC) - timedelta(hours=retention_hours)
        stmt = delete(event_listeners_table).where(
            (event_listeners_table.c.one_time.is_(True))
            & (event_listeners_table.c.enabled.is_(False))
            & (event_listeners_table.c.last_execution_at < cutoff_time)
        )
        result = await self._db.execute(stmt)
        deleted_count = result.rowcount
        if deleted_count > 0:
            self._logger.info(
                f"Cleaned up {deleted_count} completed one-time listeners "
                f"older than {retention_hours} hours"
            )
        return deleted_count

    def _process_listener_row(self, row: Mapping[str, Any]) -> EventListenerDict:
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

        return listener  # type: ignore[return-value]

    # ast-grep-ignore: no-dict-any - processes old-format event rows with dynamic metadata field
    def _process_event_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
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

    async def get_events_with_listeners(
        self,
        source_id: str | None = None,
        hours: int = 24,
        limit: int = 50,
        offset: int = 0,
        only_triggered: bool = False,
    ) -> tuple[list[dict], int]:
        """Get events with listener information."""
        try:
            return await self._get_events_with_listeners(
                source_id, hours, limit, offset, only_triggered
            )
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_events_with_listeners: {e}")
            raise

    async def _get_events_with_listeners(
        self,
        source_id: str | None,
        hours: int,
        limit: int,
        offset: int,
        only_triggered: bool,
    ) -> tuple[list[dict], int]:
        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)
        stmt = select(recent_events_table).where(
            recent_events_table.c.timestamp >= cutoff_time
        )
        if source_id:
            stmt = stmt.where(recent_events_table.c.source_id == source_id)
        if only_triggered:
            stmt = stmt.where(recent_events_table.c.triggered_listener_ids.isnot(None))

        count_stmt = select(func.count().label("count")).select_from(
            stmt.alias("events_subquery")
        )
        count_result = await self._db.fetch_one(count_stmt)
        total_count = count_result["count"] if count_result else 0
        stmt = stmt.order_by(recent_events_table.c.timestamp.desc()).limit(limit)
        rows = await self._db.fetch_all(stmt.offset(offset))

        events = []
        for row in rows:
            event = dict(row)
            listener_names = []
            for listener_id in event.get("triggered_listener_ids") or []:
                listener = await self.get_event_listener_by_id(listener_id)
                if listener:
                    listener_names.append(listener["name"])
            event["triggered_listener_names"] = listener_names
            events.append(event)
        return events, total_count

    async def get_listener_execution_stats(
        self,
        listener_id: int,
    ) -> ListenerExecutionStatsDict | None:
        """Get execution statistics for a listener."""

        try:
            return await self._get_listener_execution_stats(listener_id)
        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in get_listener_execution_stats: {e}"
            )
            raise

    async def _get_listener_execution_stats(
        self, listener_id: int
    ) -> ListenerExecutionStatsDict | None:
        listener = await self.get_event_listener_by_id(listener_id)
        if not listener:
            return None

        is_sqlite = self._db.dialect_name == "sqlite"
        if is_sqlite:
            search_pattern = f"%{listener_id}%"
            listener_filter = cast(
                recent_events_table.c.triggered_listener_ids, String
            ).like(search_pattern)
        else:
            listener_filter = recent_events_table.c.triggered_listener_ids.op("@>")(
                cast([listener_id], JSONB)
            )

        stmt = (
            select(func.count().label("count"))
            .select_from(recent_events_table)
            .where(listener_filter)
        )
        result = await self._db.fetch_one(stmt)
        total_executions = result["count"] if result else 0
        recent_stmt = (
            select(recent_events_table)
            .where(listener_filter)
            .order_by(recent_events_table.c.timestamp.desc())
            .limit(10)
        )
        recent_events = await self._db.fetch_all(recent_stmt)

        return ListenerExecutionStatsDict(
            total_executions=total_executions,
            daily_executions=listener.get("daily_executions", 0),
            daily_limit=5,
            last_execution_at=listener.get("last_execution_at"),
            recent_events=[self._normalize_event(dict(row)) for row in recent_events],
        )

    async def get_event_by_id(self, event_id: str) -> RecentEventDict | None:
        """Get a specific event by ID."""
        try:
            stmt = select(recent_events_table).where(
                recent_events_table.c.event_id == event_id
            )
            row = await self._db.fetch_one(stmt)
            if row:
                return self._normalize_event(row)
            return None

        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_event_by_id: {e}")
            raise

    async def get_all_event_listeners(
        self,
        source_id: str | None = None,
        action_type: str | None = None,
        conversation_id: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EventListenerDict], int]:
        """Get all event listeners (admin view) with pagination."""
        try:
            return await self._get_all_event_listeners(
                source_id,
                action_type,
                conversation_id,
                enabled,
                limit,
                offset,
            )
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_all_event_listeners: {e}")
            raise

    async def _get_all_event_listeners(
        self,
        source_id: str | None,
        action_type: str | None,
        conversation_id: str | None,
        enabled: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[list[EventListenerDict], int]:
        stmt = select(event_listeners_table)
        if source_id:
            stmt = stmt.where(event_listeners_table.c.source_id == source_id)
        if action_type:
            stmt = stmt.where(event_listeners_table.c.action_type == action_type)
        if conversation_id:
            stmt = stmt.where(
                event_listeners_table.c.conversation_id == conversation_id
            )
        if enabled is not None:
            stmt = stmt.where(event_listeners_table.c.enabled == enabled)

        count_stmt = select(func.count().label("count")).select_from(
            stmt.alias("listeners_subquery")
        )
        count_result = await self._db.fetch_one(count_stmt)
        total_count = count_result["count"] if count_result else 0
        stmt = stmt.order_by(event_listeners_table.c.created_at.desc()).limit(limit)
        rows = await self._db.fetch_all(stmt.offset(offset))
        listeners = [self._normalize_event_listener(dict(row)) for row in rows]
        return listeners, total_count
