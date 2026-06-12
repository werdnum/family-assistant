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
from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import (
    register_worker_wake_event,
    tasks_table,
    unregister_worker_wake_event,
)
from family_assistant.task_worker import TaskWorker
from family_assistant.tools import ToolExecutionContext
from tests.conftest import cleanup_task_worker
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

logger = logging.getLogger(__name__)

# ast-grep-ignore: no-dict-any - task payloads are heterogeneous; tests use empty payloads
TaskPayload = dict[str, Any]


async def _noop_handler(
    exec_context: ToolExecutionContext,  # noqa: ARG001
    payload: TaskPayload,  # noqa: ARG001
) -> None:
    """A handler that does nothing (workers stay idle, polling)."""


def _worker_engine_for(db_engine: AsyncEngine) -> AsyncEngine:
    """Return a dedicated engine for a worker, mirroring Assistant._worker_engine.

    A worker that parks inside a transaction must not hold a shared connection
    and block its siblings; each worker therefore gets its own engine to the same
    database. In-memory SQLite cannot be shared across engines, so the shared
    engine is reused in that case (the ``db_engine`` fixture uses an on-disk file,
    so dedicated engines are used in practice).
    """
    url = db_engine.url
    is_memory_sqlite = url.get_backend_name() == "sqlite" and (
        url.database is None or ":memory:" in url.database
    )
    if is_memory_sqlite:
        return db_engine
    return create_engine_with_sqlite_optimizations(
        url.render_as_string(hide_password=False)
    )


def _make_worker(
    db_engine: AsyncEngine,
    shutdown_event: asyncio.Event,
    *,
    dedicated_engine: bool = True,
    **kwargs: Any,  # noqa: ANN401 - passthrough to TaskWorker constructor
) -> TaskWorker:
    """Build a TaskWorker with mock externals and a real (system) clock.

    By default each worker gets its own engine (as the production pool does) so
    that concurrent workers do not contend on a single shared SQLite connection.
    """
    engine = _worker_engine_for(db_engine) if dedicated_engine else db_engine
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
    async with DatabaseContext(engine=db_engine) as db_context:
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
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
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
        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="slow-1", task_type="slow", payload={}, max_retries_override=0
            )
            await db_context.tasks.enqueue(
                task_id="slow-2", task_type="slow", payload={}, max_retries_override=0
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
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
    ) -> None:
        waiter_started.set()
        # Parks until the resolver task (run by a sibling worker) completes it.
        await future

    async def resolver_handler(
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
    ) -> None:
        if not future.done():
            future.set_result("resolved")

    workers = [_make_worker(db_engine, shutdown_event) for _ in range(2)]
    for worker in workers:
        worker.register_task_handler("waiter", waiter_handler)
        worker.register_task_handler("resolver", resolver_handler)
    pool = _Pool(workers, shutdown_event, db_engine)

    try:
        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="waiter",
                task_type="waiter",
                payload={},
                max_retries_override=0,
            )

        # Let one worker pick up and park on the waiter task.
        await asyncio.wait_for(waiter_started.wait(), timeout=5.0)

        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="resolver",
                task_type="resolver",
                payload={},
                max_retries_override=0,
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
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
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
        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="long-task",
                task_type="long",
                payload={},
                max_retries_override=0,
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
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
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
        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="hang-task",
                task_type="hang",
                payload={},
                max_retries_override=0,
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
        exec_context: ToolExecutionContext,  # noqa: ARG001
        payload: TaskPayload,  # noqa: ARG001
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

        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="quick-task",
                task_type="quick",
                payload={},
                max_retries_override=0,
            )

        # Much shorter than the 5s poll interval: must be the wake event firing.
        await asyncio.wait_for(processed.wait(), timeout=3.0)
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_enqueue_defers_worker_wake_until_commit(
    db_engine: AsyncEngine,
) -> None:
    """The worker wake fires on commit, not before the inserted row is visible.

    Waking before the enqueue transaction commits lets an idle sibling poll an
    empty queue, clear its event, and miss the not-yet-visible task row until
    the next 5s poll. The wake must be deferred to the commit hook.
    """
    wake_event = asyncio.Event()
    register_worker_wake_event(wake_event)
    try:
        async with DatabaseContext(engine=db_engine) as db_context:
            await db_context.tasks.enqueue(
                task_id="commit-visibility-task",
                task_type="quick",
                payload={},
                max_retries_override=0,
            )
            # Still inside the transaction: the row is not committed/visible yet,
            # so no worker may have been woken.
            assert not wake_event.is_set()
        # The transaction committed on context exit; the wake fires now.
        assert wake_event.is_set()
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
