"""Repository for tasks storage operations."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, case, delete, insert, null, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import functions as func
from sqlalchemy.sql.elements import ColumnElement

from family_assistant.security.definition_records import (
    CreationDisposition,
    GateProvenance,
    definition_record_from_row,
)
from family_assistant.storage.database import DatabaseTransaction
from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.tasks import notify_workers, tasks_table
from family_assistant.storage.types import TaskDict

logger = logging.getLogger(__name__)

# Statuses a system task's row can be in when it has finished with the
# occurrence it holds, and so can be handed the next one.
TERMINAL_TASK_STATUSES = ("done", "failed")


def _revive_if_terminal(
    column_name: str,
    revived_value: object,
) -> ColumnElement[Any]:
    """An UPDATE value that resets a finished occurrence and leaves others alone.

    A system task keeps one row across occurrences, so the upsert that schedules
    the next one has to hand that row back to the queue: without this the row
    stays ``done`` after its first run and dequeue, which selects only pending
    (or stale processing) rows, never picks it up again.

    A row that is still ``pending`` or ``processing`` keeps its value — a
    startup upsert must not resurrect an occurrence a worker is running, or
    reset the retry count of one that is mid-retry.
    """
    return case(
        (tasks_table.c.status.in_(TERMINAL_TASK_STATUSES), revived_value),
        else_=tasks_table.c[column_name],
    )


def _revived_occurrence_values() -> dict[str, ColumnElement[Any]]:
    """The columns an upserted system task resets when its occurrence is over."""
    return {
        "status": _revive_if_terminal("status", "pending"),
        "retry_count": _revive_if_terminal("retry_count", 0),
        "locked_by": _revive_if_terminal("locked_by", null()),
        "locked_at": _revive_if_terminal("locked_at", null()),
        "error": _revive_if_terminal("error", null()),
    }


class TasksRepository(BaseRepository):
    """Repository for managing background tasks in the database queue."""

    async def enqueue(
        self,
        task_id: str,
        task_type: str,
        # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
        payload: Mapping[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        max_retries_override: int | None = None,
        recurrence_rule: str | None = None,
        original_task_id: str | None = None,
        only_if_absent: bool = False,
    ) -> None:
        """Adds a task to the queue with automatic notification for immediate tasks.

        Args:
            task_id: Unique identifier for the task
            task_type: Type of task (determines which handler processes it)
            payload: Optional data payload for the task
            scheduled_at: When to run the task (None = immediate)
            max_retries_override: Override default max retries
            recurrence_rule: Optional recurrence rule for repeating tasks
            original_task_id: ID of the original task if this is a recurrence
            only_if_absent: Seed the row and leave an existing one alone, rather
                than upserting it. System tasks only -- a non-system enqueue
                already fails on a duplicate id. Use this where the row's
                payload carries progress the caller does not have: the default
                upsert overwrites the payload and revives a finished
                occurrence, which rewinds a cursor back to its starting value
                every time the seeding caller runs.
        """
        if only_if_absent and not task_id.startswith("system_"):
            raise ValueError(
                "only_if_absent applies to system tasks, which upsert on their "
                f"task_id; '{task_id}' is not one."
            )
        processed_scheduled_at = scheduled_at
        if processed_scheduled_at:
            if processed_scheduled_at.tzinfo is None:
                raise ValueError("scheduled_at must be timezone-aware")
            # Convert to UTC if it's aware and not already UTC
            if processed_scheduled_at.tzinfo != UTC:
                logger.debug(
                    f"Converting scheduled_at for task {task_id} from {processed_scheduled_at.tzinfo} to UTC."
                )
                processed_scheduled_at = processed_scheduled_at.astimezone(UTC)

        max_task_retries = (
            max_retries_override if max_retries_override is not None else 3
        )

        values_to_insert = {
            "task_id": task_id,
            "task_type": task_type,
            "payload": payload,
            "scheduled_at": processed_scheduled_at,  # Use the processed version
            "status": "pending",
            "retry_count": 0,
            "max_retries": max_task_retries,
            "recurrence_rule": recurrence_rule,
            "original_task_id": original_task_id if original_task_id else task_id,
        }
        # Filter out None values unless they are allowed (payload, error)
        values_to_insert = {
            k: v
            for k, v in values_to_insert.items()
            if v is not None or k in {"payload", "error"}
        }

        # Check if this is a system task (starts with "system_")
        is_system_task = task_id.startswith("system_")

        async def _enqueue(txn: DatabaseTransaction) -> None:
            """Write the row and arm the worker wake, as one unit.

            The SQLite branch reads before it writes, and the wake must not fire
            before the row is visible -- an idle sibling would poll an empty
            queue, clear its event, and miss the row until the next 5s poll.
            """
            if only_if_absent:
                # Seed-only: the existing row owns its payload and status.
                insert_fn = (
                    pg_insert if txn.dialect_name == "postgresql" else sqlite_insert
                )
                seed_stmt = (
                    insert_fn(tasks_table)
                    .values(**values_to_insert)
                    .on_conflict_do_nothing(index_elements=["task_id"])
                )
                # A driver may report -1 for an unknown rowcount. Treat that as
                # seeded: an extra worker wake costs one empty poll, while a
                # missed one leaves a fresh row sitting until the next 5s tick.
                seeded = (await txn.execute(seed_stmt)).rowcount != 0
                if not seeded:
                    logger.info(
                        f"Task {task_id} already seeded; leaving its payload and "
                        "status as they stand."
                    )
                    return
                stmt = None
            elif is_system_task:
                # For system tasks, do an upsert to handle re-scheduling
                if txn.dialect_name == "postgresql":
                    # PostgreSQL: Use ON CONFLICT DO UPDATE
                    stmt = pg_insert(tasks_table).values(**values_to_insert)
                    # Only update fields that might change for system tasks
                    update_dict = {
                        "scheduled_at": stmt.excluded.scheduled_at,
                        "payload": stmt.excluded.payload,
                        "max_retries": stmt.excluded.max_retries,
                        "recurrence_rule": stmt.excluded.recurrence_rule,
                        **_revived_occurrence_values(),
                    }
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["task_id"],  # The unique constraint column
                        set_=update_dict,
                    )
                else:
                    # SQLite fallback: try UPDATE first, then INSERT
                    update_stmt = (
                        update(tasks_table)
                        .where(tasks_table.c.task_id == task_id)
                        .values(
                            scheduled_at=processed_scheduled_at,
                            payload=payload,
                            max_retries=max_task_retries,
                            recurrence_rule=recurrence_rule,
                            **_revived_occurrence_values(),
                        )
                    )
                    result = await txn.execute(update_stmt)
                    if result.rowcount == 0:
                        # Task doesn't exist, do INSERT
                        stmt = insert(tasks_table).values(**values_to_insert)
                    else:
                        # Update succeeded, skip INSERT
                        stmt = None
            else:
                # For non-system tasks, just do a regular INSERT
                stmt = insert(tasks_table).values(**values_to_insert)

            if stmt is not None:
                await txn.execute(stmt)

            # If task is immediate, wake all workers in the pool. Fanning out to
            # every registered per-worker wake event (plus the legacy global event)
            # avoids one worker's event.clear() swallowing the wakeup for siblings.
            # Defer the wake to transaction commit (mirroring
            # storage.tasks.enqueue_task): when enqueue runs inside an open
            # transaction, waking before commit can let an idle sibling poll an
            # empty queue, clear its event, and then miss the not-yet-visible row
            # until the next 5s poll.
            if not processed_scheduled_at or processed_scheduled_at <= datetime.now(
                UTC
            ):
                txn.on_commit(notify_workers)

            logger.info(
                f"Successfully enqueued task: {task_id} (type: {task_type}, scheduled: {processed_scheduled_at})"
            )

        try:
            await self._db.atomic(_enqueue)
        except IntegrityError as e:
            # For non-system tasks, this is an error
            if not is_system_task:
                logger.exception(
                    f"ENQUEUE FAILED: Task with ID '{task_id}' already exists in the queue: {e}"
                )
                raise RuntimeError(f"Task ID '{task_id}' already exists") from e
            else:
                # For system tasks, integrity error during PostgreSQL upsert shouldn't happen
                logger.exception(
                    f"Unexpected integrity error for system task '{task_id}': {e}"
                )
                raise
        except SQLAlchemyError as e:
            logger.exception(f"Database error enqueueing task {task_id}: {e}")
            raise

    async def delete_finished(self, task_id: str) -> bool:
        """Remove a task row only if its occurrence is over.

        Pairs with ``only_if_absent``: seeding is idempotent because the row
        exists, so a caller that genuinely needs the work redone clears the
        finished row first. A pending or running occurrence is left alone --
        it is already doing the work, and deleting it would lose its cursor.
        """
        stmt = delete(tasks_table).where(
            tasks_table.c.task_id == task_id,
            tasks_table.c.status.in_(TERMINAL_TASK_STATUSES),
        )
        return (await self._db.execute(stmt)).rowcount > 0

    async def dequeue(
        self,
        worker_id: str,
        task_types: list[str],
        current_time: datetime,
    ) -> TaskDict | None:
        """
        Atomically dequeues the next available task for a worker.

        Args:
            worker_id: Unique identifier for the worker
            task_types: List of task types this worker can handle
            current_time: Current time for scheduling checks

        Returns:
            Task data if a task was dequeued, None if no tasks available
        """

        logger.debug(
            f"DEQUEUE START: Worker {worker_id} searching for tasks of types {task_types} at {current_time}"
        )

        # Task timeout: tasks stuck in processing state for longer than this will be reclaimed
        # Must be significantly larger than TASK_HANDLER_TIMEOUT (300s/5m) to prevent
        # race conditions where a running worker is treated as stalled.
        task_timeout_minutes = 15
        stale_task_cutoff = current_time - timedelta(minutes=task_timeout_minutes)

        async def _claim(txn: DatabaseTransaction) -> TaskDict | None:
            """Select a task and lock it, atomically -- the lock *is* the claim."""
            if txn.dialect_name == "postgresql":
                # PostgreSQL: SELECT FOR UPDATE SKIP LOCKED for true atomic dequeue
                stmt = (
                    select(tasks_table)
                    .where(
                        or_(
                            # Normal pending tasks
                            tasks_table.c.status == "pending",
                            # Stalled processing tasks (worker likely crashed)
                            and_(
                                tasks_table.c.status == "processing",
                                tasks_table.c.locked_at <= stale_task_cutoff,
                            ),
                        ),
                        tasks_table.c.task_type.in_(task_types),
                        or_(
                            tasks_table.c.scheduled_at.is_(None),
                            tasks_table.c.scheduled_at <= current_time,
                        ),
                        tasks_table.c.retry_count <= tasks_table.c.max_retries,
                    )
                    .order_by(
                        tasks_table.c.scheduled_at.asc().nullsfirst(),
                        tasks_table.c.retry_count.asc(),
                        tasks_table.c.created_at.asc(),
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )

                row = await txn.fetch_one(stmt)
                if row:
                    logger.info(
                        f"DEQUEUE SUCCESS (PostgreSQL): Worker {worker_id} dequeued task {row['task_id']} (type: {row['task_type']})"
                    )
                    # Update the task to mark it as locked
                    update_stmt = (
                        update(tasks_table)
                        .where(tasks_table.c.id == row["id"])
                        .values(
                            status="processing",
                            locked_by=worker_id,
                            locked_at=current_time,
                        )
                    )
                    await txn.execute(update_stmt)
                    logger.debug(
                        f"DEQUEUE LOCKED: Worker {worker_id} marked task {row['task_id']} as processing"
                    )
                    return cast("TaskDict", dict(row))
                else:
                    logger.debug(
                        f"DEQUEUE EMPTY (PostgreSQL): Worker {worker_id} found no available tasks"
                    )
                    return None
            else:
                # SQLite: Use atomic UPDATE to claim the first available task
                # This ensures only one worker can claim each task
                update_stmt = (
                    update(tasks_table)
                    .where(
                        or_(
                            # Normal pending tasks
                            tasks_table.c.status == "pending",
                            # Stalled processing tasks (worker likely crashed)
                            and_(
                                tasks_table.c.status == "processing",
                                tasks_table.c.locked_at <= stale_task_cutoff,
                            ),
                        ),
                        tasks_table.c.task_type.in_(task_types),
                        or_(
                            tasks_table.c.scheduled_at.is_(None),
                            tasks_table.c.scheduled_at <= current_time,
                        ),
                        tasks_table.c.retry_count <= tasks_table.c.max_retries,
                        # Use a subquery to enforce ordering and limit to first task
                        tasks_table.c.id
                        == select(tasks_table.c.id)
                        .where(
                            or_(
                                # Normal pending tasks
                                tasks_table.c.status == "pending",
                                # Stalled processing tasks (worker likely crashed)
                                and_(
                                    tasks_table.c.status == "processing",
                                    tasks_table.c.locked_at <= stale_task_cutoff,
                                ),
                            ),
                            tasks_table.c.task_type.in_(task_types),
                            or_(
                                tasks_table.c.scheduled_at.is_(None),
                                tasks_table.c.scheduled_at <= current_time,
                            ),
                            tasks_table.c.retry_count <= tasks_table.c.max_retries,
                        )
                        .order_by(
                            tasks_table.c.scheduled_at.asc().nullsfirst(),
                            tasks_table.c.retry_count.asc(),
                            tasks_table.c.created_at.asc(),
                        )
                        .limit(1)
                        .scalar_subquery(),
                    )
                    .values(
                        status="processing", locked_by=worker_id, locked_at=current_time
                    )
                )

                result = await txn.execute(update_stmt)
                if result.rowcount > 0:
                    # Successfully claimed a task, now fetch it
                    fetch_stmt = (
                        select(tasks_table)
                        .where(
                            tasks_table.c.locked_by == worker_id,
                            tasks_table.c.status == "processing",
                            tasks_table.c.task_type.in_(task_types),
                        )
                        .order_by(tasks_table.c.locked_at.desc())
                        .limit(1)
                    )
                    task_row = await txn.fetch_one(fetch_stmt)
                    if task_row:
                        return cast("TaskDict", dict(task_row))

                return None

        return await self._db.atomic(_claim)

    async def attach_definition_verdict(
        self,
        task_id: str,
        *,
        write_id: str,
        disposition: CreationDisposition,
        gate: GateProvenance,
    ) -> bool:
        """Attach an asynchronously computed verdict to a payload-carried definition.

        A reminder, future callback or one-shot script action has no definition
        table -- its definition is the enqueued payload -- so an observe-mode
        verdict lands here. The write id guards the update, read and write in
        one transaction: the payload must still hold the exact write the verdict
        judged, so an edit racing the review leaves the new content awaiting its
        own verdict.

        A follow-up re-enqueued before the verdict lands copies the still-pending
        record into a task of its own, and the verdict does not chase that
        descendant chain -- bounded to the same seconds-wide window, conservative,
        and accepted rather than solved with descendant tracking.

        Returns whether the verdict was attached.
        """

        async def body(txn: DatabaseTransaction) -> bool:
            # Locked, not merely re-read: on PostgreSQL a concurrent write
            # committing between the check and the update would otherwise be
            # overwritten by the record this read returned -- reverting an edit
            # while reporting the verdict attached. SQLite serializes writes on
            # the engine lock and ignores the clause.
            row = await txn.fetch_one(
                select(tasks_table.c.payload)
                .where(tasks_table.c.task_id == task_id)
                .with_for_update()
            )
            payload = row["payload"] if row is not None else None
            if not isinstance(payload, dict):
                return False
            record = definition_record_from_row(
                payload.get("tool_call_review_definition_record")
            )
            if record is None or record.pending_write_id != write_id:
                return False
            updated = dict(cast("Mapping[str, Any]", payload))
            # ast-grep-ignore: no-unstamped-executable-definition-write - verdict attach: with_verdict() derives from the stored record, leaving stamp and hash untouched
            updated["tool_call_review_definition_record"] = record.with_verdict(
                disposition, gate
            ).to_dict()
            await txn.execute(
                update(tasks_table)
                .where(tasks_table.c.task_id == task_id)
                .values(payload=updated)
            )
            return True

        return await self._db.atomic(body)

    async def update_status(
        self,
        task_id: str,
        status: str,
        error: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """
        Updates the status of a task.

        Args:
            task_id: The unique task identifier
            status: New status ('completed', 'failed', etc.')
            error: Optional error message if the task failed
            payload: Optional replacement payload
        """
        values: dict[str, object] = {"status": status}
        if error is not None:
            values["error"] = error
        if payload is not None:
            values["payload"] = payload

        stmt = (
            update(tasks_table).where(tasks_table.c.task_id == task_id).values(**values)
        )

        result = await self._db.execute(stmt)
        if result.rowcount == 0:
            logger.warning(f"Task {task_id} not found for status update to {status}")
        else:
            error_msg = f" (error: {error})" if error else ""
            logger.info(
                f"STATUS UPDATE: Task {task_id} status changed to {status}{error_msg}"
            )

    async def reschedule_for_retry(
        self,
        task_id: str,
        next_scheduled_at: datetime,
        new_retry_count: int,
        error: str,
    ) -> bool:
        """
        Reschedules a task for retry.

        Args:
            task_id: The unique task identifier
            next_scheduled_at: When to retry the task (must be timezone-aware)
            new_retry_count: The new retry count
            error: Error message from the failed attempt

        Returns:
            True if the task was rescheduled, False otherwise
        """
        if next_scheduled_at.tzinfo is None:
            raise ValueError("next_scheduled_at must be timezone-aware")

        # Update the task for retry
        update_stmt = (
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            .values(
                status="pending",
                scheduled_at=next_scheduled_at,
                retry_count=new_retry_count,
                error=error,
            )
        )

        result = await self._db.execute(update_stmt)
        if result.rowcount == 0:
            logger.error(
                f"RESCHEDULE FAILED: Task {task_id} not found for retry scheduling"
            )
            return False

        logger.info(
            f"RESCHEDULE SUCCESS: Task {task_id} rescheduled for retry #{new_retry_count} at {next_scheduled_at} (error: {error})"
        )
        return True

    async def get_all(
        self,
        status: str | None = None,
        task_type: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        sort_order: str = "asc",
        limit: int = 100,
    ) -> list[TaskDict]:
        """
        Retrieves tasks from the queue with optional filtering.

        Args:
            status: Filter by task status
            task_type: Filter by task type
            date_from: Filter tasks created after this date (inclusive)
            date_to: Filter tasks created before this date (inclusive)
            sort_order: Sort order for created_at ("asc" or "desc")
            limit: Maximum number of tasks to return

        Returns:
            List of task dictionaries
        """
        stmt = select(tasks_table)

        # Add filters
        conditions = []
        if status:
            conditions.append(tasks_table.c.status == status)
        if task_type:
            conditions.append(tasks_table.c.task_type == task_type)
        if date_from:
            conditions.append(tasks_table.c.created_at >= date_from)
        if date_to:
            conditions.append(tasks_table.c.created_at <= date_to)

        if conditions:
            stmt = stmt.where(*conditions)

        # Order by creation time based on sort_order
        if sort_order == "desc":
            # Newest first (reverse chronological)
            stmt = stmt.order_by(tasks_table.c.created_at.desc())
        else:
            # Oldest first (chronological) - original behavior
            stmt = stmt.order_by(
                tasks_table.c.scheduled_at.asc().nullsfirst(),
                tasks_table.c.created_at.asc(),
            )

        stmt = stmt.limit(limit)

        rows = await self._db.fetch_all(stmt)
        return [cast("TaskDict", dict(row)) for row in rows]

    async def get_tasks_for_listener(
        self,
        listener_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[TaskDict], int]:
        """Get script execution tasks for a specific listener."""
        # Build query for script execution tasks that match the listener
        # Task IDs for script listeners follow format: script_listener_{listener_id}_{timestamp}
        task_id_pattern = f"script_listener_{listener_id}_%"

        stmt = select(tasks_table).where(
            (tasks_table.c.task_type == "script_execution")
            & (tasks_table.c.task_id.like(task_id_pattern))
        )

        # Get total count
        count_stmt = select(func.count().label("count")).select_from(
            stmt.alias("tasks_subquery")
        )
        try:
            count_result = await self._db.fetch_one(count_stmt)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_tasks_for_listener: {e}")
            raise

        total_count = count_result["count"] if count_result else 0
        stmt = stmt.order_by(tasks_table.c.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)

        try:
            rows = await self._db.fetch_all(stmt)
        except SQLAlchemyError as e:
            self._logger.exception(f"Database error in get_tasks_for_listener: {e}")
            raise

        tasks = [cast("TaskDict", dict(row)) for row in rows]
        return tasks, total_count

    async def manually_retry(self, internal_task_id: int) -> bool:
        """
        Manually retries a task that has failed or exhausted its retries.
        Increments max_retries, sets status to pending, and schedules for immediate run.

        Args:
            internal_task_id: The internal database ID of the task (tasks_table.c.id)

        Returns:
            True if the task was successfully queued for retry, False otherwise
        """
        current_time = datetime.now(UTC)

        # Fetch the task by its internal ID
        select_stmt = select(tasks_table).where(tasks_table.c.id == internal_task_id)
        task_row = await self._db.fetch_one(select_stmt)

        if not task_row:
            logger.warning(
                f"Manual retry requested for non-existent task with internal ID {internal_task_id}."
            )
            return False

        task = dict(task_row)
        logger.info(
            f"Manual retry requested for task {task['task_id']} "
            f"(internal ID: {internal_task_id}, status: {task['status']}, "
            f"retry_count: {task['retry_count']}, max_retries: {task['max_retries']})"
        )

        # Update the task to be retryable
        # We increment max_retries to allow the retry and reset to pending
        new_max_retries = max(task["max_retries"], task["retry_count"]) + 1

        update_stmt = (
            update(tasks_table)
            .where(tasks_table.c.id == internal_task_id)
            .values(
                status="pending",
                max_retries=new_max_retries,
                scheduled_at=current_time,  # Schedule for immediate execution
                error=None,  # Clear the error to give it a fresh start
            )
        )

        result = await self._db.execute(update_stmt)

        if result.rowcount > 0:
            logger.info(
                f"Successfully queued task {task['task_id']} for manual retry. "
                f"Max retries increased to {new_max_retries}."
            )
            return True
        else:
            logger.error(f"Failed to update task {task['task_id']} for manual retry.")
            return False

    async def cancel_task(self, internal_task_id: int) -> bool:
        """
        Cancels a pending task.

        Args:
            internal_task_id: The internal database ID of the task (tasks_table.c.id)

        Returns:
            True if the task was successfully cancelled, False otherwise
            (e.g., task not found or not in pending status)
        """
        # Fetch the task by its internal ID
        select_stmt = select(tasks_table).where(tasks_table.c.id == internal_task_id)
        task_row = await self._db.fetch_one(select_stmt)

        if not task_row:
            logger.warning(
                f"Cancel requested for non-existent task with internal ID {internal_task_id}."
            )
            return False

        task = dict(task_row)
        logger.info(
            f"Cancel requested for task {task['task_id']} "
            f"(internal ID: {internal_task_id}, status: {task['status']})"
        )

        # Only allow cancelling pending tasks
        if task["status"] != "pending":
            logger.warning(
                f"Cannot cancel task {task['task_id']} with status '{task['status']}'. "
                f"Only pending tasks can be cancelled."
            )
            return False

        # Update the task status to cancelled
        update_stmt = (
            update(tasks_table)
            .where(tasks_table.c.id == internal_task_id)
            .where(tasks_table.c.status == "pending")  # Double-check status
            .values(status="cancelled")
        )

        result = await self._db.execute(update_stmt)

        if result.rowcount > 0:
            logger.info(f"Successfully cancelled task {task['task_id']}.")
            return True
        else:
            logger.error(f"Failed to cancel task {task['task_id']}.")
            return False
