"""Repository for tasks storage operations."""

import asyncio
import logging
from asyncio import Event
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from family_assistant.storage.repositories.base import BaseRepository
from family_assistant.storage.tasks import tasks_table

logger = logging.getLogger(__name__)

# Module state for task notifications
_task_event: Event | None = None


def get_task_event() -> Event:
    """Get the event that's set when new tasks are available.

    This event is automatically set when immediate tasks are enqueued.
    Task workers can wait on this event to be notified of new work.

    Returns:
        The global task notification event
    """
    global _task_event
    if _task_event is None:
        _task_event = asyncio.Event()
    return _task_event


class TasksRepository(BaseRepository):
    """Repository for managing background tasks in the database queue."""

    async def enqueue(
        self,
        task_id: str,
        task_type: str,
        payload: dict[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        max_retries_override: int | None = None,
        recurrence_rule: str | None = None,
        original_task_id: str | None = None,
    ) -> None:  # noqa: PLR0913
        """Adds a task to the queue with automatic notification for immediate tasks.

        Args:
            task_id: Unique identifier for the task
            task_type: Type of task (determines which handler processes it)
            payload: Optional data payload for the task
            scheduled_at: When to run the task (None = immediate)
            max_retries_override: Override default max retries
            recurrence_rule: Optional recurrence rule for repeating tasks
            original_task_id: ID of the original task if this is a recurrence
        """
        processed_scheduled_at = scheduled_at
        if processed_scheduled_at:
            if processed_scheduled_at.tzinfo is None:
                raise ValueError("scheduled_at must be timezone-aware")
            # Convert to UTC if it's aware and not already UTC
            if processed_scheduled_at.tzinfo != timezone.utc:
                logger.debug(
                    f"Converting scheduled_at for task {task_id} from {processed_scheduled_at.tzinfo} to UTC."
                )
                processed_scheduled_at = processed_scheduled_at.astimezone(timezone.utc)

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
            if v is not None or k in ["payload", "error"]
        }

        try:
            # Check if this is a system task (starts with "system_")
            is_system_task = task_id.startswith("system_")

            if is_system_task:
                # For system tasks, do an upsert to handle re-scheduling
                if self._db.engine.dialect.name == "postgresql":
                    # PostgreSQL: Use ON CONFLICT DO UPDATE
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    stmt = pg_insert(tasks_table).values(**values_to_insert)
                    # Only update fields that might change for system tasks
                    update_dict = {
                        "scheduled_at": stmt.excluded.scheduled_at,
                        "payload": stmt.excluded.payload,
                        "max_retries": stmt.excluded.max_retries,
                        "recurrence_rule": stmt.excluded.recurrence_rule,
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
                        )
                    )
                    result = await self._db.execute_with_retry(update_stmt)
                    if result.rowcount == 0:  # type: ignore[attr-defined]
                        # Task doesn't exist, do INSERT
                        stmt = insert(tasks_table).values(**values_to_insert)
                    else:
                        # Update succeeded, skip INSERT
                        stmt = None
            else:
                # For non-system tasks, just do a regular INSERT
                stmt = insert(tasks_table).values(**values_to_insert)

            if stmt is not None:
                await self._db.execute_with_retry(stmt)

            # If task is immediate and we're in the main context, set the event
            if not processed_scheduled_at or processed_scheduled_at <= datetime.now(
                timezone.utc
            ):
                event = get_task_event()
                event.set()

            logger.info(f"Successfully enqueued task: {task_id} (type: {task_type})")

        except IntegrityError as e:
            # For non-system tasks, this is an error
            if not is_system_task:
                logger.error(
                    f"Task with ID '{task_id}' already exists in the queue: {e}",
                    exc_info=True,
                )
                raise RuntimeError(f"Task ID '{task_id}' already exists") from e
            else:
                # For system tasks, integrity error during PostgreSQL upsert shouldn't happen
                logger.error(
                    f"Unexpected integrity error for system task '{task_id}': {e}",
                    exc_info=True,
                )
                raise
        except SQLAlchemyError as e:
            logger.error(
                f"Database error enqueueing task {task_id}: {e}", exc_info=True
            )
            raise

    async def dequeue(
        self,
        worker_id: str,
        task_types: list[str],
        current_time: datetime,
    ) -> dict[str, Any] | None:
        """
        Atomically dequeues the next available task for a worker.

        Args:
            worker_id: Unique identifier for the worker
            task_types: List of task types this worker can handle
            current_time: Current time for scheduling checks

        Returns:
            Task data if a task was dequeued, None if no tasks available
        """

        if self._db.engine.dialect.name == "postgresql":
            # PostgreSQL: Use SELECT FOR UPDATE SKIP LOCKED for true atomic dequeue
            stmt = (
                select(tasks_table)
                .where(
                    tasks_table.c.status == "pending",
                    tasks_table.c.task_type.in_(task_types),
                    or_(
                        tasks_table.c.scheduled_at.is_(None),
                        tasks_table.c.scheduled_at <= current_time,
                    ),
                    tasks_table.c.retry_count <= tasks_table.c.max_retries,
                )
                .order_by(
                    tasks_table.c.retry_count.asc(),
                    tasks_table.c.created_at.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )

            row = await self._db.fetch_one(stmt)
            if row:
                # Update the task to mark it as locked
                update_stmt = (
                    update(tasks_table)
                    .where(tasks_table.c.id == row["id"])
                    .values(
                        status="processing", locked_by=worker_id, locked_at=current_time
                    )
                )
                await self._db.execute_with_retry(update_stmt)
                return dict(row)
            return None
        else:
            # SQLite fallback: Use a two-step process (less atomic but functional)
            # First, find an available task
            select_stmt = (
                select(tasks_table.c.id)
                .where(
                    tasks_table.c.status == "pending",
                    tasks_table.c.task_type.in_(task_types),
                    or_(
                        tasks_table.c.scheduled_at.is_(None),
                        tasks_table.c.scheduled_at <= current_time,
                    ),
                    tasks_table.c.retry_count <= tasks_table.c.max_retries,
                )
                .order_by(
                    tasks_table.c.scheduled_at.asc().nullsfirst(),
                    tasks_table.c.created_at.asc(),
                )
                .limit(1)
            )

            row = await self._db.fetch_one(select_stmt)
            if not row:
                return None

            # Try to lock it
            update_stmt = (
                update(tasks_table)
                .where(
                    tasks_table.c.id == row["id"],
                    tasks_table.c.status == "pending",  # Double-check status
                )
                .values(
                    status="processing", locked_by=worker_id, locked_at=current_time
                )
            )

            result = await self._db.execute_with_retry(update_stmt)
            if result.rowcount == 0:  # type: ignore[attr-defined]
                # Someone else got it first
                return None

            # Fetch the full task data
            fetch_stmt = select(tasks_table).where(tasks_table.c.id == row["id"])
            task_row = await self._db.fetch_one(fetch_stmt)
            return dict(task_row) if task_row else None

    async def update_status(
        self,
        task_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """
        Updates the status of a task.

        Args:
            task_id: The unique task identifier
            status: New status ('completed', 'failed', etc.)
            error: Optional error message if the task failed
        """
        values = {"status": status}
        if error is not None:
            values["error"] = error

        stmt = (
            update(tasks_table).where(tasks_table.c.task_id == task_id).values(**values)
        )

        result = await self._db.execute_with_retry(stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            logger.warning(f"Task {task_id} not found for status update to {status}")

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

        result = await self._db.execute_with_retry(update_stmt)
        if result.rowcount == 0:  # type: ignore[attr-defined]
            logger.error(f"Task {task_id} not found for retry scheduling")
            return False

        logger.info(
            f"Rescheduled task {task_id} for retry {new_retry_count} at {next_scheduled_at}."
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
    ) -> list[dict[str, Any]]:
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
        return [dict(row) for row in rows]

    async def get_tasks_for_listener(
        self,
        listener_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Get script execution tasks for a specific listener."""
        from sqlalchemy.sql import functions as func

        try:
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
            count_result = await self._db.fetch_one(count_stmt)
            total_count = count_result["count"] if count_result else 0

            # Apply pagination and ordering
            stmt = stmt.order_by(tasks_table.c.created_at.desc())
            stmt = stmt.limit(limit).offset(offset)

            rows = await self._db.fetch_all(stmt)
            tasks = [dict(row) for row in rows]

            return tasks, total_count

        except SQLAlchemyError as e:
            self._logger.error(
                f"Database error in get_tasks_for_listener: {e}", exc_info=True
            )
            raise

    async def manually_retry(self, internal_task_id: int) -> bool:
        """
        Manually retries a task that has failed or exhausted its retries.
        Increments max_retries, sets status to pending, and schedules for immediate run.

        Args:
            internal_task_id: The internal database ID of the task (tasks_table.c.id)

        Returns:
            True if the task was successfully queued for retry, False otherwise
        """
        current_time = datetime.now(timezone.utc)

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

        result = await self._db.execute_with_retry(update_stmt)

        if result.rowcount > 0:  # type: ignore[attr-defined]
            logger.info(
                f"Successfully queued task {task['task_id']} for manual retry. "
                f"Max retries increased to {new_max_retries}."
            )
            return True
        else:
            logger.error(f"Failed to update task {task['task_id']} for manual retry.")
            return False
