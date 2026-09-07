"""Tests for the metrics fed from the task queue's enqueue and worker paths."""

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext


def _sample(name: str, labels: Mapping[str, str]) -> float:
    """The current value of one sample, treating "never observed" as zero."""
    return REGISTRY.get_sample_value(name, dict(labels)) or 0.0


@pytest.fixture
def task_type() -> str:
    """A task type unique to the test.

    The registry is process-global and shared with every other test in the
    worker, so a per-test label is what makes an assertion about an absolute
    counter value honest without resetting global state.
    """
    return f"metrics_probe_{uuid.uuid4().hex[:8]}"


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


@pytest.mark.asyncio
async def test_enqueue_counts_the_task_it_wrote(
    db_engine: AsyncEngine, task_type: str
) -> None:
    """Every producer is counted, because every producer goes through enqueue."""
    db = Database(db_engine)

    await db.tasks.enqueue(task_id=f"{task_type}_1", task_type=task_type)
    await db.tasks.enqueue(task_id=f"{task_type}_2", task_type=task_type)

    assert (
        _sample("family_assistant_tasks_enqueued_total", {"task_type": task_type})
        == 2.0
    )


@pytest.mark.asyncio
async def test_a_task_that_ran_is_counted_as_completed(
    db_engine: AsyncEngine, task_type: str
) -> None:
    """The success path counts the execution and times the handler."""
    db = Database(db_engine)
    worker = _worker(db_engine)

    async def handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        return None

    worker.register_task_handler(task_type, handler)
    await db.tasks.enqueue(task_id=task_type, task_type=task_type)
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[task_type],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    assert (
        _sample(
            "family_assistant_tasks_processed_total",
            {"task_type": task_type, "outcome": "completed"},
        )
        == 1.0
    )
    assert (
        _sample(
            "family_assistant_task_duration_seconds_count", {"task_type": task_type}
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_a_failing_task_with_retries_left_is_counted_as_retried(
    db_engine: AsyncEngine, task_type: str
) -> None:
    """A failure the queue will try again is its own outcome, not a failure."""
    db = Database(db_engine)
    worker = _worker(db_engine)

    async def handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("transient")

    worker.register_task_handler(task_type, handler)
    await db.tasks.enqueue(task_id=task_type, task_type=task_type)
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[task_type],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    assert (
        _sample(
            "family_assistant_tasks_processed_total",
            {"task_type": task_type, "outcome": "retried"},
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_a_failing_task_out_of_retries_is_counted_as_failed(
    db_engine: AsyncEngine, task_type: str
) -> None:
    """An execution the queue gives up on is counted once, as failed."""
    db = Database(db_engine)
    worker = _worker(db_engine)

    async def handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("permanent")

    worker.register_task_handler(task_type, handler)
    await db.tasks.enqueue(
        task_id=task_type, task_type=task_type, max_retries_override=0
    )
    task = await db.tasks.dequeue(
        worker_id="worker",
        task_types=[task_type],
        current_time=worker.clock.now(),
    )
    assert task is not None

    await worker._process_task(db, task, asyncio.Event())

    assert (
        _sample(
            "family_assistant_tasks_processed_total",
            {"task_type": task_type, "outcome": "failed"},
        )
        == 1.0
    )
