"""Repository for schedule-based automations operations."""

import uuid
from datetime import datetime, timezone
from typing import Any

from dateutil import rrule
from dateutil.parser import ParserError
from sqlalchemy import String, delete, insert, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import enqueue_task, tasks_table

# Sentinel to distinguish "not provided" from "explicitly None"
_UNSET = object()


class ScheduleAutomationsRepository(BaseRepository):
    """Repository for managing schedule-based automations."""

    def _parse_rrule_and_get_next(
        self, recurrence_rule: str, after: datetime | None = None
    ) -> datetime | None:
        """
        Parse RRULE and calculate next execution time.

        Args:
            recurrence_rule: RRULE string
            after: Calculate next execution after this time (defaults to now)

        Returns:
            Next execution datetime or None if no more executions
        """
        try:
            if after is None:
                after = datetime.now(timezone.utc)

            # Parse the RRULE
            rule = rrule.rrulestr(recurrence_rule, dtstart=after)

            # Get the next occurrence
            next_occurrence = rule.after(after)

            return next_occurrence
        except (ValueError, ParserError) as e:
            self._logger.error(f"Failed to parse RRULE '{recurrence_rule}': {e}")
            return None

    async def create(
        self,
        name: str,
        recurrence_rule: str,
        action_type: str,
        action_config: dict[str, Any],
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
        enabled: bool = True,
    ) -> int:
        """
        Create a schedule automation and schedule first task instance.

        Args:
            name: Automation name
            recurrence_rule: RRULE string
            action_type: Type of action (wake_llm or script)
            action_config: Action configuration
            conversation_id: Conversation ID
            interface_type: Interface type
            description: Optional description

        Returns:
            ID of the created automation
        """
        try:
            # Calculate first execution time
            next_scheduled_at = self._parse_rrule_and_get_next(recurrence_rule)
            if next_scheduled_at is None:
                raise ValueError(f"Invalid RRULE: {recurrence_rule}")

            # Create the automation record
            stmt = (
                insert(schedule_automations_table)
                .values(
                    name=name,
                    description=description,
                    recurrence_rule=recurrence_rule,
                    next_scheduled_at=next_scheduled_at,
                    action_type=action_type,
                    action_config=action_config,
                    conversation_id=conversation_id,
                    interface_type=interface_type,
                    enabled=enabled,
                    created_at=datetime.now(timezone.utc),
                    execution_count=0,
                )
                .returning(schedule_automations_table.c.id)
            )

            result = await self._db.execute_with_retry(stmt)
            automation_id = result.scalar_one()

            self._logger.info(
                f"Created schedule automation '{name}' (ID: {automation_id}) "
                f"for conversation {conversation_id}"
            )

            # Schedule the first task instance
            task_type = (
                "llm_callback" if action_type == "wake_llm" else "script_execution"
            )
            task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

            payload = {
                "conversation_id": conversation_id,
                "interface_type": interface_type,
                "automation_id": str(automation_id),
                "automation_type": "schedule",
            }

            # Add action-specific payload
            if action_type == "wake_llm":
                payload["callback_context"] = action_config.get("context", "")
            else:  # script
                payload["script_code"] = action_config.get("script_code", "")
                payload["task_name"] = action_config.get("task_name", name)

            # Note: We do NOT pass recurrence_rule here because recurrence
            # is managed manually via after_task_execution callback, not
            # by the task worker's automatic recurrence system
            await enqueue_task(
                db_context=self._db,
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                scheduled_at=next_scheduled_at,
            )

            self._logger.info(
                f"Scheduled first task for automation {automation_id} at {next_scheduled_at}"
            )

            return automation_id

        except IntegrityError as e:
            error_msg = str(e).lower()
            if "uq_sched_name_conversation" in error_msg or (
                "unique" in error_msg
                and "name" in error_msg
                and "conversation" in error_msg
            ):
                self._logger.error(
                    f"Schedule automation with name '{name}' already exists "
                    f"for conversation {conversation_id}"
                )
                raise ValueError(
                    f"A schedule automation named '{name}' already exists in this conversation"
                ) from e
            raise
        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in create_schedule_automation: {e}", exc_info=True
            )
            raise

    async def create_full(
        self,
        name: str,
        recurrence_rule: str,
        action_type: str,
        action_config: dict[str, Any],
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Create automation and return full entity (avoids extra query).

        Args:
            Same as create()

        Returns:
            Full automation dictionary
        """
        automation_id = await self.create(
            name=name,
            recurrence_rule=recurrence_rule,
            action_type=action_type,
            action_config=action_config,
            conversation_id=conversation_id,
            interface_type=interface_type,
            description=description,
        )

        # Fetch and return the full entity
        automation = await self.get_by_id(automation_id)
        if automation is None:
            raise RuntimeError(
                f"Failed to retrieve newly created automation {automation_id}"
            )
        return automation

    async def get_by_id(
        self, automation_id: int, conversation_id: str | None = None
    ) -> dict[str, Any] | None:
        """
        Get automation by ID, optionally verifying conversation.

        Args:
            automation_id: Automation ID
            conversation_id: Optional conversation ID for verification

        Returns:
            Automation dict or None if not found
        """
        if conversation_id:
            stmt = select(schedule_automations_table).where(
                (schedule_automations_table.c.id == automation_id)
                & (schedule_automations_table.c.conversation_id == conversation_id)
            )
        else:
            stmt = select(schedule_automations_table).where(
                schedule_automations_table.c.id == automation_id
            )

        row = await self._db.fetch_one(stmt)
        if not row:
            return None

        return dict(row)

    async def get_by_name(
        self, name: str, conversation_id: str
    ) -> dict[str, Any] | None:
        """
        Get automation by name within a conversation.

        Args:
            name: Automation name
            conversation_id: Conversation ID

        Returns:
            Automation dict or None if not found
        """
        stmt = select(schedule_automations_table).where(
            (schedule_automations_table.c.name == name)
            & (schedule_automations_table.c.conversation_id == conversation_id)
        )

        row = await self._db.fetch_one(stmt)
        if not row:
            return None

        return dict(row)

    async def list_all(
        self,
        conversation_id: str,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """
        List all schedule automations for a conversation.

        Args:
            conversation_id: Conversation ID
            enabled_only: Filter by enabled status

        Returns:
            List of automation dictionaries
        """
        stmt = select(schedule_automations_table).where(
            schedule_automations_table.c.conversation_id == conversation_id
        )

        if enabled_only:
            stmt = stmt.where(schedule_automations_table.c.enabled.is_(True))

        stmt = stmt.order_by(schedule_automations_table.c.created_at.desc())

        rows = await self._db.fetch_all(stmt)
        return [dict(row) for row in rows]

    async def update_enabled(
        self,
        automation_id: int,
        conversation_id: str,
        enabled: bool,
    ) -> bool:
        """
        Enable or disable automation.

        Args:
            automation_id: Automation ID
            conversation_id: Conversation ID for verification
            enabled: New enabled status

        Returns:
            True if updated, False if not found
        """
        stmt = (
            update(schedule_automations_table)
            .where(
                (schedule_automations_table.c.id == automation_id)
                & (schedule_automations_table.c.conversation_id == conversation_id)
            )
            .values(enabled=enabled)
        )

        result = await self._db.execute_with_retry(stmt)
        updated_count = result.rowcount  # type: ignore[attr-defined]

        if updated_count > 0:
            status = "enabled" if enabled else "disabled"
            self._logger.info(
                f"Updated schedule automation {automation_id} to {status}"
            )
            return True
        else:
            self._logger.warning(
                f"Schedule automation {automation_id} not found for conversation {conversation_id}"
            )
            return False

    async def update(
        self,
        automation_id: int,
        conversation_id: str,
        name: str | None | object = _UNSET,
        recurrence_rule: str | None | object = _UNSET,
        action_config: dict[str, Any] | None | object = _UNSET,
        description: str | None | object = _UNSET,
        enabled: bool | None | object = _UNSET,
    ) -> bool:
        """
        Update automation configuration.

        Args:
            automation_id: Automation ID
            conversation_id: Conversation ID for verification
            name: New name (optional, use None to clear)
            recurrence_rule: New RRULE (if provided, recalculates next_scheduled_at)
            action_config: New action configuration (use None to clear)
            description: New description (use None to clear)
            enabled: New enabled status (optional)

        Returns:
            True if updated, False if not found
        """
        # Verify exists and belongs to conversation
        existing = await self.get_by_id(automation_id, conversation_id)
        if not existing:
            self._logger.warning(
                f"Schedule automation {automation_id} not found for conversation {conversation_id}"
            )
            return False

        update_values: dict[str, Any] = {}

        if isinstance(name, str) or name is None:
            update_values["name"] = name

        if isinstance(description, str) or description is None:
            update_values["description"] = description

        if isinstance(action_config, dict):
            update_values["action_config"] = action_config

        if isinstance(enabled, bool):
            update_values["enabled"] = enabled

        if isinstance(recurrence_rule, str):
            # Validate and calculate new next_scheduled_at
            next_scheduled_at = self._parse_rrule_and_get_next(recurrence_rule)
            if next_scheduled_at is None:
                raise ValueError(f"Invalid RRULE: {recurrence_rule}")

            update_values["recurrence_rule"] = recurrence_rule
            update_values["next_scheduled_at"] = next_scheduled_at

            # Cancel all pending task instances for this automation
            await self._cancel_pending_tasks(automation_id)

            # Schedule new first task with updated RRULE
            action_type = existing["action_type"]
            task_type = (
                "llm_callback" if action_type == "wake_llm" else "script_execution"
            )
            task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

            payload = {
                "conversation_id": conversation_id,
                "interface_type": existing["interface_type"],
                "automation_id": str(automation_id),
                "automation_type": "schedule",
            }

            # Add action-specific payload
            # Use updated action_config if provided, otherwise use existing
            final_action_config = (
                action_config
                if isinstance(action_config, dict)
                else existing["action_config"]
            )
            if action_type == "wake_llm":
                payload["callback_context"] = final_action_config.get("context", "")
            else:  # script
                payload["script_code"] = final_action_config.get("script_code", "")
                payload["task_name"] = final_action_config.get(
                    "task_name", existing["name"]
                )

            # Note: We do NOT pass recurrence_rule here because recurrence
            # is managed manually via after_task_execution callback, not
            # by the task worker's automatic recurrence system
            await enqueue_task(
                db_context=self._db,
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                scheduled_at=next_scheduled_at,
            )

            self._logger.info(
                f"Updated schedule automation {automation_id} RRULE, "
                f"next execution at {next_scheduled_at}"
            )

        if not update_values:
            self._logger.warning("No update values provided for automation update")
            return True

        stmt = (
            update(schedule_automations_table)
            .where(
                (schedule_automations_table.c.id == automation_id)
                & (schedule_automations_table.c.conversation_id == conversation_id)
            )
            .values(**update_values)
        )

        result = await self._db.execute_with_retry(stmt)
        updated_count = result.rowcount  # type: ignore[attr-defined]

        if updated_count > 0:
            self._logger.info(
                f"Updated schedule automation {automation_id} "
                f"for conversation {conversation_id}"
            )
            return True
        else:
            self._logger.error(
                f"Failed to update automation {automation_id} - update returned 0 rows"
            )
            return False

    async def delete(
        self,
        automation_id: int,
        conversation_id: str,
    ) -> bool:
        """
        Delete automation and cancel all pending task instances.

        Args:
            automation_id: Automation ID
            conversation_id: Conversation ID for verification

        Returns:
            True if deleted, False if not found
        """
        # Get automation for logging
        automation = await self.get_by_id(automation_id, conversation_id)
        if not automation:
            self._logger.warning(
                f"Schedule automation {automation_id} not found for conversation {conversation_id}"
            )
            return False

        # Cancel all pending tasks for this automation
        await self._cancel_pending_tasks(automation_id)

        # Delete the automation record
        stmt = delete(schedule_automations_table).where(
            (schedule_automations_table.c.id == automation_id)
            & (schedule_automations_table.c.conversation_id == conversation_id)
        )

        result = await self._db.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]

        if deleted_count > 0:
            self._logger.info(
                f"Deleted schedule automation '{automation['name']}' (ID: {automation_id}) "
                f"for conversation {conversation_id}"
            )
            return True
        else:
            self._logger.error(
                f"Failed to delete automation {automation_id} - deletion returned 0 rows"
            )
            return False

    async def _cancel_pending_tasks(self, automation_id: int) -> int:
        """
        Cancel all pending task instances for an automation.

        Args:
            automation_id: Automation ID

        Returns:
            Number of cancelled tasks
        """
        try:
            # Find pending tasks with this automation_id in payload
            payload_automation_id = sa_cast(
                tasks_table.c.payload["automation_id"], String
            )

            stmt = (
                update(tasks_table)
                .where(tasks_table.c.status == "pending")
                .where(payload_automation_id == str(automation_id))
                .values(status="cancelled")
            )

            result = await self._db.execute_with_retry(stmt)
            cancelled_count = result.rowcount  # type: ignore[attr-defined]

            if cancelled_count > 0:
                self._logger.info(
                    f"Cancelled {cancelled_count} pending tasks for automation {automation_id}"
                )

            return cancelled_count

        except SQLAlchemyError as e:
            self._logger.error(
                f"Error cancelling tasks for automation {automation_id}: {e}",
                exc_info=True,
            )
            return 0

    async def after_task_execution(
        self,
        automation_id: int,
        execution_time: datetime,
    ) -> None:
        """
        Update automation after task execution and schedule next instance.

        Args:
            automation_id: Automation ID
            execution_time: When the task executed
        """
        try:
            # Get the automation
            automation = await self.get_by_id(automation_id)
            if not automation:
                self._logger.warning(
                    f"Automation {automation_id} not found during after_task_execution"
                )
                return

            # Check if still enabled
            if not automation["enabled"]:
                self._logger.info(
                    f"Automation {automation_id} is disabled, not scheduling next instance"
                )
                return

            # Update execution stats
            stmt = (
                update(schedule_automations_table)
                .where(schedule_automations_table.c.id == automation_id)
                .values(
                    last_execution_at=execution_time,
                    execution_count=schedule_automations_table.c.execution_count + 1,
                )
            )
            await self._db.execute_with_retry(stmt)

            # Calculate next execution time
            recurrence_rule = automation["recurrence_rule"]
            next_scheduled_at = self._parse_rrule_and_get_next(
                recurrence_rule, after=execution_time
            )

            if next_scheduled_at is None:
                self._logger.info(
                    f"No more executions for automation {automation_id} "
                    f"based on RRULE {recurrence_rule}"
                )
                return

            # Update next_scheduled_at
            stmt = (
                update(schedule_automations_table)
                .where(schedule_automations_table.c.id == automation_id)
                .values(next_scheduled_at=next_scheduled_at)
            )
            await self._db.execute_with_retry(stmt)

            # Schedule next task instance
            action_type = automation["action_type"]
            task_type = (
                "llm_callback" if action_type == "wake_llm" else "script_execution"
            )
            task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

            payload = {
                "conversation_id": automation["conversation_id"],
                "interface_type": automation["interface_type"],
                "automation_id": str(automation_id),
                "automation_type": "schedule",
            }

            # Add action-specific payload
            action_config = automation["action_config"]
            if action_type == "wake_llm":
                payload["callback_context"] = action_config.get("context", "")
            else:  # script
                payload["script_code"] = action_config.get("script_code", "")
                payload["task_name"] = action_config.get(
                    "task_name", automation["name"]
                )

            # Note: We do NOT pass recurrence_rule here because recurrence
            # is managed manually via after_task_execution callback, not
            # by the task worker's automatic recurrence system
            await enqueue_task(
                db_context=self._db,
                task_id=task_id,
                task_type=task_type,
                payload=payload,
                scheduled_at=next_scheduled_at,
            )

            self._logger.info(
                f"Scheduled next task for automation {automation_id} at {next_scheduled_at}"
            )

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in after_task_execution for automation {automation_id}: {e}",
                exc_info=True,
            )
            # Don't raise - we don't want task execution failures

    async def get_execution_stats(
        self,
        automation_id: int,
    ) -> dict[str, Any]:
        """
        Get execution statistics for an automation.

        Args:
            automation_id: Automation ID

        Returns:
            Dictionary with execution statistics
        """
        try:
            automation = await self.get_by_id(automation_id)
            if not automation:
                return {}

            # Query tasks table for execution history
            # The .astext accessor works across both SQLite and PostgreSQL
            stmt = select(tasks_table).where(
                tasks_table.c.payload["automation_id"].astext == str(automation_id)
            )

            stmt = stmt.where(tasks_table.c.status.in_(["completed", "failed"]))
            stmt = stmt.order_by(tasks_table.c.created_at.desc()).limit(10)

            recent_executions = await self._db.fetch_all(stmt)

            return {
                "total_executions": automation["execution_count"],
                "last_execution_at": automation["last_execution_at"],
                "next_scheduled_at": automation["next_scheduled_at"],
                "recent_executions": [dict(row) for row in recent_executions],
            }

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_execution_stats for automation {automation_id}: {e}",
                exc_info=True,
            )
            raise
