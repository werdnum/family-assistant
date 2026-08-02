"""Repository for schedule-based automations operations."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from dateutil import rrule
from dateutil.parser import ParserError
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.storage.database import DatabaseExecutor, DatabaseTransaction
from family_assistant.storage.datetime_utils import normalize_datetime
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.schedule_automations import schedule_automations_table
from family_assistant.storage.tasks import enqueue_task, tasks_table
from family_assistant.storage.types import (
    ActionConfig,
    ScheduleAutomationDict,
    ScheduleExecutionStatsDict,
)
from family_assistant.task_worker import (
    SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY,
    SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
    LlmCallbackPayload,
    ScriptExecutionPayload,
)

# Sentinel to distinguish "not provided" from "explicitly None"
_UNSET = object()

# Valid action types for schedule automations
VALID_ACTION_TYPES = {"wake_llm", "script"}


def _build_script_payload(
    action_config: ActionConfig,
    conversation_id: str,
    interface_type: str,
    automation_id: str,
    task_name: str,
    processing_profile_id: str | None = None,
    created_by_user_id: str | None = None,
) -> ScriptExecutionPayload:
    """Build a ScriptExecutionPayload from action_config, supporting both inline and stored scripts."""
    payload = ScriptExecutionPayload(
        conversation_id=conversation_id,
        interface_type=interface_type,
        automation_id=automation_id,
        automation_type="schedule",
        task_name=action_config.get("task_name", task_name),
        config=dict(action_config),
    )
    if processing_profile_id is not None:
        payload["processing_profile_id"] = processing_profile_id
    if created_by_user_id is not None:
        payload["created_by_user_id"] = created_by_user_id
    script_name = action_config.get("script_name")
    if script_name:
        payload["script_name"] = script_name
        params = action_config.get("parameters")
        if params:
            payload["script_parameters"] = params
    else:
        payload["script_code"] = action_config.get("script_code", "")
    return payload


class ScheduleAutomationsRepository(BaseRepository):
    """Repository for managing schedule-based automations."""

    def _normalize_automation(self, row: Mapping[str, Any]) -> ScheduleAutomationDict:
        """
        Normalize a database row to ScheduleAutomationDict.

        Ensures all datetime fields are timezone-aware UTC datetimes.

        Args:
            row: Raw database row as dict

        Returns:
            Normalized ScheduleAutomationDict
        """
        created_at = normalize_datetime(row["created_at"])
        if created_at is None:
            raise ValueError("created_at cannot be None for automation record")

        return ScheduleAutomationDict(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            conversation_id=row["conversation_id"],
            interface_type=row["interface_type"],
            recurrence_rule=row["recurrence_rule"],
            next_scheduled_at=normalize_datetime(row["next_scheduled_at"]),
            action_type=row["action_type"],
            action_config=row["action_config"],
            enabled=row["enabled"],
            processing_profile_id=row.get("processing_profile_id"),
            created_by_user_id=row.get("created_by_user_id"),
            created_at=created_at,
            last_execution_at=normalize_datetime(row["last_execution_at"]),
            execution_count=row["execution_count"],
        )

    def _parse_rrule_and_get_next(
        self,
        recurrence_rule: str,
        after: datetime | None = None,
        *,
        timezone: ZoneInfo,
    ) -> datetime | None:
        """
        Parse RRULE and calculate next execution time.

        Times in the RRULE (e.g. BYHOUR=9) are interpreted in the given
        timezone.  The returned datetime is always UTC so it can be stored
        directly in the database.

        Args:
            recurrence_rule: RRULE string
            after: Calculate next execution after this time (defaults to now)
            timezone: Interpret RRULE times in this timezone.
                ``after`` is converted to this timezone before being used as
                the RRULE dtstart so that hour/minute constraints are
                evaluated in local time.

        Returns:
            Next execution datetime in UTC, or None if no more executions
        """
        try:
            tz = timezone
            if after is None:
                after = datetime.now(tz)
            else:
                if after.tzinfo is None:
                    after = after.replace(tzinfo=UTC)
                after = after.astimezone(tz)

            # Parse the RRULE — dtstart is in the user's timezone so that
            # BYHOUR/BYMINUTE are evaluated in local time.
            rule = rrule.rrulestr(recurrence_rule, dtstart=after)

            # Get the next occurrence (in the user's timezone)
            next_occurrence = rule.after(after)

            if next_occurrence is None:
                return None

            # Convert to UTC for storage
            return next_occurrence.astimezone(UTC)
        except (ValueError, ParserError) as e:
            self._logger.error(f"Failed to parse RRULE '{recurrence_rule}': {e}")
            return None

    async def create(
        self,
        name: str,
        recurrence_rule: str,
        action_type: str,
        action_config: ActionConfig,
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
        enabled: bool = True,
        *,
        timezone: ZoneInfo,
        processing_profile_id: str | None = None,
        created_by_user_id: str | None = None,
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
            # Validate action_type
            if action_type not in VALID_ACTION_TYPES:
                raise ValueError(
                    f"Invalid action_type '{action_type}'. Must be one of: {', '.join(sorted(VALID_ACTION_TYPES))}"
                )

            # Calculate first execution time
            next_scheduled_at = self._parse_rrule_and_get_next(
                recurrence_rule, timezone=timezone
            )
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
                    processing_profile_id=processing_profile_id,
                    created_by_user_id=created_by_user_id,
                    created_at=datetime.now(UTC),
                    execution_count=0,
                )
                .returning(schedule_automations_table.c.id)
            )

            result = await self._db.execute(stmt)
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

            if action_type == "wake_llm":
                payload: LlmCallbackPayload | ScriptExecutionPayload = (
                    LlmCallbackPayload(
                        conversation_id=conversation_id,
                        interface_type=interface_type,
                        automation_id=str(automation_id),
                        automation_type="schedule",
                        callback_context=action_config.get("context", ""),
                        scheduling_timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                if created_by_user_id is not None:
                    payload["created_by_user_id"] = created_by_user_id
                # Scheduled wakes run under their originating profile (a trusted,
                # user-set-up trigger), honored by handle_llm_callback.
                if processing_profile_id is not None:
                    payload["processing_profile_id"] = processing_profile_id
            else:  # script
                payload = _build_script_payload(
                    action_config=action_config,
                    conversation_id=conversation_id,
                    interface_type=interface_type,
                    automation_id=str(automation_id),
                    task_name=name,
                    processing_profile_id=processing_profile_id,
                    created_by_user_id=created_by_user_id,
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
            self._logger.exception(f"Database error in create_schedule_automation: {e}")
            raise

    async def create_full(
        self,
        name: str,
        recurrence_rule: str,
        action_type: str,
        action_config: ActionConfig,
        conversation_id: str,
        interface_type: str = "telegram",
        description: str | None = None,
        *,
        timezone: ZoneInfo,
    ) -> ScheduleAutomationDict:
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
            timezone=timezone,
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
    ) -> ScheduleAutomationDict | None:
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

        return self._normalize_automation(dict(row))

    async def get_by_name(
        self, name: str, conversation_id: str
    ) -> ScheduleAutomationDict | None:
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

        return self._normalize_automation(dict(row))

    async def list_all(
        self,
        conversation_id: str,
        enabled_only: bool = False,
    ) -> list[ScheduleAutomationDict]:
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
        return [self._normalize_automation(dict(row)) for row in rows]

    async def update_enabled(
        self,
        automation_id: int,
        conversation_id: str,
        enabled: bool,
        *,
        timezone: ZoneInfo,
    ) -> bool:
        """
        Enable or disable automation, synchronizing task queue accordingly.

        When disabling, cancels all pending task queue items.
        When enabling, schedules a new task based on the current RRULE.

        When re-enabling, recalculates next_scheduled_at from now and
        reschedules the task so the automation fires at the correct time.

        Args:
            automation_id: Automation ID
            conversation_id: Conversation ID for verification
            enabled: New enabled status
            timezone: User's timezone for interpreting RRULE times when
                re-enabling

        Returns:
            True if updated, False if not found
        """

        async def _apply(txn: DatabaseTransaction) -> bool:
            """Cancel stale tasks, (re)schedule, and flip the flag as one unit.

            Split, a failure between them leaves an automation that reads
            enabled with all its tasks cancelled -- enabled and permanently
            dead -- or disabled while still firing on schedule.
            """
            # When enabling, fetch the automation so we can reschedule
            if enabled:
                automation = await txn.schedule_automations.get_by_id(
                    automation_id, conversation_id
                )
                if not automation:
                    self._logger.warning(
                        f"Schedule automation {automation_id} not found "
                        f"for conversation {conversation_id}"
                    )
                    return False

                next_scheduled_at = self._parse_rrule_and_get_next(
                    automation["recurrence_rule"], timezone=timezone
                )
                if next_scheduled_at is None:
                    self._logger.error(
                        f"Cannot enable automation {automation_id}: "
                        f"RRULE '{automation['recurrence_rule']}' yields no future occurrences"
                    )
                    raise ValueError(
                        f"Cannot enable: RRULE '{automation['recurrence_rule']}' "
                        "yields no future occurrences"
                    )

                # Cancel stale pending tasks and schedule a fresh one
                await self._cancel_pending_tasks(automation_id, db=txn)

                action_type = automation["action_type"]
                task_type = (
                    "llm_callback" if action_type == "wake_llm" else "script_execution"
                )
                task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

                action_config = automation["action_config"]
                if action_type == "wake_llm":
                    enqueue_payload: LlmCallbackPayload | ScriptExecutionPayload = (
                        LlmCallbackPayload(
                            conversation_id=conversation_id,
                            interface_type=automation["interface_type"],
                            automation_id=str(automation_id),
                            automation_type="schedule",
                            callback_context=action_config.get("context", ""),
                            scheduling_timestamp=datetime.now(UTC).isoformat(),
                        )
                    )
                    created_by = automation.get("created_by_user_id")
                    if created_by is not None:
                        enqueue_payload["created_by_user_id"] = created_by
                    enable_profile_id = automation.get("processing_profile_id")
                    if enable_profile_id is not None:
                        enqueue_payload["processing_profile_id"] = enable_profile_id
                else:
                    enqueue_payload = _build_script_payload(
                        action_config=action_config,
                        conversation_id=conversation_id,
                        interface_type=automation["interface_type"],
                        automation_id=str(automation_id),
                        task_name=automation["name"],
                        processing_profile_id=automation.get("processing_profile_id"),
                        created_by_user_id=automation.get("created_by_user_id"),
                    )

                await enqueue_task(
                    db_context=txn,
                    task_id=task_id,
                    task_type=task_type,
                    payload=enqueue_payload,
                    scheduled_at=next_scheduled_at,
                )

                stmt = (
                    update(schedule_automations_table)
                    .where(
                        (schedule_automations_table.c.id == automation_id)
                        & (
                            schedule_automations_table.c.conversation_id
                            == conversation_id
                        )
                    )
                    .values(enabled=True, next_scheduled_at=next_scheduled_at)
                )
                await txn.execute(stmt)

                self._logger.info(
                    f"Enabled schedule automation {automation_id}, "
                    f"next execution at {next_scheduled_at}"
                )
                return True

            # Disabling — cancel pending tasks and update
            await self._cancel_pending_tasks(automation_id, db=txn)

            stmt = (
                update(schedule_automations_table)
                .where(
                    (schedule_automations_table.c.id == automation_id)
                    & (schedule_automations_table.c.conversation_id == conversation_id)
                )
                .values(enabled=False)
            )

            result = await txn.execute(stmt)
            updated_count = result.rowcount

            if updated_count > 0:
                self._logger.info(f"Disabled schedule automation {automation_id}")
                return True
            else:
                self._logger.warning(
                    f"Schedule automation {automation_id} not found for conversation {conversation_id}"
                )
                return False

        return await self._db.atomic(_apply)

    async def update(
        self,
        automation_id: int,
        conversation_id: str,
        name: str | None | object = _UNSET,
        recurrence_rule: str | None | object = _UNSET,
        action_config: ActionConfig | None | object = _UNSET,
        description: str | None | object = _UNSET,
        enabled: bool | None | object = _UNSET,
        *,
        timezone: ZoneInfo,
        processing_profile_id: str | None | object = _UNSET,
        created_by_user_id: str | None | object = _UNSET,
    ) -> bool:
        """
        Update automation configuration, synchronizing task queue as needed.

        When task-affecting fields change (recurrence_rule, action_config, enabled),
        pending task queue items are cancelled and rescheduled with the new config.

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

        # ast-grep-ignore: no-dict-any - SQLAlchemy update values dict has heterogeneous column types
        update_values: dict[str, Any] = {}

        if isinstance(name, str) or name is None:
            update_values["name"] = name

        if isinstance(description, str) or description is None:
            update_values["description"] = description

        if isinstance(action_config, dict):
            update_values["action_config"] = action_config

        if isinstance(enabled, bool):
            update_values["enabled"] = enabled

        # Re-stamp creator provenance when supplied so the updated script
        # executes under the updating profile's tools. Mutate ``existing`` too so
        # the resynced task payload below carries the new profile.
        provenance_changing = (
            isinstance(processing_profile_id, str)
            and processing_profile_id != existing["processing_profile_id"]
        ) or (
            isinstance(created_by_user_id, str)
            and created_by_user_id != existing["created_by_user_id"]
        )
        if isinstance(processing_profile_id, str):
            update_values["processing_profile_id"] = processing_profile_id
            existing["processing_profile_id"] = processing_profile_id
        if isinstance(created_by_user_id, str):
            update_values["created_by_user_id"] = created_by_user_id
            existing["created_by_user_id"] = created_by_user_id

        recurrence_changing = isinstance(recurrence_rule, str)
        if recurrence_changing:
            # Validate and calculate new next_scheduled_at
            next_at = self._parse_rrule_and_get_next(recurrence_rule, timezone=timezone)
            if next_at is None:
                raise ValueError(f"Invalid RRULE: {recurrence_rule}")
            update_values["recurrence_rule"] = recurrence_rule
            update_values["next_scheduled_at"] = next_at

        # Determine if task queue needs synchronization
        action_config_changing = (
            isinstance(action_config, dict)
            and action_config != existing["action_config"]
        )
        enabled_changing = isinstance(enabled, bool) and enabled != existing["enabled"]
        name_changing = isinstance(name, str) and name != existing["name"]
        name_affects_task = (
            name_changing
            and existing["action_type"] == "script"
            and "task_name" not in (existing["action_config"] or {})
        )

        needs_task_sync = (
            recurrence_changing
            or action_config_changing
            or enabled_changing
            or name_affects_task
            # Re-stamping provenance must rebuild the pending task too, otherwise
            # the already-enqueued payload keeps the old profile/user until it
            # fires once under the wrong profile.
            or provenance_changing
        )

        if needs_task_sync:
            will_be_enabled = (
                enabled if isinstance(enabled, bool) else existing["enabled"]
            )

            next_scheduled_at = await self._sync_pending_tasks(
                automation_id,
                existing,
                enabled=will_be_enabled,
                action_config_override=cast("ActionConfig", action_config)
                if isinstance(action_config, dict)
                else None,
                recurrence_rule_override=recurrence_rule
                if recurrence_changing
                else None,
                name_override=name if isinstance(name, str) else None,
                timezone=timezone,
                next_at_override=next_at if recurrence_changing else None,
            )

            if (
                next_scheduled_at is not None
                and "next_scheduled_at" not in update_values
            ):
                update_values["next_scheduled_at"] = next_scheduled_at

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

        result = await self._db.execute(stmt)
        updated_count = result.rowcount

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

        result = await self._db.execute(stmt)
        deleted_count = result.rowcount

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

    async def _cancel_pending_tasks(
        self, automation_id: int, db: DatabaseExecutor | None = None
    ) -> int:
        """
        Cancel all pending task instances for an automation.

        Args:
            automation_id: Automation ID
            db: Optional DatabaseTransaction to use instead of self._db. When
                provided, marks and updates tasks within the caller's transaction.

        Returns:
            Number of cancelled tasks
        """
        executor = db if db is not None else self._db
        try:
            await self._mark_pending_advance_tasks_stats_only(automation_id, executor)
            await self._mark_persisted_advance_outboxes_stats_only(
                automation_id, executor
            )

            # Find pending tasks with this automation_id in payload
            stmt = (
                update(tasks_table)
                .where(tasks_table.c.status == "pending")
                .where(
                    tasks_table.c.payload["automation_id"].as_string()
                    == str(automation_id)
                )
                .where(tasks_table.c.task_type != SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE)
                .values(status="cancelled")
            )

            result = await executor.execute(stmt)
            cancelled_count = result.rowcount

            if cancelled_count > 0:
                self._logger.info(
                    f"Cancelled {cancelled_count} pending tasks for automation {automation_id}"
                )

            return cancelled_count

        except SQLAlchemyError as e:
            self._logger.exception(
                f"Error cancelling tasks for automation {automation_id}: {e}"
            )
            return 0

    async def _mark_pending_advance_tasks_stats_only(
        self, automation_id: int, db: DatabaseExecutor | None = None
    ) -> int:
        """Prevent preserved terminal-stat tasks from scheduling a future run."""
        executor = db if db is not None else self._db
        stmt = (
            select(tasks_table.c.task_id, tasks_table.c.payload)
            .where(tasks_table.c.status == "pending")
            .where(tasks_table.c.task_type == SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE)
            .where(
                tasks_table.c.payload["automation_id"].as_string() == str(automation_id)
            )
        )
        rows = await executor.fetch_all(stmt)
        marked_count = 0
        for row in rows:
            payload = dict(row["payload"] or {})
            if payload.get("schedule_next") is False:
                continue
            payload["schedule_next"] = False
            update_stmt = (
                update(tasks_table)
                .where(tasks_table.c.task_id == row["task_id"])
                .values(payload=payload)
            )
            await executor.execute(update_stmt)
            marked_count += 1

        if marked_count > 0:
            self._logger.info(
                f"Marked {marked_count} pending schedule advancement tasks "
                f"as stats-only for automation {automation_id}"
            )
        return marked_count

    async def _mark_persisted_advance_outboxes_stats_only(
        self, automation_id: int, db: DatabaseExecutor | None = None
    ) -> int:
        """Prevent persisted source-task outboxes from scheduling a future run."""
        executor = db if db is not None else self._db
        stmt = (
            select(tasks_table.c.task_id, tasks_table.c.payload)
            .where(tasks_table.c.status.in_(["done", "failed"]))
            .where(tasks_table.c.task_type.in_(["llm_callback", "script_execution"]))
            .where(
                tasks_table.c.payload["automation_id"].as_string() == str(automation_id)
            )
        )
        rows = await executor.fetch_all(stmt)
        marked_count = 0
        for row in rows:
            payload = dict(row["payload"] or {})
            outbox = payload.get(SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY)
            if not isinstance(outbox, dict) or outbox.get("schedule_next") is False:
                continue
            updated_outbox = dict(outbox)
            updated_outbox["schedule_next"] = False
            payload[SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY] = updated_outbox
            update_stmt = (
                update(tasks_table)
                .where(tasks_table.c.task_id == row["task_id"])
                .values(payload=payload)
            )
            await executor.execute(update_stmt)
            marked_count += 1

        if marked_count > 0:
            self._logger.info(
                f"Marked {marked_count} persisted schedule advancement outboxes "
                f"as stats-only for automation {automation_id}"
            )
        return marked_count

    async def _sync_pending_tasks(
        self,
        automation_id: int,
        automation: ScheduleAutomationDict,
        enabled: bool,
        action_config_override: ActionConfig | None = None,
        recurrence_rule_override: str | None = None,
        name_override: str | None = None,
        *,
        timezone: ZoneInfo,
        next_at_override: datetime | None = None,
    ) -> datetime | None:
        """
        Synchronize pending task queue items with automation state.

        Cancels all pending tasks and, if the automation is enabled,
        schedules a new task with the current configuration.

        Args:
            automation_id: Automation ID
            automation: Current automation data from DB
            enabled: Whether the automation will be enabled after the update
            action_config_override: New action_config (if changing), otherwise uses existing
            recurrence_rule_override: New recurrence_rule (if changing), otherwise uses existing
            name_override: New name (if changing), otherwise uses existing
            timezone: User's timezone for interpreting RRULE times
            next_at_override: Pre-calculated next execution time. When provided,
                skips recalculation to avoid race conditions from clock drift
                between validation and scheduling.

        Returns:
            The next_scheduled_at datetime if a task was scheduled, None otherwise
        """
        await self._cancel_pending_tasks(automation_id)

        if not enabled:
            return None

        final_recurrence_rule = (
            recurrence_rule_override
            if recurrence_rule_override is not None
            else automation["recurrence_rule"]
        )
        final_action_config = (
            action_config_override
            if action_config_override is not None
            else automation["action_config"]
        )
        final_name = name_override if name_override is not None else automation["name"]

        next_scheduled_at = next_at_override or self._parse_rrule_and_get_next(
            final_recurrence_rule, timezone=timezone
        )
        if next_scheduled_at is None:
            self._logger.info(
                f"No future executions for automation {automation_id} "
                f"based on RRULE {final_recurrence_rule}"
            )
            return None

        action_type = automation["action_type"]
        task_type = "llm_callback" if action_type == "wake_llm" else "script_execution"
        task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

        if action_type == "wake_llm":
            payload: LlmCallbackPayload | ScriptExecutionPayload = LlmCallbackPayload(
                conversation_id=automation["conversation_id"],
                interface_type=automation["interface_type"],
                automation_id=str(automation_id),
                automation_type="schedule",
                callback_context=final_action_config.get("context", ""),
                scheduling_timestamp=datetime.now(UTC).isoformat(),
            )
            created_by = automation.get("created_by_user_id")
            if created_by is not None:
                payload["created_by_user_id"] = created_by
            reschedule_profile_id = automation.get("processing_profile_id")
            if reschedule_profile_id is not None:
                payload["processing_profile_id"] = reschedule_profile_id
        else:  # script
            payload = _build_script_payload(
                action_config=final_action_config,
                conversation_id=automation["conversation_id"],
                interface_type=automation["interface_type"],
                automation_id=str(automation_id),
                task_name=final_name,
                processing_profile_id=automation.get("processing_profile_id"),
                created_by_user_id=automation.get("created_by_user_id"),
            )

        await enqueue_task(
            db_context=self._db,
            task_id=task_id,
            task_type=task_type,
            payload=payload,
            scheduled_at=next_scheduled_at,
        )

        self._logger.info(
            f"Scheduled task for automation {automation_id} at {next_scheduled_at}"
        )
        return next_scheduled_at

    async def after_task_execution(
        self,
        automation_id: int,
        execution_time: datetime,
        *,
        timezone: ZoneInfo,
        schedule_next: bool = True,
    ) -> None:
        """
        Update automation after task execution and schedule next instance.

        Args:
            automation_id: Automation ID
            execution_time: When the task executed
            timezone: User's timezone for interpreting RRULE times
            schedule_next: Whether to schedule the next run after recording
                terminal execution stats
        """
        try:
            normalized_execution_time = normalize_datetime(execution_time)
            if normalized_execution_time is None:
                raise ValueError("execution_time must be a valid datetime")
            execution_time = normalized_execution_time

            # Get the automation
            automation = await self.get_by_id(automation_id)
            if not automation:
                self._logger.warning(
                    f"Automation {automation_id} not found during after_task_execution"
                )
                return

            last_execution_at = automation["last_execution_at"]
            recorded_execution_time = (
                last_execution_at
                if last_execution_at is not None and last_execution_at > execution_time
                else execution_time
            )

            # Always update execution stats, regardless of enabled status.
            # The execution happened, so it should be recorded without moving
            # last_execution_at backward if an older stats-only advance arrives late.
            stmt = (
                update(schedule_automations_table)
                .where(schedule_automations_table.c.id == automation_id)
                .values(
                    last_execution_at=recorded_execution_time,
                    execution_count=schedule_automations_table.c.execution_count + 1,
                )
            )
            await self._db.execute(stmt)

            if not schedule_next:
                self._logger.info(
                    f"Recorded terminal stats for automation {automation_id} "
                    "without scheduling next instance"
                )
                return

            # Check if still enabled before scheduling next instance
            if not automation["enabled"]:
                self._logger.info(
                    f"Automation {automation_id} is disabled, not scheduling next instance"
                )
                return

            # Calculate next execution time
            recurrence_rule = automation["recurrence_rule"]
            next_scheduled_at = self._parse_rrule_and_get_next(
                recurrence_rule, after=execution_time, timezone=timezone
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
            await self._db.execute(stmt)

            # Schedule next task instance
            action_type = automation["action_type"]
            task_type = (
                "llm_callback" if action_type == "wake_llm" else "script_execution"
            )
            task_id = f"sched_auto_{automation_id}_{uuid.uuid4().hex[:8]}"

            action_config = automation["action_config"]
            if action_type == "wake_llm":
                recur_payload: LlmCallbackPayload | ScriptExecutionPayload = (
                    LlmCallbackPayload(
                        conversation_id=automation["conversation_id"],
                        interface_type=automation["interface_type"],
                        automation_id=str(automation_id),
                        automation_type="schedule",
                        callback_context=action_config.get("context", ""),
                        scheduling_timestamp=datetime.now(UTC).isoformat(),
                    )
                )
                created_by = automation.get("created_by_user_id")
                if created_by is not None:
                    recur_payload["created_by_user_id"] = created_by
                recur_profile_id = automation.get("processing_profile_id")
                if recur_profile_id is not None:
                    recur_payload["processing_profile_id"] = recur_profile_id
            else:  # script
                recur_payload = _build_script_payload(
                    action_config=action_config,
                    conversation_id=automation["conversation_id"],
                    interface_type=automation["interface_type"],
                    automation_id=str(automation_id),
                    task_name=automation["name"],
                    processing_profile_id=automation.get("processing_profile_id"),
                    created_by_user_id=automation.get("created_by_user_id"),
                )

            # Note: We do NOT pass recurrence_rule here because recurrence
            # is managed manually via after_task_execution callback, not
            # by the task worker's automatic recurrence system
            await enqueue_task(
                db_context=self._db,
                task_id=task_id,
                task_type=task_type,
                payload=recur_payload,
                scheduled_at=next_scheduled_at,
            )

            self._logger.info(
                f"Scheduled next task for automation {automation_id} at {next_scheduled_at}"
            )

        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in after_task_execution for automation {automation_id}: {e}"
            )
            raise

    async def get_execution_stats(
        self,
        automation_id: int,
    ) -> ScheduleExecutionStatsDict | None:
        """
        Get execution statistics for an automation.

        Args:
            automation_id: Automation ID

        Returns:
            Dictionary with execution statistics, or None if not found
        """
        try:
            automation = await self.get_by_id(automation_id)
            if not automation:
                return None

            # Query tasks table for execution history
            stmt = select(tasks_table).where(
                tasks_table.c.payload["automation_id"].as_string() == str(automation_id)
            )

            stmt = stmt.where(tasks_table.c.status.in_(["completed", "failed"]))
            stmt = stmt.order_by(tasks_table.c.created_at.desc()).limit(10)

            recent_executions = await self._db.fetch_all(stmt)

            return ScheduleExecutionStatsDict(
                total_executions=automation["execution_count"],
                last_execution_at=automation["last_execution_at"],
                next_scheduled_at=automation["next_scheduled_at"],
                recent_executions=recent_executions,
            )

        except SQLAlchemyError as e:
            self._logger.exception(
                f"Database error in get_execution_stats for automation {automation_id}: {e}"
            )
            raise
