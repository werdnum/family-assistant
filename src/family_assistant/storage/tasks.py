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
    task_id: str,
    task_type: str,
    payload: Optional[Dict[str, Any]] = None,
    scheduled_at: Optional[datetime] = None,
    max_retries_override: Optional[int] = None,
    recurrence_rule: Optional[str] = None,
    original_task_id: Optional[str] = None,
    notify_event: Optional[asyncio.Event] = None,
):  # noqa: PLR0913
    """Adds a task, handles retry logic, optional notification."""
    max_db_retries = 3
    base_delay = 0.5

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
    values_to_insert = {
        k: v
        for k, v in values_to_insert.items()
        if v is not None or k in ["payload", "error"]
    }

    for attempt in range(max_db_retries):
        try:
            async with engine.connect() as conn:
                stmt = insert(tasks_table).values(**values_to_insert)
                await conn.execute(stmt)
                await conn.commit()
                logger.info(
                    f"Enqueued task {task_id} (Type: {task_type}, Original: {values_to_insert['original_task_id']}, Recurrence: {'Yes' if recurrence_rule else 'No'})."
                )
                is_immediate = scheduled_at is None or scheduled_at <= datetime.now(
                    timezone.utc
                )
                if is_immediate and notify_event:
                    notify_event.set()
                    logger.debug(f"Notified worker about immediate task {task_id}.")
                return
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in enqueue_task (attempt {attempt + 1}/{max_db_retries}): {e}. Retrying..."
            )
            if attempt == max_db_retries - 1:
                logger.error(
                    f"Max retries exceeded for enqueue_task({task_id}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                f"Non-retryable error in enqueue_task {task_id}: {e}", exc_info=True
            )
            raise
    raise RuntimeError(
        f"Database operation failed for enqueue_task({task_id}) after multiple retries"
    )


async def dequeue_task(
    worker_id: str, task_types: List[str]
) -> Optional[Dict[str, Any]]:
    """Atomically dequeues the next available task, handles retries."""
    now = datetime.now(timezone.utc)
    max_db_retries = 3
    base_delay = 0.5

    for attempt in range(max_db_retries):
        try:
            async with engine.connect() as conn:
                async with conn.begin():
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
                        .with_for_update(skip_locked=True)
                    )
                    result = await conn.execute(stmt)
                    task_row = result.fetchone()

                    if task_row:
                        update_stmt = (
                            update(tasks_table)
                            .where(tasks_table.c.id == task_row.id)
                            .where(tasks_table.c.status == "pending")
                            .values(
                                status="processing", locked_by=worker_id, locked_at=now
                            )
                        )
                        update_result = await conn.execute(update_stmt)
                        if update_result.rowcount == 1:
                            logger.info(
                                f"Worker {worker_id} dequeued task {task_row.task_id}"
                            )
                            return task_row._mapping
                        else:
                            logger.warning(
                                f"Worker {worker_id} failed to lock task {task_row.task_id} after selection."
                            )
                            return None  # Transaction rollback handles cleanup
                    else:
                        return None  # No suitable task found
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in dequeue_task (attempt {attempt + 1}/{max_db_retries}): {e}. Retrying..."
            )
            if attempt == max_db_retries - 1:
                logger.error("Max retries exceeded for dequeue_task. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in dequeue_task: {e}", exc_info=True)
            raise
    raise RuntimeError(
        "Database operation failed for dequeue_task after multiple retries"
    )


async def update_task_status(
    task_id: str, status: str, error: Optional[str] = None
) -> bool:
    """Updates task status, handles retries."""
    max_db_retries = 3
    base_delay = 0.5
    values_to_update = {"status": status, "locked_by": None, "locked_at": None}
    if status == "failed":
        values_to_update["error"] = error

    for attempt in range(max_db_retries):
        try:
            async with engine.connect() as conn:
                stmt = (
                    update(tasks_table)
                    .where(tasks_table.c.task_id == task_id)
                    .values(**values_to_update)
                )
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(f"Updated task {task_id} status to {status}.")
                    return True
                logger.warning(
                    f"Task {task_id} not found or status unchanged when updating to {status}."
                )
                return False
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in update_task_status (attempt {attempt + 1}/{max_db_retries}): {e}. Retrying..."
            )
            if attempt == max_db_retries - 1:
                logger.error(
                    f"Max retries exceeded for update_task_status({task_id}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(
                f"Non-retryable error in update_task_status({task_id}): {e}",
                exc_info=True,
            )
            raise
    raise RuntimeError(
        f"Database operation failed for update_task_status({task_id}) after multiple retries"
    )


async def reschedule_task_for_retry(
    task_id: str, next_scheduled_at: datetime, new_retry_count: int, error: str
) -> bool:
    """Reschedules a task for retry, handles retries."""
    max_db_retries = 3
    base_delay = 0.5

    if next_scheduled_at.tzinfo is None:
        raise ValueError("next_scheduled_at must be timezone-aware")

    for attempt in range(max_db_retries):
        try:
            async with engine.connect() as conn:
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
                result = await conn.execute(stmt)
                await conn.commit()
                if result.rowcount > 0:
                    logger.info(
                        f"Rescheduled task {task_id} for retry {new_retry_count} at {next_scheduled_at}."
                    )
                    return True
                logger.warning(f"Task {task_id} not found when rescheduling for retry.")
                return False
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in reschedule_task_for_retry (attempt {attempt + 1}/{max_db_retries}): {e}. Retrying..."
            )
            if attempt == max_db_retries - 1:
                logger.error(
                    f"Max retries exceeded for reschedule_task_for_retry({task_id}). Raising error."
                )
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                f"Non-retryable error in reschedule_task_for_retry({task_id}): {e}",
                exc_info=True,
            )
            raise
    raise RuntimeError(
        f"Database operation failed for reschedule_task_for_retry({task_id}) after multiple retries"
    )


async def get_all_tasks(limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves tasks, ordered by creation descending, handles retries."""
    max_db_retries = 3
    base_delay = 0.5
    stmt = select(tasks_table).order_by(tasks_table.c.created_at.desc()).limit(limit)

    for attempt in range(max_db_retries):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(stmt)
                rows = result.fetchall()
                return [row._mapping for row in rows]
        except DBAPIError as e:
            logger.warning(
                f"DBAPIError in get_all_tasks (attempt {attempt + 1}/{max_db_retries}): {e}. Retrying..."
            )
            if attempt == max_db_retries - 1:
                logger.error("Max retries exceeded for get_all_tasks. Raising error.")
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, base_delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Non-retryable error in get_all_tasks: {e}", exc_info=True)
            raise
    raise RuntimeError(
        "Database operation failed for get_all_tasks after multiple retries"
    )
