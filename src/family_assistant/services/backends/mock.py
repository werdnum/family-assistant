"""Mock backend for testing worker tasks without infrastructure.

This backend simulates task execution for testing purposes, allowing
full end-to-end testing of the worker tool chain without Kubernetes or Docker.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from family_assistant.services.worker_backend import WorkerStatus, WorkerTaskResult

logger = logging.getLogger(__name__)


@dataclass
class MockTask:
    """Represents a mock task in the mock backend."""

    task_id: str
    prompt_path: str
    output_dir: str
    webhook_url: str
    model: str
    timeout_minutes: int
    context_paths: list[str]
    status: WorkerStatus = WorkerStatus.PENDING
    job_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    error_message: str | None = None


# Terminal statuses that cannot be changed
_TERMINAL_STATUSES = {
    WorkerStatus.SUCCESS,
    WorkerStatus.FAILED,
    WorkerStatus.TIMEOUT,
    WorkerStatus.CANCELLED,
}


class MockBackend:
    """Mock backend for testing.

    This backend stores tasks in memory and allows tests to control
    task completion via explicit methods.

    Example usage in tests:
        backend = MockBackend()
        job_id = await backend.spawn_task(...)

        # Simulate completion
        backend.complete_task(job_id, success=True, output="Task completed")

        # Or simulate failure
        backend.fail_task(job_id, error="Something went wrong")
    """

    def __init__(self) -> None:
        self._tasks: dict[str, MockTask] = {}
        self._job_counter = 0
        self._auto_complete = False
        self._auto_complete_delay = 0.0

    async def spawn_task(
        self,
        task_id: str,
        prompt_path: str,
        output_dir: str,
        webhook_url: str,
        model: str,
        timeout_minutes: int,
        context_paths: list[str] | None = None,
    ) -> str:
        """Spawn a mock task.

        Returns a mock job ID that can be used to control task completion.
        """
        self._job_counter += 1
        job_id = f"mock-job-{self._job_counter}"

        task = MockTask(
            task_id=task_id,
            prompt_path=prompt_path,
            output_dir=output_dir,
            webhook_url=webhook_url,
            model=model,
            timeout_minutes=timeout_minutes,
            context_paths=context_paths or [],
            status=WorkerStatus.SUBMITTED,
            job_id=job_id,
        )
        self._tasks[job_id] = task

        logger.info(f"MockBackend: Created task {task_id} with job_id {job_id}")

        if self._auto_complete:
            asyncio.create_task(self._auto_complete_task(job_id))

        return job_id

    async def _auto_complete_task(self, job_id: str) -> None:
        """Auto-complete a task after delay (for testing async flows)."""
        if self._auto_complete_delay > 0:
            await asyncio.sleep(self._auto_complete_delay)
        self.complete_task(
            job_id, success=True, output="Auto-completed by mock backend"
        )

    async def get_task_status(self, job_id: str) -> WorkerTaskResult:
        """Get the status of a mock task."""
        task = self._tasks.get(job_id)
        if not task:
            return WorkerTaskResult(
                status=WorkerStatus.FAILED,
                error_message=f"Task not found: {job_id}",
            )

        return WorkerTaskResult(
            status=task.status,
            exit_code=task.exit_code,
            error_message=task.error_message,
        )

    async def cancel_task(self, job_id: str) -> bool:
        """Cancel a mock task."""
        task = self._tasks.get(job_id)
        if not task:
            return False

        if task.status in _TERMINAL_STATUSES:
            return False

        task.status = WorkerStatus.CANCELLED
        task.completed_at = datetime.now(UTC)
        logger.info(f"MockBackend: Cancelled task {job_id}")
        return True

    def complete_task(
        self,
        job_id: str,
        success: bool = True,
        output: str | None = None,
        exit_code: int | None = None,
    ) -> bool:
        """Manually complete a task (for use in tests).

        Args:
            job_id: The job ID to complete
            success: Whether the task succeeded
            output: Optional output summary
            exit_code: Exit code (defaults to 0 for success, 1 for failure)

        Returns:
            True if task was found and completed, False otherwise
        """
        task = self._tasks.get(job_id)
        if not task:
            return False

        if task.status in _TERMINAL_STATUSES:
            return False

        task.status = WorkerStatus.SUCCESS if success else WorkerStatus.FAILED
        task.completed_at = datetime.now(UTC)
        task.exit_code = exit_code if exit_code is not None else (0 if success else 1)
        if not success and output:
            task.error_message = output

        logger.info(f"MockBackend: Completed task {job_id} with status {task.status}")
        return True

    def fail_task(self, job_id: str, error: str, exit_code: int = 1) -> bool:
        """Manually fail a task (for use in tests).

        Args:
            job_id: The job ID to fail
            error: Error message
            exit_code: Exit code (defaults to 1)

        Returns:
            True if task was found and failed, False otherwise
        """
        task = self._tasks.get(job_id)
        if not task:
            return False

        if task.status in _TERMINAL_STATUSES:
            return False

        task.status = WorkerStatus.FAILED
        task.completed_at = datetime.now(UTC)
        task.exit_code = exit_code
        task.error_message = error

        logger.info(f"MockBackend: Failed task {job_id}: {error}")
        return True

    def timeout_task(self, job_id: str) -> bool:
        """Mark a task as timed out (for use in tests).

        Args:
            job_id: The job ID to timeout

        Returns:
            True if task was found and timed out, False otherwise
        """
        task = self._tasks.get(job_id)
        if not task:
            return False

        if task.status in _TERMINAL_STATUSES:
            return False

        task.status = WorkerStatus.TIMEOUT
        task.completed_at = datetime.now(UTC)
        task.error_message = f"Task exceeded timeout of {task.timeout_minutes} minutes"

        logger.info(f"MockBackend: Timed out task {job_id}")
        return True

    def set_running(self, job_id: str) -> bool:
        """Mark a task as running (for use in tests).

        Args:
            job_id: The job ID to mark as running

        Returns:
            True if task was found and updated, False otherwise
        """
        task = self._tasks.get(job_id)
        if not task:
            return False

        if task.status != WorkerStatus.SUBMITTED:
            return False

        task.status = WorkerStatus.RUNNING
        task.started_at = datetime.now(UTC)

        logger.info(f"MockBackend: Task {job_id} is now running")
        return True

    def get_task(self, job_id: str) -> MockTask | None:
        """Get a task by job ID (for inspection in tests)."""
        return self._tasks.get(job_id)

    def get_task_by_task_id(self, task_id: str) -> MockTask | None:
        """Get a task by its task ID (for inspection in tests)."""
        for task in self._tasks.values():
            if task.task_id == task_id:
                return task
        return None

    def enable_auto_complete(self, delay: float = 0.0) -> None:
        """Enable auto-completion of tasks (for testing async flows).

        Args:
            delay: Delay in seconds before auto-completing tasks
        """
        self._auto_complete = True
        self._auto_complete_delay = delay

    def disable_auto_complete(self) -> None:
        """Disable auto-completion of tasks."""
        self._auto_complete = False

    def clear(self) -> None:
        """Clear all tasks (for test cleanup)."""
        self._tasks.clear()
        self._job_counter = 0
