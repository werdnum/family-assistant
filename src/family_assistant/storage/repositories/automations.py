"""Unified repository for managing both event and schedule-based automations."""

from typing import Any, Literal

from sqlalchemy import literal, select, union_all
from sqlalchemy.sql import functions as func

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.datetime_utils import normalize_datetime
from family_assistant.storage.events import event_listeners_table
from family_assistant.storage.models import Automation
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.repositories.events import EventsRepository
from family_assistant.storage.repositories.schedule_automations import (
    ScheduleAutomationsRepository,
)
from family_assistant.storage.schedule_automations import schedule_automations_table

AutomationType = Literal["event", "schedule"]


class AutomationsRepository(BaseRepository):
    """
    Unified repository providing consistent interface for both automation types.

    This repository wraps EventsRepository and ScheduleAutomationsRepository,
    providing a unified API for managing automations regardless of trigger type.
    Name uniqueness is enforced across both automation types at this layer.
    """

    def __init__(self, db_context: DatabaseContext) -> None:
        """Initialize with database context."""
        super().__init__(db_context)
        self._events_repo = EventsRepository(db_context)
        self._schedule_repo = ScheduleAutomationsRepository(db_context)

    async def list_all(
        self,
        conversation_id: str | None = None,
        automation_type: AutomationType | None = None,
        enabled: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[Automation], int]:
        """
        List automations with optional filtering.

        Args:
            conversation_id: Optional conversation ID to filter by. If None, returns automations from all conversations.
            automation_type: Filter by type (event, schedule) or None for all
            enabled: Filter by enabled status (True, False, or None for all)
            limit: Maximum number of results to return
            offset: Number of results to skip

        Returns:
            Tuple of (automations list, total count)
        """
        # Build queries for each automation type
        queries = []

        # Event listeners query
        if automation_type is None or automation_type == "event":
            event_query = select(
                event_listeners_table.c.id,
                event_listeners_table.c.name,
                event_listeners_table.c.description,
                event_listeners_table.c.conversation_id,
                event_listeners_table.c.interface_type,
                event_listeners_table.c.action_type,
                event_listeners_table.c.action_config,
                event_listeners_table.c.enabled,
                event_listeners_table.c.created_at,
                event_listeners_table.c.source_id,
                event_listeners_table.c.match_conditions,
                event_listeners_table.c.condition_script,
                event_listeners_table.c.one_time,
                event_listeners_table.c.daily_executions,
                event_listeners_table.c.daily_reset_at,
                event_listeners_table.c.last_execution_at,
                literal("event").label("type"),
                literal(None).label("recurrence_rule"),
                literal(None).label("next_scheduled_at"),
                literal(None).label("execution_count"),
            )

            if conversation_id is not None:
                event_query = event_query.where(
                    event_listeners_table.c.conversation_id == conversation_id
                )

            if enabled is not None:
                event_query = event_query.where(
                    event_listeners_table.c.enabled.is_(enabled)
                )

            queries.append(event_query)

        # Schedule automations query
        if automation_type is None or automation_type == "schedule":
            schedule_query = select(
                schedule_automations_table.c.id,
                schedule_automations_table.c.name,
                schedule_automations_table.c.description,
                schedule_automations_table.c.conversation_id,
                schedule_automations_table.c.interface_type,
                schedule_automations_table.c.action_type,
                schedule_automations_table.c.action_config,
                schedule_automations_table.c.enabled,
                schedule_automations_table.c.created_at,
                literal(None).label("source_id"),
                literal(None).label("match_conditions"),
                literal(None).label("condition_script"),
                literal(None).label("one_time"),
                literal(None).label("daily_executions"),
                literal(None).label("daily_reset_at"),
                schedule_automations_table.c.last_execution_at,
                literal("schedule").label("type"),
                schedule_automations_table.c.recurrence_rule,
                schedule_automations_table.c.next_scheduled_at,
                schedule_automations_table.c.execution_count,
            )

            if conversation_id is not None:
                schedule_query = schedule_query.where(
                    schedule_automations_table.c.conversation_id == conversation_id
                )

            if enabled is not None:
                schedule_query = schedule_query.where(
                    schedule_automations_table.c.enabled.is_(enabled)
                )

            queries.append(schedule_query)

        # Combine queries with UNION ALL or use single query
        if len(queries) == 1:
            # Single query case - convert to subquery for consistent handling
            subquery = queries[0].subquery()
        else:
            # Combine with UNION ALL
            subquery = union_all(*queries).subquery()

        # Get total count before pagination
        count_query = select(func.count().label("count")).select_from(subquery)
        count_row = await self._db.fetch_one(count_query)
        total_count = count_row["count"] if count_row else 0

        # Build final query with ordering and pagination
        combined_query = select(subquery).order_by(subquery.c.created_at.desc())

        if limit is not None:
            combined_query = combined_query.limit(limit)
        if offset is not None:
            combined_query = combined_query.offset(offset)

        # Execute and convert to Automation models, normalizing datetime fields
        rows = await self._db.fetch_all(combined_query)
        automations = []
        for row in rows:
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            auto: dict[str, Any] = dict(row)
            # Normalize datetime fields
            auto["created_at"] = normalize_datetime(
                auto.get("created_at")  # type: ignore[arg-type]
            )
            auto["last_execution_at"] = normalize_datetime(
                auto.get("last_execution_at")  # type: ignore[arg-type]
            )
            auto["next_scheduled_at"] = normalize_datetime(
                auto.get("next_scheduled_at")  # type: ignore[arg-type]
            )
            auto["daily_reset_at"] = normalize_datetime(
                auto.get("daily_reset_at")  # type: ignore[arg-type]
            )
            automations.append(Automation(**auto))  # type: ignore[arg-type]

        return automations, total_count

    async def get_by_id(
        self,
        automation_id: int,
        automation_type: AutomationType,
        conversation_id: str | None = None,
    ) -> Automation | None:
        """
        Get automation by ID and type.

        Args:
            automation_id: Automation ID
            automation_type: Type (event or schedule)
            conversation_id: Optional conversation ID for verification

        Returns:
            Automation model or None if not found
        """
        if automation_type == "event":
            automation = await self._events_repo.get_event_listener_by_id(
                listener_id=automation_id,
                conversation_id=conversation_id,
            )
            if automation:
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                event_dict: dict[str, Any] = dict(automation)
                event_dict["type"] = "event"
                # Normalize datetime fields
                event_dict["created_at"] = normalize_datetime(
                    event_dict.get("created_at")  # type: ignore[arg-type]
                )
                event_dict["last_execution_at"] = normalize_datetime(
                    event_dict.get("last_execution_at")  # type: ignore[arg-type]
                )
                event_dict["daily_reset_at"] = normalize_datetime(
                    event_dict.get("daily_reset_at")  # type: ignore[arg-type]
                )
                return Automation(**event_dict)  # type: ignore[arg-type]
        else:  # schedule
            automation = await self._schedule_repo.get_by_id(
                automation_id=automation_id,
                conversation_id=conversation_id,
            )
            if automation:
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                schedule_dict: dict[str, Any] = dict(automation)
                schedule_dict["type"] = "schedule"
                # Normalize datetime fields
                schedule_dict["created_at"] = normalize_datetime(
                    schedule_dict.get("created_at")  # type: ignore[arg-type]
                )
                schedule_dict["last_execution_at"] = normalize_datetime(
                    schedule_dict.get("last_execution_at")  # type: ignore[arg-type]
                )
                schedule_dict["next_scheduled_at"] = normalize_datetime(
                    schedule_dict.get("next_scheduled_at")  # type: ignore[arg-type]
                )
                return Automation(**schedule_dict)  # type: ignore[arg-type]

        return None

    async def get_by_name(
        self,
        name: str,
        conversation_id: str,
    ) -> Automation | None:
        """
        Get automation by name (searches both types).

        Args:
            name: Automation name
            conversation_id: Conversation ID

        Returns:
            Automation model or None if not found
        """
        # Check event listeners first with efficient query
        stmt = select(event_listeners_table).where(
            (event_listeners_table.c.name == name)
            & (event_listeners_table.c.conversation_id == conversation_id)
        )
        row = await self._events_repo._db.fetch_one(stmt)
        if row:
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            event_listener: dict[str, Any] = dict(row)
            # Normalize datetime fields
            event_listener["created_at"] = normalize_datetime(
                event_listener.get("created_at")  # type: ignore[arg-type]
            )
            event_listener["last_execution_at"] = normalize_datetime(
                event_listener.get("last_execution_at")  # type: ignore[arg-type]
            )
            event_listener["daily_reset_at"] = normalize_datetime(
                event_listener.get("daily_reset_at")  # type: ignore[arg-type]
            )
            event_listener["type"] = "event"
            return Automation(**event_listener)  # type: ignore[arg-type]

        # Check schedule automations
        schedule_automation = await self._schedule_repo.get_by_name(
            name=name,
            conversation_id=conversation_id,
        )
        if schedule_automation:
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            schedule_dict: dict[str, Any] = dict(schedule_automation)
            schedule_dict["type"] = "schedule"
            # Normalize datetime fields
            schedule_dict["created_at"] = normalize_datetime(
                schedule_dict.get("created_at")  # type: ignore[arg-type]
            )
            schedule_dict["last_execution_at"] = normalize_datetime(
                schedule_dict.get("last_execution_at")  # type: ignore[arg-type]
            )
            schedule_dict["next_scheduled_at"] = normalize_datetime(
                schedule_dict.get("next_scheduled_at")  # type: ignore[arg-type]
            )
            return Automation(**schedule_dict)  # type: ignore[arg-type]

        return None

    async def check_name_available(
        self,
        name: str,
        conversation_id: str,
        exclude_id: int | None = None,
        exclude_type: AutomationType | None = None,
    ) -> tuple[bool, str | None]:
        """
        Check if an automation name is available (not used by any automation).

        Args:
            name: Name to check
            conversation_id: Conversation ID
            exclude_id: Optional ID to exclude from check (for updates)
            exclude_type: Optional type to exclude (must be provided with exclude_id)

        Returns:
            Tuple of (is_available, error_message)
        """
        # Get all automations with this name
        existing = await self.get_by_name(name, conversation_id)

        if existing is None:
            return True, None

        # Check if we should exclude this automation (update case)
        if (
            exclude_id is not None
            and exclude_type is not None
            and existing.id == exclude_id
            and existing.type == exclude_type
        ):
            return True, None

        # Name is taken
        existing_type = existing.type
        return (
            False,
            f"An automation named '{name}' already exists "
            f"({existing_type} automation ID: {existing.id})",
        )

    async def update_enabled(
        self,
        automation_id: int,
        automation_type: AutomationType,
        conversation_id: str,
        enabled: bool,
    ) -> bool:
        """
        Enable or disable an automation.

        Args:
            automation_id: Automation ID
            automation_type: Type (event or schedule)
            conversation_id: Conversation ID for verification
            enabled: New enabled status

        Returns:
            True if updated, False if not found
        """
        if automation_type == "event":
            return await self._events_repo.update_event_listener_enabled(
                listener_id=automation_id,
                conversation_id=conversation_id,
                enabled=enabled,
            )
        else:  # schedule
            return await self._schedule_repo.update_enabled(
                automation_id=automation_id,
                conversation_id=conversation_id,
                enabled=enabled,
            )

    async def delete(
        self,
        automation_id: int,
        automation_type: AutomationType,
        conversation_id: str,
    ) -> bool:
        """
        Delete an automation.

        Args:
            automation_id: Automation ID
            automation_type: Type (event or schedule)
            conversation_id: Conversation ID for verification

        Returns:
            True if deleted, False if not found
        """
        if automation_type == "event":
            return await self._events_repo.delete_event_listener(
                listener_id=automation_id,
                conversation_id=conversation_id,
            )
        else:  # schedule
            return await self._schedule_repo.delete(
                automation_id=automation_id,
                conversation_id=conversation_id,
            )

    async def get_execution_stats(
        self,
        automation_id: int,
        automation_type: AutomationType,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> dict[str, Any]:
        """
        Get execution statistics for an automation.

        Args:
            automation_id: Automation ID
            automation_type: Type (event or schedule)

        Returns:
            Dictionary with execution statistics
        """
        if automation_type == "event":
            return await self._events_repo.get_listener_execution_stats(
                listener_id=automation_id
            )
        else:  # schedule
            return await self._schedule_repo.get_execution_stats(
                automation_id=automation_id
            )
