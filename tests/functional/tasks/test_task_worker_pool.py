"""Tests for the multi-worker TaskWorker pool.

These tests exercise the behaviours that motivate running more than one
in-process worker:

- Two workers process independent queued tasks concurrently rather than
  serializing them.
- A worker parked on an in-process future is unblocked by a sibling worker that
  runs the task resolving that future (the generic form of the confirmation
  deadlock the pool fixes).
- Per-task-type handler timeout overrides are honoured.
- Per-worker wake events: enqueueing a task promptly wakes an idle sibling.
- Reserved workers keep interactive capacity free: they claim neither background
  tasks nor handlers that park on queued work, so they still serve the queue
  while every general worker is parked.

The DB-touching tests run on both SQLite and PostgreSQL via the ``db_engine``
fixture so the added concurrency is validated on the production backend too.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.assistant import Assistant
from family_assistant.config_models import AppConfig
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import (
    TaskPriority,
    register_worker_wake_event,
    tasks_table,
    unregister_worker_wake_event,
)
from family_assistant.task_worker import PARKING_TASK_TYPES, TaskWorker
from family_assistant.tools import ToolExecutionContext
from tests.conftest import cleanup_task_worker
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

logger = logging.getLogger(__name__)

# ast-grep-ignore: no-dict-any - task payloads are heterogeneous; tests use empty payloads
TaskPayload = dict[str, Any]


async def _noop_handler(
    exec_context: ToolExecutionContext,
    payload: TaskPayload,
) -> None:
    """A handler that does nothing (workers stay idle, polling)."""


def _make_worker(
    db_engine: AsyncEngine,
    shutdown_event: asyncio.Event,
    **kwargs: Any,  # noqa: ANN401 - passthrough to TaskWorker constructor
) -> TaskWorker:
    """Build a TaskWorker with mock externals and a real (system) clock.

    Every worker shares the application engine, as the production pool does --
    one database, one engine, one connection pool. That is what makes SQLite's
    per-engine transaction lock able to serialize the pool at all.
    """
    engine = db_engine
    return TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=engine,
        **kwargs,
    )


async def _task_status(db_engine: AsyncEngine, task_id: str) -> str | None:
    """Return the status of a task row, or None if it does not exist."""
    db_context = Database(engine=db_engine)
    stmt = select(tasks_table).where(tasks_table.c.task_id == task_id)
    rows = await db_context.fetch_all(stmt)
    return rows[0]["status"] if rows else None


async def _dispose_worker_engine(
    worker: TaskWorker, shared_engine: AsyncEngine
) -> None:
    """Dispose a worker's dedicated engine (never the fixture's shared engine)."""
    if worker.engine is not None and worker.engine is not shared_engine:
        await worker.engine.dispose()


class _Pool:
    """Start and stop a pool of identically-configured workers."""

    def __init__(
        self,
        workers: list[TaskWorker],
        shutdown_event: asyncio.Event,
        shared_engine: AsyncEngine,
    ) -> None:
        self.workers = workers
        self.shutdown_event = shutdown_event
        self.shared_engine = shared_engine
        # Each worker creates and registers its OWN wake event (run() with no arg).
        self.tasks = [asyncio.create_task(worker.run()) for worker in workers]

    async def stop(self) -> None:
        self.shutdown_event.set()
        for task in self.tasks:
            await cleanup_task_worker(task, self.shutdown_event)
        # Dispose dedicated per-worker engines (not the fixture's shared engine).
        for worker in self.workers:
            if worker.engine is not None and worker.engine is not self.shared_engine:
                await worker.engine.dispose()


@pytest.fixture
async def shutdown_event() -> AsyncGenerator[asyncio.Event]:
    event = asyncio.Event()
    yield event
    event.set()


@pytest.mark.asyncio
async def test_two_workers_process_tasks_concurrently(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """Two slow tasks run by two workers overlap rather than serializing."""
    release = asyncio.Event()
    started = 0
    started_both = asyncio.Event()

    async def slow_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        nonlocal started
        started += 1
        if started >= 2:
            started_both.set()
        await release.wait()

    workers = [_make_worker(db_engine, shutdown_event) for _ in range(2)]
    for worker in workers:
        worker.register_task_handler("slow", slow_handler)
    pool = _Pool(workers, shutdown_event, db_engine)

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="slow-1",
            task_type="slow",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )
        await db_context.tasks.enqueue(
            task_id="slow-2",
            task_type="slow",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        # Both handlers must be in-flight simultaneously: if the pool serialized
        # tasks the second handler could not start until the first released.
        await asyncio.wait_for(started_both.wait(), timeout=5.0)
        assert started == 2

        release.set()
        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"slow-1", "slow-2"},
            timeout_seconds=10.0,
        )
    finally:
        release.set()
        await pool.stop()


@pytest.mark.asyncio
async def test_parked_worker_unblocked_by_sibling(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A worker awaiting an in-process future is unblocked by a sibling worker.

    This is the generic form of the confirmation-gated delegation deadlock: the
    first task parks on a future that only a *second* queued task can resolve. A
    single sequential worker would deadlock; a pool does not.
    """
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    waiter_started = asyncio.Event()

    async def waiter_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        waiter_started.set()
        # Parks until the resolver task (run by a sibling worker) completes it.
        await future

    async def resolver_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        if not future.done():
            future.set_result("resolved")

    workers = [_make_worker(db_engine, shutdown_event) for _ in range(2)]
    for worker in workers:
        worker.register_task_handler("waiter", waiter_handler)
        worker.register_task_handler("resolver", resolver_handler)
    pool = _Pool(workers, shutdown_event, db_engine)

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="waiter",
            task_type="waiter",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        # Let one worker pick up and park on the waiter task.
        await asyncio.wait_for(waiter_started.wait(), timeout=10.0)

        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="resolver",
            task_type="resolver",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        # If only one worker existed, the resolver could never run and the future
        # would never resolve. With a pool, the sibling runs it and unblocks the
        # parked worker.
        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"waiter", "resolver"},
            timeout_seconds=10.0,
        )
        assert future.result() == "resolved"
    finally:
        if not future.done():
            future.set_result("cleanup")
        await pool.stop()


@pytest.mark.asyncio
async def test_per_task_type_timeout_override_applied(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A task type with a longer override is not cancelled at the default timeout."""
    default_timeout = 0.3
    long_task_duration = default_timeout + 0.4

    async def long_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - exercising timeout budget
        await asyncio.sleep(long_task_duration)

    worker = _make_worker(
        db_engine,
        shutdown_event,
        handler_timeout=default_timeout,
        handler_timeout_overrides={"long": long_task_duration + 5.0},
    )
    worker.register_task_handler("long", long_handler)
    worker_task = asyncio.create_task(worker.run())

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="long-task",
            task_type="long",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"long-task"},
            timeout_seconds=10.0,
        )
        # The override let it finish: status is done, not failed-by-timeout.
        assert await _task_status(db_engine, "long-task") == "done"
    finally:
        await cleanup_task_worker(worker_task, shutdown_event)
        await _dispose_worker_engine(worker, db_engine)


@pytest.mark.asyncio
async def test_default_timeout_still_applies_without_override(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A task type with no override is still cancelled at the default timeout."""
    default_timeout = 0.3

    async def hanging_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - exercising timeout budget
        await asyncio.sleep(default_timeout + 5.0)

    worker = _make_worker(
        db_engine,
        shutdown_event,
        handler_timeout=default_timeout,
        handler_timeout_overrides={"other": 60.0},
    )
    worker.register_task_handler("hang", hanging_handler)
    worker_task = asyncio.create_task(worker.run())

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="hang-task",
            task_type="hang",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"hang-task"},
            timeout_seconds=10.0,
            allow_failures=True,
        )
        assert await _task_status(db_engine, "hang-task") == "failed"
    finally:
        await cleanup_task_worker(worker_task, shutdown_event)
        await _dispose_worker_engine(worker, db_engine)


@pytest.mark.asyncio
async def test_enqueue_wakes_idle_sibling_promptly(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """Enqueueing a task wakes an idle worker via its own wake event, not just polling.

    The pool starts idle (no tasks). Enqueue then fans out to every registered
    per-worker wake event, so the task is picked up well before the 5s poll
    interval would fire.
    """
    processed = asyncio.Event()

    async def quick_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        processed.set()

    workers = [_make_worker(db_engine, shutdown_event) for _ in range(2)]
    for worker in workers:
        worker.register_task_handler("quick", quick_handler)
    pool = _Pool(workers, shutdown_event, db_engine)

    try:
        # Let both workers settle into their poll-wait on their own events.
        await wait_for_condition(
            lambda: all(w.last_activity is not None for w in workers),
            timeout=2.0,
            description="workers to start",
        )

        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="quick-task",
            task_type="quick",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        # Much shorter than the 5s poll interval: must be the wake event firing.
        await asyncio.wait_for(processed.wait(), timeout=3.0)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_enqueue_wakes_workers_only_once_the_row_is_visible(
    db_engine: AsyncEngine,
) -> None:
    """The worker wake never precedes the enqueued row becoming visible.

    Waking first would let an idle sibling poll an empty queue, clear its
    event, and miss the row until the next 5s poll. The wake is registered on
    the enqueue's own transaction, so by the time enqueue returns the row is
    committed and the wake has fired.
    """
    wake_event = asyncio.Event()
    register_worker_wake_event(wake_event)
    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="commit-visibility-task",
            task_type="quick",
            payload={},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        assert wake_event.is_set()
        # A woken worker reads on its own connection, so the row must be
        # visible there too.
        observer = Database(engine=db_engine)
        row = await observer.fetch_one(
            select(tasks_table).where(tasks_table.c.task_id == "commit-visibility-task")
        )
        assert row is not None
    finally:
        unregister_worker_wake_event(wake_event)


def test_task_worker_count_defaults_to_two() -> None:
    """The pool size defaults to 2 and is configurable."""
    assert AppConfig().task_worker_count == 2
    assert AppConfig(task_worker_count=4).task_worker_count == 4


def test_task_worker_count_must_be_at_least_one() -> None:
    """A worker count below 1 is rejected at config validation time."""
    with pytest.raises(ValidationError):
        AppConfig(task_worker_count=0)


@pytest.mark.asyncio
async def test_health_monitor_restarts_dead_worker_among_pool(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A worker whose run task has died is restarted in place; siblings untouched."""
    workers = [_make_worker(db_engine, shutdown_event) for _ in range(3)]
    for worker in workers:
        worker.register_task_handler("noop", _noop_handler)

    # Build a minimal Assistant carrying just the pool state the monitor reads.
    assistant = Assistant.__new__(Assistant)
    assistant.task_workers = workers
    assistant.task_worker_tasks = [
        asyncio.create_task(worker.run()) for worker in workers
    ]

    try:
        # Wait for all workers to be running.
        await wait_for_condition(
            lambda: all(w.last_activity is not None for w in workers),
            timeout=2.0,
            description="workers to start",
        )

        # Kill the middle worker's run task.
        dead_index = 1
        original_tasks = list(assistant.task_worker_tasks)
        original_tasks[dead_index].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await original_tasks[dead_index]
        assert assistant.task_worker_tasks[dead_index].done()

        await assistant._check_and_restart_workers()

        # The dead index now holds a fresh, running task; siblings unchanged.
        assert assistant.task_worker_tasks[dead_index] is not original_tasks[dead_index]
        assert not assistant.task_worker_tasks[dead_index].done()
        for index in (0, 2):
            assert assistant.task_worker_tasks[index] is original_tasks[index]
            assert not assistant.task_worker_tasks[index].done()
    finally:
        shutdown_event.set()
        for task in assistant.task_worker_tasks:
            await cleanup_task_worker(task, shutdown_event)
        for worker in workers:
            await _dispose_worker_engine(worker, db_engine)


def _make_reserved_worker(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> TaskWorker:
    """Build a worker reserved for the interactive lane."""
    return _make_worker(
        db_engine, shutdown_event, min_priority=TaskPriority.INTERACTIVE
    )


@pytest.mark.asyncio
async def test_reserved_worker_never_claims_a_background_task(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A background task is left alone even when it is the only one due."""
    processed: list[str] = []

    async def recording_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        processed.append(str(payload["name"]))

    worker = _make_reserved_worker(db_engine, shutdown_event)
    worker.register_task_handler("recorded", recording_handler)
    worker_task = asyncio.create_task(worker.run())

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="background-task",
            task_type="recorded",
            payload={"name": "background"},
            max_retries_override=0,
            priority=TaskPriority.BACKGROUND,
        )
        # The interactive task is the reserved worker's only permitted work, so
        # its completion is the point by which the background one would have run.
        await db_context.tasks.enqueue(
            task_id="interactive-task",
            task_type="recorded",
            payload={"name": "interactive"},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"interactive-task"},
            timeout_seconds=10.0,
        )
        assert processed == ["interactive"]
        assert await _task_status(db_engine, "background-task") == "pending"
    finally:
        await cleanup_task_worker(worker_task, shutdown_event)
        await _dispose_worker_engine(worker, db_engine)


@pytest.mark.asyncio
async def test_reserved_worker_never_claims_a_parking_task_type(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """An interactive task whose handler parks on queued work is left alone."""
    parking_type = next(iter(PARKING_TASK_TYPES))
    ran: list[str] = []

    async def recording_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        ran.append(str(payload["name"]))

    worker = _make_reserved_worker(db_engine, shutdown_event)
    worker.register_task_handler(parking_type, recording_handler)
    worker.register_task_handler("sentinel", recording_handler)
    assert parking_type not in worker.dequeued_task_types()
    worker_task = asyncio.create_task(worker.run())

    try:
        db_context = Database(engine=db_engine)
        await db_context.tasks.enqueue(
            task_id="parking-task",
            task_type=parking_type,
            payload={"name": "parking"},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )
        await db_context.tasks.enqueue(
            task_id="sentinel-task",
            task_type="sentinel",
            payload={"name": "sentinel"},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"sentinel-task"},
            timeout_seconds=10.0,
        )
        assert ran == ["sentinel"]
        assert await _task_status(db_engine, "parking-task") == "pending"
    finally:
        await cleanup_task_worker(worker_task, shutdown_event)
        await _dispose_worker_engine(worker, db_engine)


@pytest.mark.asyncio
async def test_reserved_worker_serves_the_queue_while_general_workers_park(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """With every general worker parked, the reserved worker keeps working.

    The parked handlers stand in for confirmation-gated delegated runs: each
    waits on an in-process future that only another queued task resolves. Under
    the default pool shape the reserved worker runs both the interactive task
    that came due meanwhile and the task that releases the parked runs.
    """
    config = AppConfig()
    parking_type = next(iter(PARKING_TASK_TYPES))
    release: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    parked_count = 0
    all_parked = asyncio.Event()
    ran_on: dict[str, str] = {}

    async def parking_handler(
        exec_context: ToolExecutionContext,
        payload: TaskPayload,
    ) -> None:
        nonlocal parked_count
        parked_count += 1
        if parked_count >= config.task_worker_count:
            all_parked.set()
        await release

    def make_recording_handler(
        worker_id: str,
    ) -> Any:  # noqa: ANN401 - handler signature is declared on registration
        async def handler(
            exec_context: ToolExecutionContext,
            payload: TaskPayload,
        ) -> None:
            ran_on[str(payload["name"])] = worker_id
            if payload["name"] == "release" and not release.done():
                release.set_result("released")

        return handler

    general_workers = [
        _make_worker(db_engine, shutdown_event) for _ in range(config.task_worker_count)
    ]
    reserved_workers = [
        _make_reserved_worker(db_engine, shutdown_event)
        for _ in range(config.reserved_task_worker_count)
    ]
    workers = general_workers + reserved_workers
    for worker in workers:
        worker.register_task_handler(parking_type, parking_handler)
        recording_handler = make_recording_handler(worker.worker_id)
        worker.register_task_handler("ping", recording_handler)
        worker.register_task_handler("release", recording_handler)
    pool = _Pool(workers, shutdown_event, db_engine)

    try:
        db_context = Database(engine=db_engine)
        for index in range(config.task_worker_count):
            await db_context.tasks.enqueue(
                task_id=f"parked-{index}",
                task_type=parking_type,
                payload={},
                max_retries_override=0,
                priority=TaskPriority.INTERACTIVE,
            )

        await asyncio.wait_for(all_parked.wait(), timeout=10.0)

        await db_context.tasks.enqueue(
            task_id="ping-task",
            task_type="ping",
            payload={"name": "ping"},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )
        await db_context.tasks.enqueue(
            task_id="release-task",
            task_type="release",
            payload={"name": "release"},
            max_retries_override=0,
            priority=TaskPriority.INTERACTIVE,
        )

        # Well inside the 5s poll interval: the reserved worker is woken by the
        # enqueue rather than finding the work on its next poll.
        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={"ping-task", "release-task"},
            timeout_seconds=4.0,
            poll_interval_seconds=0.1,
        )
        reserved_ids = {worker.worker_id for worker in reserved_workers}
        assert set(ran_on) == {"ping", "release"}
        assert set(ran_on.values()) <= reserved_ids

        # Releasing the future lets the parked runs finish on their own workers.
        await wait_for_tasks_to_complete(
            engine=db_engine,
            task_ids={f"parked-{index}" for index in range(config.task_worker_count)},
            timeout_seconds=10.0,
        )
    finally:
        if not release.done():
            release.set_result("cleanup")
        await pool.stop()


def test_reserved_task_worker_count_defaults_to_one() -> None:
    """One worker is held back for interactive work by default."""
    assert AppConfig().reserved_task_worker_count == 1
    assert AppConfig(reserved_task_worker_count=3).reserved_task_worker_count == 3


def test_reserved_task_worker_count_may_be_zero_but_not_negative() -> None:
    """Reserving nothing is a valid choice; a negative count is not."""
    assert AppConfig(reserved_task_worker_count=0).reserved_task_worker_count == 0
    with pytest.raises(ValidationError):
        AppConfig(reserved_task_worker_count=-1)


@pytest.mark.asyncio
async def test_health_monitor_restarts_a_reserved_worker_as_reserved(
    db_engine: AsyncEngine, shutdown_event: asyncio.Event
) -> None:
    """A restarted reserved worker keeps its lane: the instance is reused."""
    workers = [
        _make_worker(db_engine, shutdown_event),
        _make_worker(db_engine, shutdown_event),
        _make_reserved_worker(db_engine, shutdown_event),
    ]
    for worker in workers:
        worker.register_task_handler("noop", _noop_handler)

    assistant = Assistant.__new__(Assistant)
    assistant.task_workers = workers
    assistant.task_worker_tasks = [
        asyncio.create_task(worker.run()) for worker in workers
    ]

    try:
        await wait_for_condition(
            lambda: all(w.last_activity is not None for w in workers),
            timeout=2.0,
            description="workers to start",
        )

        reserved_index = 2
        original_tasks = list(assistant.task_worker_tasks)
        original_tasks[reserved_index].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await original_tasks[reserved_index]

        await assistant._check_and_restart_workers()

        restarted = assistant.task_workers[reserved_index]
        assert restarted is workers[reserved_index]
        assert restarted.min_priority == TaskPriority.INTERACTIVE
        assert (
            assistant.task_worker_tasks[reserved_index]
            is not original_tasks[reserved_index]
        )
        assert not assistant.task_worker_tasks[reserved_index].done()
    finally:
        shutdown_event.set()
        for task in assistant.task_worker_tasks:
            await cleanup_task_worker(task, shutdown_event)
        for worker in workers:
            await _dispose_worker_engine(worker, db_engine)
