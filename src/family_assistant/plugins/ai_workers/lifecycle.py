"""Keeping recorded worker tasks in step with their backend."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.plugins.ai_workers.backend import WorkerStatus

if TYPE_CHECKING:
    from family_assistant.plugins.ai_workers.backend import WorkerBackend
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = {
    WorkerStatus.SUCCESS,
    WorkerStatus.FAILED,
    WorkerStatus.TIMEOUT,
    WorkerStatus.CANCELLED,
}

_STATUS_MAP = {
    WorkerStatus.SUCCESS: "success",
    WorkerStatus.FAILED: "failed",
    WorkerStatus.TIMEOUT: "timeout",
    WorkerStatus.CANCELLED: "cancelled",
}

TERMINAL_DB_STATUSES = set(_STATUS_MAP.values())


async def reconcile_stale_tasks(db_context: Database, backend: WorkerBackend) -> int:
    """Check active DB tasks against backend state and mark stale ones as failed.

    For each task with status "submitted" or "running" in the DB:
    - If it has no job_name, mark as failed (spawn never completed)
    - If backend reports a terminal status, update DB accordingly
    - If backend still shows active, leave it alone

    Returns:
        Number of tasks reconciled
    """
    active_tasks = await db_context.worker_tasks.get_active_tasks()
    if not active_tasks:
        return 0

    reconciled = 0
    for task in active_tasks:
        task_id = task["task_id"]
        job_name = task.get("job_name")

        if not job_name:
            await db_context.worker_tasks.update_task_status(
                task_id=task_id,
                status="failed",
                error_message="Task has no job_name — spawn never completed",
            )
            reconciled += 1
            logger.info(f"Reconciled task {task_id}: no job_name, marked failed")
            continue

        try:
            result = await backend.get_task_status(job_name)
        except Exception:
            logger.warning(
                f"Failed to check backend status for task {task_id} (job {job_name})",
                exc_info=True,
            )
            continue

        if result.status in _TERMINAL_STATUSES:
            db_status = _STATUS_MAP.get(result.status, "failed")
            await db_context.worker_tasks.update_task_status(
                task_id=task_id,
                status=db_status,
                error_message=result.error_message
                or f"Reconciled from backend status: {result.status.value}",
                exit_code=result.exit_code,
            )
            reconciled += 1
            logger.info(
                f"Reconciled task {task_id}: backend status {result.status.value} → {db_status}"
            )

    if reconciled:
        logger.info(f"Reconciled {reconciled} stale worker tasks")
    return reconciled
