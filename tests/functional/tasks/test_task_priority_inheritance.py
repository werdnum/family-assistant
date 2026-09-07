"""Tests for how a task's lane reaches the work that task enqueues.

Priority is chosen where work enters the queue from outside it and carried, not
re-decided, once inside: the worker puts the dequeued row's lane on the
execution context, and a handler enqueueing more of its own kind of work passes
that value on.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext

CHILD_TASK_TYPE = "priority_inheritance_child"

# ast-grep-ignore: no-dict-any - task payloads are heterogeneous; these tests use empty payloads
TaskPayload = dict[str, Any]


def _worker(db_engine: AsyncEngine) -> TaskWorker:
    return TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),
    )


async def _priority_of(db: Database, task_id: str) -> int:
    row = await db.fetch_one(
        select(tasks_table.c.priority).where(tasks_table.c.task_id == task_id)
    )
    assert row is not None
    return row["priority"]


@pytest.mark.asyncio
async def test_handler_context_carries_the_dequeued_rows_lane(
    db_engine: AsyncEngine,
) -> None:
    """The worker is the one place a running task's lane becomes known."""
    db = Database(db_engine)
    worker = _worker(db_engine)
    seen: list[TaskPriority | None] = []

    async def handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        seen.append(exec_context.task_priority)

    worker.register_task_handler("lane_probe", handler)
    await db.tasks.enqueue(
        task_id="lane_probe",
        task_type="lane_probe",
        priority=TaskPriority.BACKGROUND,
    )
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=["lane_probe"],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    assert seen == [TaskPriority.BACKGROUND]


@pytest.mark.asyncio
async def test_work_a_handler_enqueues_stays_in_its_lane(
    db_engine: AsyncEngine,
) -> None:
    """A continuation inherits rather than re-deciding, so a chain cannot escape."""
    db = Database(db_engine)
    worker = _worker(db_engine)

    async def handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        await exec_context.db_context.tasks.enqueue(
            task_id="continuation",
            task_type=CHILD_TASK_TYPE,
            priority=exec_context.inherited_task_priority(),
        )

    worker.register_task_handler("walking_probe", handler)
    await db.tasks.enqueue(
        task_id="walking_probe",
        task_type="walking_probe",
        priority=TaskPriority.BACKGROUND,
    )
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=["walking_probe"],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    assert await _priority_of(db, "continuation") == TaskPriority.BACKGROUND


@pytest.mark.asyncio
async def test_the_next_occurrence_of_a_recurring_task_keeps_its_lane(
    db_engine: AsyncEngine,
) -> None:
    """Recurrence copies the row, so the lane comes with it."""
    db = Database(db_engine)
    worker = _worker(db_engine)

    async def handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        return None

    worker.register_task_handler("recurring_probe", handler)
    scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.tasks.enqueue(
        task_id="recurring_probe",
        task_type="recurring_probe",
        scheduled_at=scheduled_at,
        recurrence_rule="FREQ=DAILY",
        priority=TaskPriority.BACKGROUND,
    )
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=["recurring_probe"],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    rows = await db.fetch_all(
        select(tasks_table.c.task_id, tasks_table.c.priority).where(
            tasks_table.c.original_task_id == "recurring_probe",
            tasks_table.c.status == "pending",
        )
    )
    assert [row["priority"] for row in rows] == [TaskPriority.BACKGROUND]


@pytest.mark.asyncio
async def test_a_context_outside_a_task_has_no_lane_to_inherit(
    db_engine: AsyncEngine,
) -> None:
    """A producer outside the queue is made to choose rather than guess."""
    exec_context = ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-1",
        user_name="Someone",
        turn_id=None,
        db_context=Database(db_engine),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )

    with pytest.raises(RuntimeError, match="not a task's"):
        exec_context.inherited_task_priority()
