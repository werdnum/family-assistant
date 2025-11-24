"""
Tests for race conditions in task processing.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import MockClock

logger = logging.getLogger(__name__)


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
        timezone_str="UTC",
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
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        clock=clock_b,
        handler_timeout=600,
    )
    worker_b.worker_id = "worker_b"

    # Enqueue task
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.tasks.enqueue(
            task_id="race_task_prevented",
            task_type="race_test",
            payload={},
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

    # 3. Run Worker B.
    wake_event_b = asyncio.Event()
    wake_event_b.set()
    task_b = asyncio.create_task(worker_b.run(wake_event_b))

    # Wait a bit to ensure B had time to check
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Simulating wait for race condition check
    await asyncio.sleep(1.0)

    # execution_count should be 0 (A is waiting, B shouldn't have run)
    assert execution_count == 0, (
        "Worker B executed the task prematurely! Stale task race condition detected."
    )
    logger.info("Worker B did not execute the task (correct behavior).")

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
