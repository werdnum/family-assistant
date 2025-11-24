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
from tests.helpers import wait_for_tasks_to_complete

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
            recurrence_rule="FREQ=MINUTELY;INTERVAL=1",  # Should run every minute
        )

    # Small delay to ensure task is committed
    await asyncio.sleep(0.1)

    # Wake up worker to process task
    new_task_event.set()

    # Wait for task to fail
    await wait_for_tasks_to_complete(
        engine=worker.engine,
        timeout_seconds=5.0,
        task_ids={"recur_fail_test"},
        allow_failures=True,
    )

    # Check that the original task is failed
    async with DatabaseContext(engine=worker.engine) as db_context:
        stmt = select(tasks_table).where(tasks_table.c.task_id == "recur_fail_test")
        tasks = await db_context.fetch_all(stmt)
        task = tasks[0] if tasks else None

        assert task is not None
        assert task["status"] == "failed"
        assert task["recurrence_rule"] is not None

        # Now check if any NEW task was created (recurrence)
        # The new task ID would start with "recur_fail_test_recur_"
        stmt = select(tasks_table).where(
            tasks_table.c.task_id.like("recur_fail_test_recur_%")
        )
        recur_tasks = await db_context.fetch_all(stmt)

        # Expectation: New task created even if the original failed
        assert len(recur_tasks) == 1, (
            "Recurring task SHOULD have been rescheduled after failure"
        )
