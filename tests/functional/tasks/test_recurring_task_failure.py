import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from tests.helpers import wait_for_condition

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_recurring_task_failure_continues_recurrence(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that a failing recurring task reschedules the next instance."""

    # Create worker using the fixture factory with short timeout
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        handler_timeout=1.0,
    )
    assert worker.engine is not None

    # Handler that always raises exception
    async def failing_handler(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Testing arbitrary payload
        payload: dict[str, Any],
    ) -> None:
        raise ValueError("Task intentionally failed")

    worker.register_task_handler("fail_recur", failing_handler)

    # Create recurring task with NO retries allowed (for speed)
    async with DatabaseContext(engine=worker.engine) as db_context:
        await db_context.tasks.enqueue(
            task_id="recur_fail_test",
            task_type="fail_recur",
            payload={},
            max_retries_override=0,  # Fail immediately
            recurrence_rule="FREQ=MINUTELY;INTERVAL=1",
        )

    # Give time for task to be committed to database
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Ensuring database commit before worker query
    await asyncio.sleep(0.1)

    # Wake up worker to process task
    new_task_event.set()

    # Poll until both conditions are met, using fresh DatabaseContext each time.
    # This is necessary because with SQLite's StaticPool, we need a fresh context
    # to see committed data from concurrent transactions.
    async def check_conditions() -> bool:
        async with DatabaseContext(engine=worker.engine) as db_context:
            # Check original task status
            stmt = select(tasks_table).where(tasks_table.c.task_id == "recur_fail_test")
            tasks = await db_context.fetch_all(stmt)
            task = tasks[0] if tasks else None
            task_failed = task is not None and task["status"] == "failed"

            # Check for recurrence task
            recur_stmt = select(tasks_table).where(
                tasks_table.c.task_id.like("recur_fail_test_recur_%")
            )
            recur_tasks = await db_context.fetch_all(recur_stmt)
            recurrence_exists = len(recur_tasks) >= 1

            return task_failed and recurrence_exists

    await wait_for_condition(
        check_conditions,
        timeout_seconds=5.0,
        error_message="Original task should fail and recurring task should be created",
    )

    # Verify the original task has recurrence rule set
    async with DatabaseContext(engine=worker.engine) as db_context:
        stmt = select(tasks_table).where(tasks_table.c.task_id == "recur_fail_test")
        tasks = await db_context.fetch_all(stmt)
        task = tasks[0] if tasks else None
        assert task is not None
        assert task["recurrence_rule"] is not None
