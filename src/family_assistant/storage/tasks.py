"""
Handles storage and retrieval of background tasks using the database queue.
"""

import logging
import random
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import (
    Table,
    Column,
    String,
    Integer,
    Text,
    DateTime,
    JSON,
    select,
    insert,
    update,
    desc,
)
from sqlalchemy.exc import DBAPIError

# Use absolute package path
from family_assistant.storage.base import metadata, get_engine

logger = logging.getLogger(__name__)
engine = get_engine()

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
    db_context: DatabaseContext, # Added context
    task_id: str,
    task_type: str,
    payload: Optional[Dict[str, Any]] = None,
    scheduled_at: Optional[datetime] = None,
    max_retries_override: Optional[int] = None,
    recurrence_rule: Optional[str] = None,
    original_task_id: Optional[str] = None,
    notify_event: Optional[asyncio.Event] = None,
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
        await db_context.execute_and_commit(stmt)
        logger.info(
            f"Enqueued task {task_id} (Type: {task_type}, Original: {values_to_insert.get('original_task_id')}, Recurrence: {'Yes' if recurrence_rule else 'No'})."
        )
        is_immediate = scheduled_at is None or scheduled_at <= datetime.now(
            timezone.utc
        )
        if is_immediate and notify_event:
            notify_event.set()
            logger.debug(f"Notified worker about immediate task {task_id}.")
    except ValueError: # Re-raise specific errors
        raise
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in enqueue_task {task_id}: {e}", exc_info=True
        )
        raise


async def dequeue_task(
    db_context: DatabaseContext, # Added context
    worker_id: str,
    task_types: List[str]
) -> Optional[Dict[str, Any]]:
    """Atomically dequeues the next available task."""
    now = datetime.now(timezone.utc)

    # This operation needs to be atomic (SELECT FOR UPDATE + UPDATE)
    # DatabaseContext doesn't directly expose SELECT FOR UPDATE easily with retry.
    # We'll perform this within an explicit transaction managed by the context.
    try:
        await db_context.begin() # Start transaction

        stmt = (
            select(tasks_table)
            .where(tasks_table.c.status == "pending")
            .where(tasks_table.c.task_type.in_(task_types))
            .where(
                (tasks_table.c.scheduled_at == None)
                | (tasks_table.c.scheduled_at <= now)
            )  # noqa: E711
            .where(tasks_table.c.retry_count < tasks_table.c.max_retries)
            .order_by(
                tasks_table.c.retry_count.asc(),
                tasks_table.c.created_at.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True) # Lock the selected row
        )
        # Execute within the transaction, using the context's retry logic
        result = await db_context.execute_with_retry(stmt)
        task_row = result.fetchone() # Use fetchone directly on the result proxy

        if task_row:
            update_stmt = (
                update(tasks_table)
                .where(tasks_table.c.id == task_row.id)
                .where(tasks_table.c.status == "pending") # Ensure status hasn't changed
                .values(
                    status="processing", locked_by=worker_id, locked_at=now
                )
            )
            # Execute update within the same transaction
            update_result = await db_context.execute_with_retry(update_stmt)

            if update_result.rowcount == 1:
                await db_context.commit() # Commit the transaction
                logger.info(
                    f"Worker {worker_id} dequeued task {task_row.task_id}"
                )
                return task_row._mapping # Return the original row data
            else:
                # This means the row was locked or status changed between select and update
                logger.warning(
                    f"Worker {worker_id} failed to lock task {task_row.task_id} after selection (rowcount={update_result.rowcount}). Rolling back."
                )
                await db_context.rollback()
                return None
        else:
            await db_context.rollback() # Rollback if no task found
            return None # No suitable task found

    except SQLAlchemyError as e:
        logger.error(f"Database error in dequeue_task: {e}", exc_info=True)
        # Ensure rollback happens on error if transaction was started
        if db_context._in_transaction: # Check internal flag (use with caution)
             await db_context.rollback()
        raise
    except Exception as e:
        logger.error(f"Unexpected error in dequeue_task: {e}", exc_info=True)
        if db_context._in_transaction:
             await db_context.rollback()
        raise


async def update_task_status(
    db_context: DatabaseContext, # Added context
    task_id: str,
    status: str,
    error: Optional[str] = None
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
        result = await db_context.execute_and_commit(stmt)
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
    db_context: DatabaseContext, # Added context
    task_id: str,
    next_scheduled_at: datetime,
    new_retry_count: int,
    error: str
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
        result = await db_context.execute_and_commit(stmt)
        if result.rowcount > 0:
            logger.info(
                f"Rescheduled task {task_id} for retry {new_retry_count} at {next_scheduled_at}."
            )
            return True
        else:
            logger.warning(f"Task {task_id} not found when rescheduling for retry.")
            return False
    except ValueError: # Re-raise specific errors
        raise
    except SQLAlchemyError as e:
        logger.error(
            f"Database error in reschedule_task_for_retry({task_id}): {e}",
            exc_info=True,
        )
        raise


async def get_all_tasks(db_context: DatabaseContext, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves tasks, ordered by creation descending."""
    try:
        stmt = select(tasks_table).order_by(tasks_table.c.created_at.desc()).limit(limit)
        rows = await db_context.fetch_all(stmt)
        return rows # fetch_all already returns list of dict-like mappings
    except SQLAlchemyError as e:
        logger.error(f"Database error in get_all_tasks: {e}", exc_info=True)
        raise
