import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import Clock
from tests.helpers import wait_for_condition

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_recurring_task_rescheduled_manual_retry_preserves_schedule(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: Clock,
) -> None:
    """Test that a recurring task, when manually retried (rescheduled), preserves its original schedule cycle."""

    # Create worker
    worker, new_task_event, _ = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    assert worker.engine is not None

    # Dummy handler
    async def dummy_handler(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - Testing arbitrary payload
        payload: dict[str, Any],
    ) -> None:
        pass

    worker.register_task_handler("dummy_recur", dummy_handler)

    # 1. Create a task scheduled for 8:00 PM
    # For test simplicity, we'll set it to a time in the past relative to "now"
    base_time = mock_clock.now().replace(
        hour=20, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)

    # Ensure base_time is in UTC for the DB
    base_time_utc = base_time.astimezone(UTC)

    task_id = f"manual_retry_test_{uuid.uuid4()}"

    async with DatabaseContext(engine=worker.engine) as db_context:
        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="dummy_recur",
            payload={},
            scheduled_at=base_time_utc,
            recurrence_rule="FREQ=DAILY",  # Daily at 8 PM implied by start time
        )

    # 2. Simulate manual retry:
    # User notices task failed/didn't run, and reschedules it for "now" (e.g., 9:26 PM)
    retry_time = base_time.replace(hour=21, minute=26)
    retry_time_utc = retry_time.astimezone(UTC)

    async with DatabaseContext(engine=worker.engine) as db_context:
        # Simulate what "reschedule for retry" does - updating scheduled_at
        # NOTE: manually_retry_task should be used if testing manual retry, but here we simulate
        # the effect (updating scheduled_at) using reschedule_for_retry to be generic, or we can use manually_retry_task if we have the internal ID.
        # But wait, manually_retry_task requires internal ID. reschedule_for_retry uses task_id.
        # The user action likely triggers one of these.
        # reschedule_for_retry is what I updated.
        await db_context.tasks.reschedule_for_retry(
            task_id=task_id,
            next_scheduled_at=retry_time_utc,
            new_retry_count=0,  # Reset retries
            error="Manual retry",
        )
        # Also ensure status is pending (reschedule_for_retry sets it to pending)

    # 3. Wake worker to process the "retried" task
    new_task_event.set()

    # 4. Wait for processing to complete and next instance to be scheduled
    async def check_recurrence() -> bool:
        async with DatabaseContext(engine=worker.engine) as db_context:
            # Check for recurrence task
            recur_stmt = (
                select(tasks_table)
                .where(tasks_table.c.original_task_id == task_id)
                .where(tasks_table.c.task_id != task_id)
            )
            recur_tasks = await db_context.fetch_all(recur_stmt)
            return len(recur_tasks) >= 1

    await wait_for_condition(
        check_recurrence,
        timeout_seconds=5.0,
        error_message="Recurring task should be created",
    )

    # 5. Verify the scheduled time of the NEXT instance
    async with DatabaseContext(engine=worker.engine) as db_context:
        recur_stmt = (
            select(tasks_table)
            .where(tasks_table.c.original_task_id == task_id)
            .where(tasks_table.c.task_id != task_id)
        )
        recur_tasks = await db_context.fetch_all(recur_stmt)
        next_task = recur_tasks[0]

        next_scheduled_at = next_task["scheduled_at"].replace(tzinfo=UTC)

        # Expected: 8:00 PM next day (relative to original base_time)
        # NOT 9:26 PM next day

        # Original was Day X 8:00 PM.
        # Retried at Day X 9:26 PM.
        # Next should be Day X+1 8:00 PM.

        expected_time = base_time_utc + timedelta(days=1)

        logger.info(f"Original Scheduled: {base_time_utc}")
        logger.info(f"Retried At: {retry_time_utc}")
        logger.info(f"Next Scheduled: {next_scheduled_at}")
        logger.info(f"Expected: {expected_time}")

        # Use a tolerance for comparison if needed, but it should be exact minute
        assert next_scheduled_at.hour == expected_time.hour
        assert next_scheduled_at.minute == expected_time.minute
