"""A recurring system task has to come back after the occurrence it finished.

System tasks keep one row across occurrences — the recurrence and the startup
setup both re-enqueue under the same ``system_...`` id — so the upsert that
schedules the next occurrence is the only thing that can hand the row back to
the queue. Dequeue selects pending (or stale processing) rows, so a row left
``done`` is a task that has silently stopped running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from family_assistant.storage.database import Database
from family_assistant.storage.tasks import tasks_table

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

TASK_ID = "system_probe_cleanup_daily"
RECURRENCE = "FREQ=DAILY;BYHOUR=3;BYMINUTE=0"


async def _enqueue_probe(db_context: Database, *, scheduled_at: datetime) -> None:
    await db_context.tasks.enqueue(
        task_id=TASK_ID,
        task_type="probe_cleanup",
        payload={},
        scheduled_at=scheduled_at,
        recurrence_rule=RECURRENCE,
        max_retries_override=5,
    )


async def _row(db_context: Database) -> dict:
    row = await db_context.fetch_one(
        select(tasks_table).where(tasks_table.c.task_id == TASK_ID)
    )
    assert row is not None
    return dict(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("finished_status", ["done", "failed"])
async def test_next_occurrence_returns_a_finished_row_to_the_queue(
    db_engine: AsyncEngine, finished_status: str
) -> None:
    db_context = Database(engine=db_engine)
    await _enqueue_probe(db_context, scheduled_at=datetime.now(UTC))
    await db_context.tasks.update_status(task_id=TASK_ID, status=finished_status)

    next_run = datetime.now(UTC) + timedelta(days=1)
    await _enqueue_probe(db_context, scheduled_at=next_run)

    row = await _row(db_context)
    assert row["status"] == "pending"
    assert row["retry_count"] == 0
    assert row["locked_by"] is None
    assert row["locked_at"] is None


@pytest.mark.asyncio
async def test_upsert_does_not_resurrect_a_running_occurrence(
    db_engine: AsyncEngine,
) -> None:
    """A startup upsert must not hand a worker's in-flight occurrence back out."""
    db_context = Database(engine=db_engine)
    await _enqueue_probe(db_context, scheduled_at=datetime.now(UTC))
    claimed = await db_context.tasks.dequeue(
        worker_id="worker-1",
        task_types=["probe_cleanup"],
        current_time=datetime.now(UTC),
    )
    assert claimed is not None
    assert claimed["task_id"] == TASK_ID

    await _enqueue_probe(db_context, scheduled_at=datetime.now(UTC) + timedelta(days=1))

    row = await _row(db_context)
    assert row["status"] == "processing"
    assert row["locked_by"] == "worker-1"
