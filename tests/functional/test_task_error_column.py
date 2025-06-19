"""Test that the error column fix works correctly in the tasks repository."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext


@pytest.mark.asyncio
async def test_reschedule_for_retry_uses_correct_error_column(
    test_db_engine: AsyncEngine,
) -> None:
    """Test that reschedule_for_retry uses 'error' column, not 'last_error'."""
    async with DatabaseContext() as db:
        # Create a task
        task_id = "test_error_column"
        await db.tasks.enqueue(
            task_id=task_id,
            task_type="test_task",
            payload={"test": "data"},
        )

        # Reschedule it for retry with an error message
        next_time = datetime.now(timezone.utc) + timedelta(minutes=1)
        error_msg = "Test error message"

        # This should work now that we fixed the column name
        success = await db.tasks.reschedule_for_retry(
            task_id=task_id,
            next_scheduled_at=next_time,
            new_retry_count=1,
            error=error_msg,
        )

        assert success is True

        # Verify the error was stored correctly
        tasks = await db.tasks.get_all(limit=1)
        assert len(tasks) == 1
        task = tasks[0]

        assert task["error"] == error_msg
        assert task["retry_count"] == 1
        assert task["status"] == "pending"


@pytest.mark.asyncio
async def test_manually_retry_clears_error_column(test_db_engine: AsyncEngine) -> None:
    """Test that manually_retry clears the 'error' column correctly."""
    async with DatabaseContext() as db:
        # Create a failed task with an error
        task_id = "test_manual_retry_error"
        await db.tasks.enqueue(
            task_id=task_id,
            task_type="test_task",
            payload={"test": "data"},
            max_retries_override=0,  # No automatic retries
        )

        # Mark it as failed with an error
        await db.tasks.update_status(
            task_id=task_id,
            status="failed",
            error="Original error message",
        )

        # Get the internal task ID
        tasks = await db.tasks.get_all(limit=1)
        internal_id = tasks[0]["id"]

        # Manually retry the task
        success = await db.tasks.manually_retry(internal_id)
        assert success is True

        # Verify the error was cleared
        tasks = await db.tasks.get_all(limit=1)
        task = tasks[0]

        assert task["error"] is None  # Error should be cleared
        assert task["status"] == "pending"
        assert task["max_retries"] == 1  # Should be incremented
