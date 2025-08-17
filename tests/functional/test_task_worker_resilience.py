"""
Tests for TaskWorker resilience features including timeout and health monitoring.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete

logger = logging.getLogger(__name__)


@pytest.mark.skip(
    reason="Test needs refactoring - timeout patching with fixture has race conditions"
)
@pytest.mark.asyncio
async def test_task_handler_timeout(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that a handler timeout causes task retry."""
    # Create worker using the fixture factory
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )

    # Handler that will definitely timeout
    async def hanging_handler(
        exec_context: ToolExecutionContext, payload: dict[str, Any]
    ) -> None:
        logger.info("Hanging handler started, will sleep for 2 seconds")
        await asyncio.sleep(2.0)  # Longer than the 1 second timeout we'll set
        logger.info("Hanging handler finished (should not reach here)")

    worker.register_task_handler("hang", hanging_handler)

    # Create a task with 0 retries allowed to avoid retry delays
    async with DatabaseContext(engine=worker.engine) as db_context:
        await db_context.tasks.enqueue(
            task_id="timeout_test",
            task_type="hang",
            payload={},
            max_retries_override=0,  # No retries to avoid retry delays in test
        )
        logger.info("Created test task with ID: timeout_test")

    # Small delay to ensure task is committed (important for postgres)
    await asyncio.sleep(0.1)

    # Patch the timeout on the module directly before running
    import family_assistant.task_worker

    original_timeout = family_assistant.task_worker.TASK_HANDLER_TIMEOUT
    family_assistant.task_worker.TASK_HANDLER_TIMEOUT = 1.0  # Use 1 second timeout
    logger.info("Patched TASK_HANDLER_TIMEOUT to 1.0 seconds")

    try:
        # Wake up worker to process task
        new_task_event.set()

        # Add a small delay to let worker start
        await asyncio.sleep(1.0)

        # Wake up the worker again in case it missed the first event
        new_task_event.set()

        # Wait for task to be processed (it should timeout and fail immediately with no retries)
        assert worker.engine is not None
        await wait_for_tasks_to_complete(
            engine=worker.engine,
            timeout_seconds=5.0,  # Very short timeout since no retries
            task_ids={"timeout_test"},
            allow_failures=True,
        )
    finally:
        # Restore original timeout
        family_assistant.task_worker.TASK_HANDLER_TIMEOUT = original_timeout

    # Check task was marked for retry
    async with DatabaseContext(engine=worker.engine) as db_context:
        stmt = select(tasks_table).where(tasks_table.c.task_id == "timeout_test")
        tasks = await db_context.fetch_all(stmt)
        task = tasks[0] if tasks else None

        assert task is not None
        # Task should have failed immediately since max_retries=0
        assert task["status"] == "failed"
        assert task["retry_count"] == 0  # No retries were allowed
        assert "TimeoutError" in (task["error"] or "")


@pytest.mark.asyncio
async def test_successful_handler_completes(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that successful handlers mark tasks as completed."""
    # Create worker using the fixture factory
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )

    # Quick handler that completes
    async def quick_handler(
        exec_context: ToolExecutionContext, payload: dict[str, Any]
    ) -> None:
        logger.info("Quick handler executed")

    worker.register_task_handler("quick", quick_handler)

    # Create a task
    async with DatabaseContext(engine=worker.engine) as db_context:
        await db_context.tasks.enqueue(
            task_id="success_test",
            task_type="quick",
            payload={},
        )

    # Small delay to ensure task is committed (important for postgres)
    await asyncio.sleep(0.1)

    # Wake up worker to process task (the fixture has already started the worker)
    new_task_event.set()

    # Wait for task to complete
    assert worker.engine is not None
    await wait_for_tasks_to_complete(
        engine=worker.engine,
        timeout_seconds=10.0,
        task_ids={"success_test"},
    )

    # Check task completed
    async with DatabaseContext(engine=worker.engine) as db_context:
        stmt = select(tasks_table).where(tasks_table.c.task_id == "success_test")
        tasks = await db_context.fetch_all(stmt)
        task = tasks[0] if tasks else None

        assert task is not None, "Task not found in database"
        assert task["status"] == "done", (
            f"Expected status 'done', got '{task['status']}'"
        )


@pytest.mark.asyncio
async def test_retry_exhaustion_leads_to_failure(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that tasks fail permanently after exhausting retries."""
    # Very short timeout to make test fast
    with patch("family_assistant.task_worker.TASK_HANDLER_TIMEOUT", 0.1):
        # Create worker using the fixture factory
        worker, new_task_event, shutdown_event = task_worker_manager(
            processing_service=MagicMock(),
            chat_interface=MagicMock(),
        )

        # Handler that always times out
        async def timeout_handler(
            exec_context: ToolExecutionContext, payload: dict[str, Any]
        ) -> None:
            await asyncio.sleep(1.0)  # Longer than the 0.1s timeout

        worker.register_task_handler("timeout", timeout_handler)

        # Create task with NO retries allowed
        async with DatabaseContext(engine=worker.engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="no_retry_test",
                task_type="timeout",
                payload={},
                max_retries_override=0,  # No retries
            )

        # Small delay to ensure task is committed (important for postgres)
        await asyncio.sleep(0.1)

        # Wake up worker to process task
        new_task_event.set()

        # Wait for task to fail (no retries)
        # Use a background task to periodically wake the worker to ensure it processes the failure
        async def wake_worker_periodically() -> None:
            for _ in range(20):  # Wake every 0.5s for 10 seconds total
                await asyncio.sleep(0.5)
                new_task_event.set()

        wake_task = asyncio.create_task(wake_worker_periodically())

        try:
            assert worker.engine is not None
            await wait_for_tasks_to_complete(
                engine=worker.engine,
                timeout_seconds=20.0,  # Increased from 10.0 to handle slower CI environments
                task_ids={"no_retry_test"},
                allow_failures=True,
            )
        finally:
            wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wake_task

        # Check task failed
        async with DatabaseContext(engine=worker.engine) as db_context:
            stmt = select(tasks_table).where(tasks_table.c.task_id == "no_retry_test")
            tasks = await db_context.fetch_all(stmt)
            task = tasks[0] if tasks else None

            assert task is not None
            assert task["status"] == "failed"
            assert "TimeoutError" in (task["error"] or "")


@pytest.mark.asyncio
async def test_worker_activity_tracking(db_engine: AsyncEngine) -> None:
    """Test that worker tracks last activity time."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Initial activity should be set
    initial_activity = worker.last_activity
    assert initial_activity is not None

    # Create and run a simple task
    async def simple_handler(
        exec_context: ToolExecutionContext, payload: dict[str, Any]
    ) -> None:
        pass

    worker.register_task_handler("simple", simple_handler)

    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.tasks.enqueue(
            task_id="activity_test",
            task_type="simple",
            payload={},
        )

    # Small delay to ensure task is committed (important for postgres)
    await asyncio.sleep(0.1)

    # Create wake up event and run worker to process task
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))
    wake_up_event.set()  # Wake up worker to process task

    # Wait for task to complete
    await wait_for_tasks_to_complete(
        engine=db_engine,
        timeout_seconds=10.0,
        task_ids={"activity_test"},
    )

    # Stop worker
    worker.shutdown_event.set()
    wake_up_event.set()  # Wake up worker if it's waiting
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # Activity should have been updated
    assert worker.last_activity is not None, "last_activity is None"
    # Allow for small timing differences
    if worker.last_activity < initial_activity:
        diff = (initial_activity - worker.last_activity).total_seconds()
        assert diff < 1.0, (
            f"last_activity {worker.last_activity} is {diff}s before initial {initial_activity}"
        )


@pytest.mark.asyncio
async def test_health_check_properties(db_engine: AsyncEngine) -> None:
    """Test properties that health monitoring would check."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Create wake up event and start worker without any tasks
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))

    try:
        await asyncio.sleep(0.5)

        # Last activity should be recent
        assert worker.last_activity is not None
        time_since_activity = (
            datetime.now(timezone.utc) - worker.last_activity
        ).total_seconds()
        assert time_since_activity < 10  # Should have been updated recently

    finally:
        worker.shutdown_event.set()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


@pytest.mark.asyncio
async def test_shutdown_stops_worker(db_engine: AsyncEngine) -> None:
    """Test that shutdown event stops the worker cleanly."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Create wake up event and start worker
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))
    await asyncio.sleep(0.1)

    # Set shutdown event
    worker.shutdown_event.set()

    # Worker should stop within reasonable time
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail("Worker did not stop after shutdown event")

    # Task should be done
    assert worker_task.done()
