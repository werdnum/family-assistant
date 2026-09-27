"""
Tests for race conditions in task processing.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import MockClock

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_stale_task_cutoff_is_fifteen_minutes(db_engine: AsyncEngine) -> None:
    """The dequeue stale cutoff protects a lock for 15 minutes, no longer.

    Deterministic and worker-free: drives ``TaskRepository.dequeue`` directly
    with fixed timestamps, so there is no polling loop whose timing could make
    the negative assertion (task still locked) pass vacuously.
    """
    db_context = Database(engine=db_engine)
    start_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
    await db_context.tasks.enqueue(
        task_id="stale_cutoff_probe",
        task_type="race_test",
        payload={},
        priority=TaskPriority.INTERACTIVE,
    )

    claimed = await db_context.tasks.dequeue(
        worker_id="worker_a",
        task_types=["race_test"],
        current_time=start_time,
    )
    assert claimed is not None
    assert claimed["task_id"] == "stale_cutoff_probe"

    still_locked = await db_context.tasks.dequeue(
        worker_id="worker_b",
        task_types=["race_test"],
        current_time=start_time + timedelta(minutes=15) - timedelta(microseconds=1),
    )
    assert still_locked is None, "Lock must remain held until the 15-minute cutoff"

    now_stale = await db_context.tasks.dequeue(
        worker_id="worker_b",
        task_types=["race_test"],
        current_time=start_time + timedelta(minutes=15),
    )
    assert now_stale is not None, (
        "A lock held past the 15-minute cutoff must become reclaimable"
    )
    assert now_stale["task_id"] == "stale_cutoff_probe"


@pytest.mark.asyncio
async def test_stale_task_pickup_prevented_by_timeout_buffer(
    db_engine: AsyncEngine,
) -> None:
    """
    Test that a task running for 6 minutes is NOT picked up by another worker
    because the stale timeout buffer is sufficient (15 minutes).

    This ensures that we don't have a race condition where a worker is still running
    a task (e.g. nearing the 5-minute handler timeout) but another worker considers
    it stale and picks it up, leading to duplicate execution.
    """
    start_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)

    # Create two clocks starting at same time
    clock_a = MockClock(start_time)
    clock_b = MockClock(start_time)

    # Events for coordination
    shutdown_event = asyncio.Event()

    worker_a_event = asyncio.Event()  # To signal worker A to proceed
    worker_a_waiting = (
        asyncio.Event()
    )  # To signal test that worker A is waiting inside handler

    # Shared counter to verify execution
    execution_count = 0

    # Setup workers
    worker_a = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        clock=clock_a,
        handler_timeout=600,  # Long timeout so it doesn't self-cancel during test
    )
    worker_a.worker_id = "worker_a"

    worker_b = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        clock=clock_b,
        handler_timeout=600,
    )
    worker_b.worker_id = "worker_b"

    # Enqueue task
    db_context = Database(engine=db_engine)
    await db_context.tasks.enqueue(
        task_id="race_task_prevented",
        task_type="race_test",
        payload={},
        priority=TaskPriority.INTERACTIVE,
    )

    # Handler for Worker A
    async def handler_a(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Test payload
        payload: dict[str, Any],
    ) -> None:
        nonlocal execution_count
        logger.info("Worker A Handler started")
        worker_a_waiting.set()
        # Wait until test signals us to proceed
        await worker_a_event.wait()
        execution_count += 1
        logger.info("Worker A Handler finished")

    # Handler for Worker B
    async def handler_b(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Test payload
        payload: dict[str, Any],
    ) -> None:
        nonlocal execution_count
        logger.info("Worker B Handler started")
        execution_count += 1
        logger.info("Worker B Handler finished")

    worker_a.register_task_handler("race_test", handler_a)
    worker_b.register_task_handler("race_test", handler_b)

    # 1. Run Worker A. It should pick up the task and wait.
    wake_event_a = asyncio.Event()
    wake_event_a.set()
    task_a = asyncio.create_task(worker_a.run(wake_event_a))

    # Wait for A to pick up and wait
    try:
        await asyncio.wait_for(worker_a_waiting.wait(), timeout=5.0)
    except TimeoutError:
        logger.error("Worker A did not pick up task in time")
        shutdown_event.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task_a
        raise

    logger.info("Worker A has locked the task and is waiting.")

    # 2. Advance time for Worker B to 6 minutes later.
    # If stale_timeout was 5 minutes (old value), this would make the task stale.
    # With stale_timeout = 15 minutes (new value), the task should remain locked.
    clock_b.advance(timedelta(minutes=6))

    # Prove the lock resists B's would-be claim deterministically, by driving
    # the same dequeue B's loop would use directly rather than racing an
    # in-process poll against a fixed sleep. A dequeue is exclusive, so calling
    # it here does not create a false negative: only a genuinely stale lock
    # would let this steal the task.
    premature_claim = await db_context.tasks.dequeue(
        worker_id="worker_b_probe",
        task_types=["race_test"],
        current_time=clock_b.now(),
    )
    assert premature_claim is None, (
        "Task became stealable after only 6 minutes; stale task race condition detected."
    )
    assert execution_count == 0
    logger.info("Worker B's dequeue correctly found no claimable task.")

    # 3. Run Worker B, to verify the full worker loop also leaves the task alone.
    wake_event_b = asyncio.Event()
    wake_event_b.set()
    task_b = asyncio.create_task(worker_b.run(wake_event_b))

    # 4. Now signal Worker A to finish
    worker_a_event.set()

    # Wait for A to finish
    # We poll execution_count
    for _ in range(50):
        if execution_count >= 1:
            break
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling
        await asyncio.sleep(0.1)

    logger.info(f"Final execution count: {execution_count}")

    # Stop workers
    shutdown_event.set()
    wake_event_a.set()
    wake_event_b.set()

    await asyncio.gather(task_a, task_b)

    # Assert correct behavior: only executed once
    assert execution_count == 1, f"Task executed {execution_count} times, expected 1"
