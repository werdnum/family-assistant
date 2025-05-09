"""
Handles storage and retrieval of background tasks using the database queue.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    insert,
    select,
    or_,
    update,
)
from sqlalchemy.exc import SQLAlchemyError

# Use absolute package path
from family_assistant.storage.base import metadata  # Keep metadata

# Remove get_engine import
from family_assistant.storage.context import DatabaseContext  # Import DatabaseContext

logger = logging.getLogger(__name__)
# Remove engine = get_engine()

# Define the tasks table for the message queue
tasks_table = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String, nullable=False, unique=True, index=True),
    Column("task_type", String, nullable=False, index=True),
    Column("payload", JSON, nullable=True),
    Column("scheduled_at", DateTime(timezone=True), nullable=True, index=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    ),
    Column("status", String, default="pending", nullable=False, index=True),
    Column("locked_by", String, nullable=True),
    Column("locked_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    Column("retry_count", Integer, default=0, nullable=False),
    Column("max_retries", Integer, default=3, nullable=False),
    Column("recurrence_rule", String, nullable=True),
    Column("original_task_id", String, nullable=True, index=True),
)


async def enqueue_task(
    db_context: DatabaseContext,  # Added context
    task_id: str,
    task_type: str,
    payload: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,
    max_retries_override: int | None = None,
    recurrence_rule: str | None = None,
    original_task_id: str | None = None,
    notify_event: asyncio.Event | None = None,
):  # noqa: PLR0913
    """Adds a task to the queue, optional notification."""
    if scheduled_at and scheduled_at.tzinfo is None:
        raise ValueError("scheduled_at must be timezone-aware")

    max_task_retries = max_retries_override if max_retries_override is not None else 3

    values_to_insert = {
        "task_id": task_id,
        "task_type": task_type,
        "payload": payload,
        "scheduled_at": scheduled_at,
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
        stmt = insert(tasks_table).values(**values_to_insert)
        # Use execute_with_retry as commit is handled by context manager
        await db_context.execute_with_retry(stmt)
        logger.info(
            f"Enqueued task {task_id} (Type: {task_type}, Original: {values_to_insert.get('original_task_id')}, Recurrence: {'Yes' if recurrence_rule else 'No'})."
        )
        is_immediate = scheduled_at is None or scheduled_at <= datetime.now(
            timezone.utc
        )
        if is_immediate and notify_event:

            def notify(*args):
                notify_event.set()
                logger.info(f"Notified worker about immediate task {task_id}.")

            # Trigger eager task execution after the transaction commits.
            logger.info("Scheduling worker task notification for transaction commit.")
            db_context.on_commit(notify)
    except ValueError:  # Re-raise specific errors
        raise
    except SQLAlchemyError as e:
        logger.error(f"Database error in enqueue_task {task_id}: {e}", exc_info=True)
        raise


async def dequeue_task(
    db_context: DatabaseContext,
    worker_id: str,
    task_types: list[str],  # Added context
) -> dict[str, Any] | None:
    """Atomically dequeues the next available task."""
    now = datetime.now(timezone.utc)

    # This operation needs to be atomic (SELECT FOR UPDATE + UPDATE)
    # The transaction is now managed by the DatabaseContext context manager itself.
    try:
        # No need to call db_context.begin() here

        stmt = (
            select(tasks_table)
            .where(tasks_table.c.status == "pending")
            .where(tasks_table.c.task_type.in_(task_types))
            .where(or_(
                tasks_table.c.scheduled_at.is_(None),
                tasks_table.c.scheduled_at <= now
            ))
            .where(tasks_table.c.retry_count < tasks_table.c.max_retries)
            .order_by(
                tasks_table.c.retry_count.asc(),
                tasks_table.c.created_at.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)  # Lock the selected row
        )
        # Execute within the transaction, using the context's retry logic
        result = await db_context.execute_with_retry(stmt)
        task_row = result.fetchone()  # Use fetchone directly on the result proxy

        if task_row:
            update_stmt = (
                update(tasks_table)
                .where(tasks_table.c.id == task_row.id)
                .where(
                    tasks_table.c.status == "pending"
                )  # Ensure status hasn't changed
                .values(status="processing", locked_by=worker_id, locked_at=now)
            )
            # Execute update within the same transaction
            update_result = await db_context.execute_with_retry(update_stmt)

            if update_result.rowcount == 1:
                # No need to call db_context.commit() here, context manager handles it
                logger.info(f"Worker {worker_id} dequeued task {task_row.task_id}")
                return task_row._mapping  # Return the original row data
            else:
                # This means the row was locked or status changed between select and update
                logger.warning(
                    f"Worker {worker_id} failed to lock task {task_row.task_id} after selection (rowcount={update_result.rowcount}). Rolling back."
                )
                # No need to call db_context.rollback() here, context manager handles it on exit if error occurred
                return None
        else:
            # No need to call db_context.rollback() here, context manager handles it on exit
            return None  # No suitable task found

    except SQLAlchemyError as e:
        logger.error(f"Database error in dequeue_task: {e}", exc_info=True)
        # Rollback is handled by the context manager's __aexit__ on exception
        raise
    except Exception as e:
        logger.error(f"Unexpected error in dequeue_task: {e}", exc_info=True)
        # Rollback is handled by the context manager's __aexit__ on exception
        raise


async def update_task_status(
    db_context: DatabaseContext,  # Added context
    task_id: str,
    status: str,
    error: str | None = None,
) -> bool:
    """Updates task status."""
    values_to_update = {"status": status, "locked_by": None, "locked_at": None}
    if status == "failed":
        values_to_update["error"] = error

    try:
        stmt = (
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            .values(**values_to_update)
        )
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
        if result.rowcount > 0:
            logger.info(f"Updated task {task_id} status to {status}.")
            return True
        else:
            logger.warning(
                f"Task {task_id} not found or status unchanged when updating to {status}."
            )
            return False
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in update_task_status({task_id}): {e}",
            exc_info=True,
        )
        raise


async def reschedule_task_for_retry(
    db_context: DatabaseContext,  # Added context
    task_id: str,
    next_scheduled_at: datetime,
    new_retry_count: int,
    error: str,
) -> bool:
    """Reschedules a task for retry."""
    if next_scheduled_at.tzinfo is None:
        raise ValueError("next_scheduled_at must be timezone-aware")

    try:
        stmt = (
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            .values(
                status="pending",
                retry_count=new_retry_count,
                scheduled_at=next_scheduled_at,
                error=error,
                locked_by=None,
                locked_at=None,
            )
        )
        # Use execute_with_retry as commit is handled by context manager
        result = await db_context.execute_with_retry(stmt)
        if result.rowcount > 0:
            logger.info(
                f"Rescheduled task {task_id} for retry {new_retry_count} at {next_scheduled_at}."
            )
            return True
        else:
            logger.warning(f"Task {task_id} not found when rescheduling for retry.")
            return False
    except ValueError:  # Re-raise specific errors
        raise
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in reschedule_task_for_retry({task_id}): {e}",
            exc_info=True,
        )
        raise


async def get_all_tasks(
    db_context: DatabaseContext, limit: int = 100
) -> list[dict[str, Any]]:
    """Retrieves tasks, ordered by creation descending."""
    try:
        stmt = (
            select(tasks_table).order_by(tasks_table.c.created_at.desc()).limit(limit)
        )
        rows = await db_context.fetch_all(stmt)
        return rows  # fetch_all already returns list of dict-like mappings
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_all_tasks: {e}", exc_info=True)
        raise
