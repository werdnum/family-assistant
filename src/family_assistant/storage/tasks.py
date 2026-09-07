"""Task table definition and the worker wake-up registry.

Reads and writes of the queue itself live in
:class:`~family_assistant.storage.repositories.tasks.TasksRepository`; what is
left here is the table the repository uses and the process-wide notification
events it fires on enqueue.
"""

import asyncio
import logging
from asyncio import Event
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
)

from family_assistant.storage.base import metadata

logger = logging.getLogger(__name__)

# Module state for task notifications
_task_event: Event | None = None

# Registry of per-worker wake-up events. When a pool of TaskWorker instances runs
# concurrently, each worker waits on its OWN event so that one worker clearing its
# event after a wake does not swallow the notification for its siblings. The
# enqueue-notification path fans out to every registered event (see notify_workers)
# so an enqueued task wakes all idle workers promptly. The legacy module-global
# event from get_task_event() is always notified too, so single-worker callers and
# tests that pass an explicit event keep working unchanged.
_worker_wake_events: set[Event] = set()


def get_task_event() -> Event:
    """Get the event that's set when new tasks are available.

    This event is automatically set when immediate tasks are enqueued.
    Task workers can wait on this event to be notified of new work.

    Returns:
        The global task notification event
    """
    global _task_event
    if _task_event is None:
        _task_event = asyncio.Event()
    return _task_event


def register_worker_wake_event(event: Event) -> None:
    """Register a per-worker wake-up event for enqueue notifications.

    Every registered event is ``set()`` when an immediate task is enqueued, so a
    pool of workers each waiting on its own event are all woken. Idempotent.
    """
    _worker_wake_events.add(event)


def unregister_worker_wake_event(event: Event) -> None:
    """Remove a previously registered per-worker wake-up event.

    Safe to call for an event that is not registered (no-op).
    """
    _worker_wake_events.discard(event)


def notify_workers() -> None:
    """Wake all task workers about newly available work.

    Sets the legacy module-global event plus every per-worker event registered via
    :func:`register_worker_wake_event`. Using a snapshot of the registry guards
    against concurrent mutation during iteration.
    """
    get_task_event().set()
    for event in list(_worker_wake_events):
        event.set()


def notify_other_workers(except_event: Event) -> None:
    """Wake every registered worker except the one owning ``except_event``.

    Used when a worker has just claimed a task: there may be more queued tasks
    that this worker's single-task dequeue did not pick up, and a sibling that
    lost a dequeue race is otherwise parked until the next poll. Waking siblings
    lets them re-poll immediately so concurrent work is not delayed.
    """
    get_task_event().set()
    for event in list(_worker_wake_events):
        if event is not except_event:
            event.set()


# Define the tasks table for the message queue
tasks_table = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String, nullable=False, unique=True, index=True),
    Column("task_type", String, nullable=False, index=True),
    Column("payload", JSON, nullable=True),
    Column("scheduled_at", DateTime(timezone=True), nullable=True, index=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    ),
    Column("status", String, default="pending", nullable=False, index=True),
    Column("locked_by", String, nullable=True),
    Column("locked_at", DateTime(timezone=True), nullable=True),
    Column("error", Text, nullable=True),
    Column("retry_count", Integer, default=0, nullable=False),
    Column("max_retries", Integer, default=3, nullable=False),
    Column("recurrence_rule", String, nullable=True),
    Column("original_task_id", String, nullable=True, index=True),
)
