"""The AI worker plugin's background tasks."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import aiofiles.os

from family_assistant.storage.events import (
    WORKER_COMPLETION_EVENT_TYPE,
    EventSourceType,
)

if TYPE_CHECKING:
    from family_assistant.storage.database import Database
    from family_assistant.storage.types import EventListenerDict
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

WORKER_TASK_CLEANUP_TASK_TYPE = "worker_task_cleanup"


class WorkerTaskCleanupPayload(TypedDict, total=False):
    """Payload for worker_task_cleanup tasks."""

    retention_hours: int
    workspace_path: str
    dead_worker_grace_hours: int
    abandoned_listener_hours: int


def _worker_completion_task_id(listener: EventListenerDict) -> str | None:
    """Return the worker task a listener is waiting on, if it is one.

    ``spawn_worker`` arms a one-time webhook listener matching its own task's
    completion event; the task ID it matches on is the only handle back to the
    worker the listener exists for.
    """
    conditions = listener.get("match_conditions") or {}
    if conditions.get("event_type") != WORKER_COMPLETION_EVENT_TYPE:
        return None
    task_id = conditions.get("data.task_id")
    return task_id if isinstance(task_id, str) else None


async def _cleanup_dead_worker_completion_listeners(
    db_context: Database,
    *,
    now: datetime,
    dead_worker_grace_hours: int,
    abandoned_listener_hours: int,
) -> int:
    """Delete worker completion listeners whose worker can no longer report.

    There are two grades of evidence, and they earn different waits.

    We watched the task finish: its own finish time starts the clock, and the
    listener goes once that is past the grace. The grace is what keeps this off
    a completion still being acted on -- the webhook marks a task terminal
    before the event it carried has fired the listener.

    We never watched it finish, because the row is gone or because it is still
    recorded live: the death is inferred, so the listener waits out
    ``abandoned_listener_hours`` of silence. That covers a worker task reaped
    while its own completion was still in flight -- the row retention is
    shorter than this wait -- and a backend that lost a job, leaving a row live
    forever. A row still live must also be past its own deadline, so a task an
    operator gave a fortnight to is not judged by anyone else's clock.
    """
    listeners = await db_context.events.get_untriggered_one_time_listeners(
        created_before=now - timedelta(hours=dead_worker_grace_hours),
        source_id=EventSourceType.webhook,
    )

    waiting: list[tuple[EventListenerDict, str]] = []
    for listener in listeners:
        task_id = _worker_completion_task_id(listener)
        if task_id is not None:
            waiting.append((listener, task_id))

    if not waiting:
        return 0

    quiet_times = await db_context.worker_tasks.get_quiet_times([
        task_id for _, task_id in waiting
    ])
    finished_cutoff = now - timedelta(hours=dead_worker_grace_hours)
    abandoned_cutoff = now - timedelta(hours=abandoned_listener_hours)

    doomed: list[int] = []
    for listener, task_id in waiting:
        quiet = quiet_times.get(task_id)
        if quiet is not None and not quiet.is_live:
            if quiet.at < finished_cutoff:
                doomed.append(listener["id"])
        elif listener["created_at"] < abandoned_cutoff and (
            quiet is None or now > quiet.at
        ):
            doomed.append(listener["id"])

    return await db_context.events.delete_event_listeners_by_id(doomed)


async def handle_worker_task_cleanup(
    exec_context: ToolExecutionContext,
    payload: WorkerTaskCleanupPayload,
) -> None:
    """Task handler for collecting what finished worker tasks leave behind.

    In order: completion listeners whose worker can no longer report, stale
    tasks (marked failed), old task records, and, when the payload names the
    workspace, old task directories under its ``tasks/``.

    Payload can include:
        retention_hours: How long finished task records and directories are
            kept (default: 48)
        workspace_path: The shared workspace whose task directories to sweep
        dead_worker_grace_hours: Age a worker completion listener must reach
            before its worker's state is taken as final (default: 24)
        abandoned_listener_hours: Age at which an untriggered worker completion
            listener is dropped regardless of its worker's status (default: 168)
    """
    retention_hours = payload.get("retention_hours", 48)
    workspace_path = payload.get("workspace_path")
    dead_worker_grace_hours = int(payload.get("dead_worker_grace_hours", 24))
    abandoned_listener_hours = int(payload.get("abandoned_listener_hours", 24 * 7))

    logger.info(f"Starting worker task cleanup (retention: {retention_hours} hours)")

    db_deleted = 0
    dirs_deleted = 0
    stale_marked = 0
    listeners_deleted = 0

    async def clean_up_worker_tasks() -> None:
        nonlocal db_deleted, dirs_deleted, stale_marked, listeners_deleted
        # Before any record is deleted: a listener's fate depends on its task.
        listeners_deleted = await _cleanup_dead_worker_completion_listeners(
            exec_context.db_context,
            now=datetime.now(UTC),
            dead_worker_grace_hours=dead_worker_grace_hours,
            abandoned_listener_hours=abandoned_listener_hours,
        )

        # Step 0: Mark stale tasks as failed before cleanup
        stale_marked = await exec_context.db_context.worker_tasks.mark_stale_tasks()
        if stale_marked:
            logger.info(f"Marked {stale_marked} stale worker tasks as failed")

        # Step 1: Clean up database records
        db_deleted = await exec_context.db_context.worker_tasks.cleanup_old_tasks(
            retention_hours
        )

        # Step 2: Clean up old task directories from filesystem
        if workspace_path:
            tasks_dir = Path(workspace_path) / "tasks"
            if await aiofiles.os.path.exists(tasks_dir):
                cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)

                # List directories in tasks/
                for entry in await aiofiles.os.listdir(tasks_dir):
                    task_path = tasks_dir / entry
                    if await aiofiles.os.path.isdir(task_path):
                        # Check directory modification time
                        stat_info = await aiofiles.os.stat(task_path)
                        mtime = datetime.fromtimestamp(stat_info.st_mtime, tz=UTC)

                        if mtime < cutoff:
                            # Remove old task directory
                            try:
                                await asyncio.to_thread(shutil.rmtree, task_path)
                                dirs_deleted += 1
                                logger.debug(f"Removed old task directory: {task_path}")
                            except OSError as e:
                                logger.warning(
                                    f"Failed to remove task directory {task_path}: {e}"
                                )

        logger.info(
            f"Worker task cleanup completed. "
            f"Deleted {listeners_deleted} dead worker completion listeners, "
            f"marked {stale_marked} stale tasks, "
            f"deleted {db_deleted} database records, {dirs_deleted} task directories "
            f"older than {retention_hours} hours."
        )

    try:
        await clean_up_worker_tasks()
    except Exception as e:
        logger.exception(f"Error during worker task cleanup: {e}")
        raise
