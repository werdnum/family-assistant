"""Unified repository for managing both event and schedule-based automations."""

from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.events import event_listeners_table
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.repositories.events import EventsRepository
from family_assistant.storage.repositories.schedule_automations import (
    ScheduleAutomationsRepository,
)

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
        conversation_id: str,
        automation_type: AutomationType | None = None,
        enabled: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        List automations for a conversation with pagination.

        Args:
            conversation_id: Conversation ID
            automation_type: Filter by type (event, schedule) or None for all
            enabled: Filter by enabled status (True, False, or None for all)
            limit: Maximum number of results to return
            offset: Number of results to skip

        Returns:
            Tuple of (automations list, total count)
        """
        automations = []

        # Fetch event listeners if needed
        if automation_type is None or automation_type == "event":
            event_listeners = await self._events_repo.get_event_listeners(
                conversation_id=conversation_id,
                enabled=enabled,
            )
            for listener in event_listeners:
                listener["type"] = "event"
                automations.append(listener)

        # Fetch schedule automations if needed
        if automation_type is None or automation_type == "schedule":
            # Map enabled filter to enabled_only parameter
            # enabled=True -> enabled_only=True (only enabled)
            # enabled=False -> fetch all then filter (repository doesn't support disabled-only)
            # enabled=None -> enabled_only=False (all)
            schedule_automations = await self._schedule_repo.list_all(
                conversation_id=conversation_id,
                enabled_only=enabled is True,
            )
            # If enabled=False, filter out enabled ones to get only disabled
            if enabled is False:
                schedule_automations = [
                    auto
                    for auto in schedule_automations
                    if not auto.get("enabled", True)
                ]
            for automation in schedule_automations:
                automation["type"] = "schedule"
                automations.append(automation)

        # Sort by created_at descending (newest first)
        # Use a sentinel datetime for missing created_at (shouldn't happen in practice)
        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        automations.sort(key=lambda x: x.get("created_at") or sentinel, reverse=True)

        # Get total count before pagination
        total_count = len(automations)

        # Apply pagination if specified
        # TODO: This is in-memory pagination which doesn't scale well. For better performance,
        # we should implement database-level pagination, possibly using a UNION query across
        # both event_listeners and schedule_automations tables.
        if offset is not None:
            automations = automations[offset:]
        if limit is not None:
            automations = automations[:limit]

        return automations, total_count

    async def get_by_id(
        self,
        automation_id: int,
        automation_type: AutomationType,
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Get automation by ID and type.

        Args:
            automation_id: Automation ID
            automation_type: Type (event or schedule)
            conversation_id: Optional conversation ID for verification

        Returns:
            Automation dict with "type" field or None if not found
        """
        if automation_type == "event":
            automation = await self._events_repo.get_event_listener_by_id(
                listener_id=automation_id,
                conversation_id=conversation_id,
            )
            if automation:
                automation["type"] = "event"
        else:  # schedule
            automation = await self._schedule_repo.get_by_id(
                automation_id=automation_id,
                conversation_id=conversation_id,
            )
            if automation:
                automation["type"] = "schedule"

        return automation

    async def get_by_name(
        self,
        name: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        """
        Get automation by name (searches both types).

        Args:
            name: Automation name
            conversation_id: Conversation ID

        Returns:
            Automation dict with "type" field or None if not found
        """
        # Check event listeners first with efficient query
        stmt = select(event_listeners_table).where(
            (event_listeners_table.c.name == name)
            & (event_listeners_table.c.conversation_id == conversation_id)
        )
        row = await self._events_repo._db.fetch_one(stmt)
        if row:
            event_listener = dict(row)
            event_listener["type"] = "event"
            return event_listener

        # Check schedule automations
        schedule_automation = await self._schedule_repo.get_by_name(
            name=name,
            conversation_id=conversation_id,
        )
        if schedule_automation:
            schedule_automation["type"] = "schedule"
            return schedule_automation

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
            and existing["id"] == exclude_id
            and existing["type"] == exclude_type
        ):
            return True, None

        # Name is taken
        existing_type = existing["type"]
        return (
            False,
            f"An automation named '{name}' already exists "
            f"({existing_type} automation ID: {existing['id']})",
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
