"""
Tests for TaskWorker resilience features including timeout and health monitoring.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace  # pylint: disable=no-name-in-module
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.repositories.tasks import TasksRepository
from family_assistant.storage.tasks import tasks_table
from family_assistant.storage.types import ActionConfig, TaskDict
from family_assistant.task_worker import (
    SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY,
    SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
    TaskWorker,
    handle_llm_callback,
)
from family_assistant.tools import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

logger = logging.getLogger(__name__)


async def _get_tasks_for_automation(
    engine: AsyncEngine,
    automation_id: int,
) -> list[TaskDict]:
    db_context = Database(engine=engine)
    rows = await db_context.fetch_all(
        select(tasks_table).order_by(tasks_table.c.created_at)
    )

    tasks: list[TaskDict] = []
    for row in rows:
        task = cast("TaskDict", dict(row))
        payload = task.get("payload") or {}
        if payload.get("automation_id") == str(automation_id):
            tasks.append(task)
    return tasks


async def _get_task(engine: AsyncEngine, task_id: str) -> TaskDict | None:
    db_context = Database(engine=engine)
    row = await db_context.fetch_one(
        select(tasks_table).where(tasks_table.c.task_id == task_id)
    )
    if row is None:
        return None
    return cast("TaskDict", dict(row))


async def _make_schedule_task_due_now(
    engine: AsyncEngine,
    automation_id: int,
    scheduled_at: datetime,
    *,
    max_retries: int,
) -> TaskDict:
    tasks = await _get_tasks_for_automation(engine, automation_id)
    pending_tasks = [task for task in tasks if task["status"] == "pending"]
    assert len(pending_tasks) == 1
    task = pending_tasks[0]

    db_context = Database(engine=engine)
    await db_context.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id == task["task_id"])
        .values(scheduled_at=scheduled_at, max_retries=max_retries)
    )

    updated_task = await _get_task(engine, task["task_id"])
    assert updated_task is not None
    return updated_task


def _processing_service_with_callback_result(
    result: SimpleNamespace,
) -> MagicMock:
    processing_service = MagicMock()
    processing_service.handle_chat_interaction = AsyncMock(return_value=result)
    processing_service.home_assistant_client = None
    processing_service.attachment_registry = None
    processing_service.processing_services_registry = {}
    processing_service.service_config.id = "test_profile"
    processing_service.service_config.visibility_grants = None
    processing_service.service_config.default_note_visibility_labels = None
    return processing_service


async def _create_schedule_automation(
    engine: AsyncEngine,
    *,
    action_type: str,
    action_config: dict[str, str | bool],
    conversation_id: str,
) -> int:
    db_context = Database(engine=engine)
    return await db_context.schedule_automations.create(
        name=f"Resilience {action_type} {conversation_id}",
        recurrence_rule="FREQ=MINUTELY",
        action_type=action_type,
        action_config=cast("ActionConfig", action_config),
        conversation_id=conversation_id,
        interface_type="telegram",
        timezone=ZoneInfo("UTC"),
    )


async def _wait_for_one_next_schedule_task(
    engine: AsyncEngine,
    automation_id: int,
    original_task_id: str,
) -> TaskDict:
    async def next_schedule_task_exists() -> TaskDict | None:
        tasks = await _get_tasks_for_automation(engine, automation_id)
        original = next(task for task in tasks if task["task_id"] == original_task_id)
        pending_next = [
            task
            for task in tasks
            if task["status"] == "pending"
            and task["task_id"] != original_task_id
            and task["task_type"] != SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE
        ]
        if original["status"] in {"done", "failed"} and len(pending_next) == 1:
            return pending_next[0]
        return None

    next_task = await wait_for_condition(
        next_schedule_task_exists,
        timeout=10.0,
        description="next schedule automation task",
    )
    assert next_task is not None
    return next_task


@pytest.mark.asyncio
async def test_task_handler_timeout(
    db_engine: AsyncEngine,
) -> None:
    """Test that a handler timeout causes task failure."""
    test_timeout = 1.0  # Use 1 second timeout

    # Create events for worker coordination
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    # Create worker with custom timeout - no global patching needed
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        handler_timeout=test_timeout,  # Set timeout per instance
    )

    # Handler that will definitely timeout
    async def hanging_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        logger.info(
            f"Hanging handler started, will sleep for {test_timeout + 0.5} seconds"
        )
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timeout behavior
        await asyncio.sleep(test_timeout + 0.5)  # Longer than timeout
        logger.info("Hanging handler finished (should not reach here)")

    worker.register_task_handler("hang", hanging_handler)

    # Start worker task
    worker_task = asyncio.create_task(worker.run(new_task_event))
    logger.info("Started TaskWorker in background")

    # Give worker a moment to start up
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Ensuring worker initialization completes
    await asyncio.sleep(0.1)

    # Create a task with 0 retries allowed to avoid retry delays
    db_context = Database(engine=db_engine)
    await db_context.tasks.enqueue(
        task_id="timeout_test",
        task_type="hang",
        payload={},
        max_retries_override=0,  # No retries to avoid retry delays in test
    )
    logger.info("Created test task with ID: timeout_test")

    # Wake up worker to process task immediately
    new_task_event.set()
    logger.info("Signaled worker to process task")

    # Wait for task to be processed (it should timeout and fail immediately with no retries)
    await wait_for_tasks_to_complete(
        engine=db_engine,
        timeout_seconds=10.0,  # Give enough time for timeout + processing
        task_ids={"timeout_test"},
        allow_failures=True,
    )
    logger.info("Task processing completed")

    # Stop the worker
    shutdown_event.set()
    new_task_event.set()  # Wake worker so it sees shutdown

    try:
        await asyncio.wait_for(worker_task, timeout=5.0)
    except TimeoutError:
        logger.warning("Worker did not shut down cleanly, canceling")
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    # Check task was marked as failed due to timeout
    db_context = Database(engine=db_engine)
    stmt = select(tasks_table).where(tasks_table.c.task_id == "timeout_test")
    tasks = await db_context.fetch_all(stmt)
    task = tasks[0] if tasks else None

    assert task is not None, "Task not found in database"
    # Task should have failed immediately since max_retries=0
    assert task["status"] == "failed", (
        f"Expected status 'failed', got '{task['status']}'"
    )
    assert task["retry_count"] == 0, (
        f"Expected retry_count 0, got {task['retry_count']}"
    )  # No retries were allowed
    assert "TimeoutError" in (task["error"] or ""), (
        f"Expected 'TimeoutError' in error, got: {task['error']}"
    )
    logger.info(f"Task correctly failed with timeout: {task['error']}")


@pytest.mark.asyncio
async def test_successful_handler_completes(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that successful handlers mark tasks as completed."""
    # Create worker using the fixture factory
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    # Quick handler that completes
    async def quick_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        logger.info("Quick handler executed")

    worker.register_task_handler("quick", quick_handler)

    # Create a task
    db_context = Database(engine=engine)
    await db_context.tasks.enqueue(
        task_id="success_test",
        task_type="quick",
        payload={},
    )

    # Small delay to ensure task is committed (important for postgres)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
    await asyncio.sleep(0.1)

    # Wake up worker to process task (the fixture has already started the worker)
    new_task_event.set()

    # Wait for task to complete
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={"success_test"},
    )

    # Check task completed
    db_context = Database(engine=engine)
    stmt = select(tasks_table).where(tasks_table.c.task_id == "success_test")
    tasks = await db_context.fetch_all(stmt)
    task = tasks[0] if tasks else None

    assert task is not None, "Task not found in database"
    assert task["status"] == "done", f"Expected status 'done', got '{task['status']}'"


@pytest.mark.asyncio
async def test_task_worker_context_includes_taint_tracker(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Task, automation, and script handlers must not run taint-blind."""
    worker, new_task_event, _shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None
    received_contexts: list[ToolExecutionContext] = []

    async def handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        received_contexts.append(exec_context)

    worker.register_task_handler("captures_context", handler)

    db_context = Database(engine=engine)
    await db_context.tasks.enqueue(
        task_id="taint_context_test",
        task_type="captures_context",
        payload={},
    )

    # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
    await asyncio.sleep(0.1)
    new_task_event.set()

    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={"taint_context_test"},
    )

    assert len(received_contexts) == 1
    assert received_contexts[0].taint_tracker is not None


@pytest.mark.asyncio
async def test_retry_exhaustion_leads_to_failure(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that tasks fail permanently after exhausting retries."""
    # Create worker using the fixture factory with short timeout
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        handler_timeout=0.1,  # Very short timeout to make test fast
    )
    engine = worker.engine
    assert engine is not None

    # Handler that always times out
    async def timeout_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
        await asyncio.sleep(1.0)  # Longer than the 0.1s timeout

    worker.register_task_handler("timeout", timeout_handler)

    # Create task with NO retries allowed
    db_context = Database(engine=engine)
    await db_context.tasks.enqueue(
        task_id="no_retry_test",
        task_type="timeout",
        payload={},
        max_retries_override=0,  # No retries
    )

    # Small delay to ensure task is committed (important for postgres)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
    await asyncio.sleep(0.1)

    # Wake up worker to process task
    new_task_event.set()

    # Wait for task to fail (no retries)
    # Use a background task to periodically wake the worker to ensure it processes the failure
    async def wake_worker_periodically() -> None:
        for _ in range(
            40
        ):  # Wake every 0.5s for 20 seconds total (matches main timeout)
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
            await asyncio.sleep(0.5)
            new_task_event.set()

    wake_task = asyncio.create_task(wake_worker_periodically())

    try:
        await wait_for_tasks_to_complete(
            engine=engine,
            timeout_seconds=20.0,  # Increased from 10.0 to handle slower CI environments
            task_ids={"no_retry_test"},
            allow_failures=True,
        )
    finally:
        wake_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wake_task

    # Check task failed
    db_context = Database(engine=engine)
    stmt = select(tasks_table).where(tasks_table.c.task_id == "no_retry_test")
    tasks = await db_context.fetch_all(stmt)
    task = tasks[0] if tasks else None

    assert task is not None
    assert task["status"] == "failed"
    assert "TimeoutError" in (task["error"] or "")


@pytest.mark.asyncio
async def test_failed_schedule_script_reschedules_after_retry_exhaustion(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A schedule automation script keeps recurring after an exhausted failure."""
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    async def failing_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("scheduled script failed")

    worker.register_task_handler("script_execution", failing_handler)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="script",
        action_config={
            "script_code": "raise RuntimeError('scheduled script failed')",
            "notify_on_failure": False,
        },
        conversation_id="schedule-script-failure",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=0,
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={original_task["task_id"]},
        allow_failures=True,
    )

    next_task = await _wait_for_one_next_schedule_task(
        engine, automation_id, original_task["task_id"]
    )
    assert next_task["task_type"] == "script_execution"

    db_context = Database(engine=engine)
    automation = await db_context.schedule_automations.get_by_id(automation_id)
    assert automation is not None
    assert automation["execution_count"] == 1
    assert automation["last_execution_at"] is not None


@pytest.mark.asyncio
async def test_failed_schedule_llm_callback_delivers_error_once_and_reschedules(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A delivered callback error fails once and still advances the schedule."""
    processing_service = _processing_service_with_callback_result(
        SimpleNamespace(
            text_reply="User-visible callback error.",
            assistant_message_internal_id=None,
            reasoning_info=None,
            error_traceback="callback exploded",
            attachment_ids=[],
        )
    )
    chat_interface = AsyncMock()
    chat_interface.send_message.return_value = "mock-error-message-id"
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=chat_interface,
    )
    engine = worker.engine
    assert engine is not None
    worker.register_task_handler("llm_callback", handle_llm_callback)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="wake_llm",
        action_config={"context": "trigger a failing callback"},
        conversation_id="schedule-llm-failure",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=3,
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={original_task["task_id"]},
        allow_failures=True,
    )

    completed_task = await _get_task(engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["status"] == "failed"
    assert completed_task["retry_count"] == 0
    assert "callback exploded" in (completed_task["error"] or "")

    next_task = await _wait_for_one_next_schedule_task(
        engine, automation_id, original_task["task_id"]
    )
    assert next_task["task_type"] == "llm_callback"
    chat_interface.send_message.assert_awaited_once()
    assert chat_interface.send_message.await_args.kwargs["text"] == (
        "User-visible callback error."
    )


@pytest.mark.asyncio
async def test_retryable_schedule_failure_does_not_reschedule_next_run(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Schedule automations wait for retries before advancing the recurrence."""
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    async def failing_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("retry me")

    worker.register_task_handler("script_execution", failing_handler)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="script",
        action_config={"script_code": "raise RuntimeError('retry me')"},
        conversation_id="schedule-retryable-failure",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=1,
    )

    new_task_event.set()

    async def task_was_rescheduled_for_retry() -> TaskDict | None:
        task = await _get_task(engine, original_task["task_id"])
        if (
            task is not None
            and task["status"] == "pending"
            and task["retry_count"] == 1
            and task["error"]
        ):
            return task
        return None

    retried_task = await wait_for_condition(
        task_was_rescheduled_for_retry,
        timeout=10.0,
        description="schedule automation task retry",
    )
    assert retried_task is not None
    assert retried_task["task_id"] == original_task["task_id"]

    tasks = await _get_tasks_for_automation(engine, automation_id)
    assert [task for task in tasks if task["task_id"] != original_task["task_id"]] == []

    db_context = Database(engine=engine)
    automation = await db_context.schedule_automations.get_by_id(automation_id)
    assert automation is not None
    assert automation["execution_count"] == 0
    assert automation["last_execution_at"] is None


@pytest.mark.asyncio
async def test_schedule_advance_enqueued_when_retry_reschedule_fails(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If retry rescheduling fails into terminal failure, schedule advancement remains retryable."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),
    )

    automation_id = await _create_schedule_automation(
        db_engine,
        action_type="script",
        action_config={"script_code": "raise RuntimeError('retry me')"},
        conversation_id="schedule-retry-reschedule-failure",
    )
    original_task = await _make_schedule_task_due_now(
        db_engine,
        automation_id,
        worker.clock.now(),
        max_retries=1,
    )

    db_context = Database(engine=db_engine)

    async def fail_retry_reschedule(
        task_id: str,
        next_scheduled_at: datetime,
        new_retry_count: int,
        error: str,
    ) -> None:
        raise RuntimeError("retry reschedule failed")

    monkeypatch.setattr(
        db_context.tasks,
        "reschedule_for_retry",
        fail_retry_reschedule,
    )

    advance_request = await worker._handle_task_failure(
        db_context,
        original_task,
        RuntimeError("handler failed"),
        0.0,
    )
    assert advance_request is not None

    completed_task = await _get_task(db_engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["status"] == "failed"
    assert "Reschedule Failed" in (completed_task["error"] or "")
    assert completed_task["payload"] is not None
    assert "_schedule_automation_advance" in completed_task["payload"]

    db_context = Database(engine=db_engine)
    flushed = await worker._flush_schedule_automation_advance_outbox(
        db_context,
        advance_request.source_task_id,
    )
    assert flushed is True

    tasks = await _get_tasks_for_automation(db_engine, automation_id)
    advance_tasks = [
        task
        for task in tasks
        if task["task_type"] == SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE
    ]
    assert len(advance_tasks) == 1
    assert advance_tasks[0]["status"] == "pending"
    advance_payload = advance_tasks[0]["payload"]
    assert advance_payload is not None
    assert advance_payload["automation_id"] == str(automation_id)
    assert advance_payload["source_task_id"] == original_task["task_id"]
    assert datetime.fromisoformat(advance_payload["execution_time"]).tzinfo is not None


@pytest.mark.asyncio
async def test_schedule_advance_enqueue_failure_does_not_retry_completed_action(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed schedule action stays done if advancing its schedule cannot enqueue."""
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    handler_calls = 0

    async def successful_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        nonlocal handler_calls
        handler_calls += 1

    worker.register_task_handler("script_execution", successful_handler)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="script",
        action_config={"script_code": "print('done')"},
        conversation_id="schedule-success-advance-enqueue-failure",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=3,
    )

    original_enqueue = TasksRepository.enqueue
    advance_enqueue_attempted = asyncio.Event()

    async def fail_advance_enqueue(
        repository: TasksRepository,
        task_id: str,
        task_type: str,
        payload: Mapping[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        max_retries_override: int | None = None,
        recurrence_rule: str | None = None,
        original_task_id: str | None = None,
    ) -> None:
        if task_type == SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE:
            advance_enqueue_attempted.set()
            raise RuntimeError("advance enqueue failed")
        await original_enqueue(
            repository,
            task_id,
            task_type,
            payload,
            scheduled_at,
            max_retries_override,
            recurrence_rule,
            original_task_id,
        )

    monkeypatch.setattr(TasksRepository, "enqueue", fail_advance_enqueue)

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={original_task["task_id"]},
    )
    await wait_for_condition(
        advance_enqueue_attempted.is_set,
        timeout=10.0,
        description="schedule advance enqueue attempt",
    )

    completed_task = await _get_task(engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["status"] == "done"
    assert completed_task["retry_count"] == 0
    assert completed_task["payload"] is not None
    assert "_schedule_automation_advance" in completed_task["payload"]
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_schedule_advance_enqueue_failure_preserves_failed_source_status(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted schedule failure stays failed if advance enqueue fails."""
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    async def failing_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("scheduled action failed")

    worker.register_task_handler("script_execution", failing_handler)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="script",
        action_config={
            "script_code": "raise RuntimeError('scheduled action failed')",
            "notify_on_failure": False,
        },
        conversation_id="schedule-failed-advance-enqueue-failure",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=0,
    )

    original_enqueue = TasksRepository.enqueue
    advance_enqueue_attempted = asyncio.Event()

    async def fail_advance_enqueue(
        repository: TasksRepository,
        task_id: str,
        task_type: str,
        payload: Mapping[str, Any] | None = None,
        scheduled_at: datetime | None = None,
        max_retries_override: int | None = None,
        recurrence_rule: str | None = None,
        original_task_id: str | None = None,
    ) -> None:
        if task_type == SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE:
            advance_enqueue_attempted.set()
            raise RuntimeError("advance enqueue failed")
        await original_enqueue(
            repository,
            task_id,
            task_type,
            payload,
            scheduled_at,
            max_retries_override,
            recurrence_rule,
            original_task_id,
        )

    monkeypatch.setattr(TasksRepository, "enqueue", fail_advance_enqueue)

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={original_task["task_id"]},
        allow_failures=True,
    )
    await wait_for_condition(
        advance_enqueue_attempted.is_set,
        timeout=10.0,
        description="schedule advance enqueue attempt",
    )

    completed_task = await _get_task(engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["status"] == "failed"
    assert completed_task["retry_count"] == 0
    assert "scheduled action failed" in (completed_task["error"] or "")
    assert completed_task["payload"] is not None
    assert "_schedule_automation_advance" in completed_task["payload"]


@pytest.mark.asyncio
async def test_schedule_advance_outbox_drains_after_source_commit(
    db_engine: AsyncEngine,
) -> None:
    """A persisted schedule advancement outbox can recover after source commit."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),
    )

    automation_id = await _create_schedule_automation(
        db_engine,
        action_type="script",
        action_config={"script_code": "print('done')"},
        conversation_id="schedule-outbox-recovery",
    )
    original_task = await _make_schedule_task_due_now(
        db_engine,
        automation_id,
        worker.clock.now(),
        max_retries=0,
    )
    advance_request = worker._schedule_automation_advance_request_for_task(
        original_task
    )
    assert advance_request is not None

    payload = worker._payload_with_schedule_automation_advance_outbox(
        original_task,
        advance_request,
    )
    assert payload is not None
    outbox = payload[SCHEDULE_AUTOMATION_ADVANCE_OUTBOX_KEY]
    assert isinstance(outbox, dict)
    outbox["schedule_next"] = False

    db_context = Database(engine=db_engine)
    await db_context.tasks.update_status(
        task_id=original_task["task_id"],
        status="done",
        payload=payload,
    )
    await db_context.execute(
        update(tasks_table)
        .where(tasks_table.c.task_id == original_task["task_id"])
        .values(created_at=datetime(2026, 6, 23, 12, tzinfo=UTC))
    )

    db_context = Database(engine=db_engine)
    drained = await worker._drain_schedule_automation_advance_outbox(db_context)
    assert drained == 1

    completed_task = await _get_task(db_engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["payload"] is not None
    assert "_schedule_automation_advance" not in completed_task["payload"]

    tasks = await _get_tasks_for_automation(db_engine, automation_id)
    advance_tasks = [
        task
        for task in tasks
        if task["task_type"] == SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE
    ]
    assert len(advance_tasks) == 1
    assert advance_tasks[0]["status"] == "pending"
    advance_payload = advance_tasks[0]["payload"]
    assert advance_payload is not None
    assert advance_payload["schedule_next"] is False


@pytest.mark.asyncio
async def test_schedule_advance_outbox_drain_finds_buried_entries(
    db_engine: AsyncEngine,
) -> None:
    """Outbox recovery filters for pending advancement work before applying its batch limit."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),
    )

    automation_id = await _create_schedule_automation(
        db_engine,
        action_type="script",
        action_config={"script_code": "print('done')"},
        conversation_id="schedule-outbox-buried",
    )
    original_task = await _make_schedule_task_due_now(
        db_engine,
        automation_id,
        worker.clock.now(),
        max_retries=0,
    )
    advance_request = worker._schedule_automation_advance_request_for_task(
        original_task
    )
    assert advance_request is not None

    db_context = Database(engine=db_engine)
    for index in range(25):
        task_id = f"noise_terminal_task_{index}"
        await db_context.tasks.enqueue(
            task_id=task_id,
            task_type="script_execution",
            payload={"noise": index},
        )
        await db_context.tasks.update_status(
            task_id=task_id,
            status="done",
        )
    await db_context.tasks.update_status(
        task_id=original_task["task_id"],
        status="done",
        payload=worker._payload_with_schedule_automation_advance_outbox(
            original_task,
            advance_request,
        ),
    )

    db_context = Database(engine=db_engine)
    drained = await worker._drain_schedule_automation_advance_outbox(db_context)
    assert drained == 1

    completed_task = await _get_task(db_engine, original_task["task_id"])
    assert completed_task is not None
    assert completed_task["payload"] is not None
    assert "_schedule_automation_advance" not in completed_task["payload"]


@pytest.mark.asyncio
async def test_schedule_advance_uses_source_execution_time(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    mock_clock: MockClock,
) -> None:
    """Delayed schedule advancement records the source task terminal time."""
    source_execution_time = datetime(2026, 6, 22, 4, 28, tzinfo=UTC)
    mock_clock.set_time(source_execution_time + timedelta(hours=6))
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
    )
    engine = worker.engine
    assert engine is not None

    automation_id = await _create_schedule_automation(
        engine,
        action_type="script",
        action_config={"script_code": "print('scheduled')"},
        conversation_id="schedule-advance-source-time",
    )

    advance_task_id = "schedule_advance_source_time"
    db_context = Database(engine=engine)
    await db_context.tasks.enqueue(
        task_id=advance_task_id,
        task_type=SCHEDULE_AUTOMATION_ADVANCE_TASK_TYPE,
        payload={
            "automation_id": str(automation_id),
            "source_task_id": "source-schedule-task",
            "execution_time": source_execution_time.isoformat(),
        },
        max_retries_override=0,
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={advance_task_id},
    )

    completed_advance = await _get_task(engine, advance_task_id)
    assert completed_advance is not None
    assert completed_advance["status"] == "done"

    db_context = Database(engine=engine)
    automation = await db_context.schedule_automations.get_by_id(automation_id)
    assert automation is not None
    assert automation["execution_count"] == 1
    assert automation["last_execution_at"] == source_execution_time


@pytest.mark.asyncio
async def test_successful_schedule_llm_callback_reschedules_once(
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A successful schedule callback should enqueue exactly one next run."""
    processing_service = _processing_service_with_callback_result(
        SimpleNamespace(
            text_reply="Callback completed.",
            assistant_message_internal_id=None,
            reasoning_info=None,
            error_traceback=None,
            attachment_ids=[],
        )
    )
    chat_interface = AsyncMock()
    chat_interface.send_message.return_value = "mock-message-id"
    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service=processing_service,
        chat_interface=chat_interface,
    )
    engine = worker.engine
    assert engine is not None
    worker.register_task_handler("llm_callback", handle_llm_callback)

    automation_id = await _create_schedule_automation(
        engine,
        action_type="wake_llm",
        action_config={"context": "trigger a successful callback"},
        conversation_id="schedule-llm-success",
    )
    original_task = await _make_schedule_task_due_now(
        engine,
        automation_id,
        worker.clock.now(),
        max_retries=0,
    )

    new_task_event.set()
    await wait_for_tasks_to_complete(
        engine=engine,
        timeout_seconds=10.0,
        task_ids={original_task["task_id"]},
    )

    next_task = await _wait_for_one_next_schedule_task(
        engine, automation_id, original_task["task_id"]
    )
    assert next_task["task_type"] == "llm_callback"
    chat_interface.send_message.assert_awaited_once()

    db_context = Database(engine=engine)
    automation = await db_context.schedule_automations.get_by_id(automation_id)
    assert automation is not None
    assert automation["execution_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("genuine_response", [False, True])
async def test_follow_up_reminder_retry_distinguishes_trigger_from_user_response(
    db_engine: AsyncEngine,
    mock_clock: MockClock,
    genuine_response: bool,
) -> None:
    """A retry ignores only its trigger, while a real response still cancels."""
    callback_turn_id = "retrying-follow-up-reminder-turn"
    conversation_id = "retrying-follow-up-reminder-conversation"
    processing_service = _processing_service_with_callback_result(
        SimpleNamespace(
            text_reply="Reminder delivered after retry.",
            assistant_message_internal_id=None,
            reasoning_info=None,
            error_traceback=None,
            attachment_ids=[],
        )
    )
    processing_service.service_config.allow_wake_llm = True
    successful_result = processing_service.handle_chat_interaction.return_value
    processing_service.handle_chat_interaction.side_effect = [
        RuntimeError("fail after persisting trigger"),
        successful_result,
    ]
    chat_interface = AsyncMock()
    chat_interface.send_message.return_value = "delivered-reminder-id"
    db_context = Database(engine=db_engine)
    exec_context = ToolExecutionContext(
        interface_type="telegram",
        conversation_id=conversation_id,
        user_name="Reminder User",
        turn_id=callback_turn_id,
        db_context=db_context,
        processing_service=processing_service,
        clock=mock_clock,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
    )
    payload = {
        "interface_type": "telegram",
        "conversation_id": conversation_id,
        "user_name": "Reminder User",
        "callback_context": "Take the medication",
        "scheduling_timestamp": (mock_clock.now() - timedelta(minutes=1)).isoformat(),
        "reminder_config": {
            "is_reminder": True,
            "follow_up": True,
            "follow_up_interval": "30 minutes",
            "max_follow_ups": 2,
            "current_attempt": 1,
        },
    }

    with pytest.raises(RuntimeError, match="fail after persisting trigger"):
        await handle_llm_callback(exec_context, cast("Any", payload))

    trigger_rows_after_failure = await db_context.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.turn_id == callback_turn_id,
            message_history_table.c.role == "user",
            message_history_table.c.interface_message_id.is_(None),
        )
    )
    assert len(trigger_rows_after_failure) == 1

    if genuine_response:
        await db_context.message_history.add_message(
            UserMessage(content="I handled the reminder already."),
            interface_type="telegram",
            conversation_id=conversation_id,
            interface_message_id="genuine-user-response-id",
            turn_id=callback_turn_id,
            timestamp=mock_clock.now() + timedelta(seconds=1),
            user_id="reminder-user",
        )

    await handle_llm_callback(exec_context, cast("Any", payload))

    trigger_rows_after_retry = await db_context.fetch_all(
        select(message_history_table.c.internal_id).where(
            message_history_table.c.turn_id == callback_turn_id,
            message_history_table.c.role == "user",
            message_history_table.c.interface_message_id.is_(None),
        )
    )
    assert trigger_rows_after_retry == trigger_rows_after_failure
    follow_ups = await db_context.fetch_all(
        select(tasks_table).where(
            tasks_table.c.task_type == "llm_callback",
            tasks_table.c.status == "pending",
        )
    )
    if genuine_response:
        assert processing_service.handle_chat_interaction.await_count == 1
        chat_interface.send_message.assert_not_awaited()
        assert follow_ups == []
    else:
        assert processing_service.handle_chat_interaction.await_count == 2
        chat_interface.send_message.assert_awaited_once()
        assert chat_interface.send_message.await_args.kwargs["text"] == (
            "Reminder delivered after retry."
        )
        assert len(follow_ups) == 1
        assert follow_ups[0]["payload"]["reminder_config"]["current_attempt"] == 2


@pytest.mark.asyncio
async def test_worker_activity_tracking(db_engine: AsyncEngine) -> None:
    """Test that worker tracks last activity time."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Initial activity should be set
    initial_activity = worker.last_activity
    assert initial_activity is not None

    # Create and run a simple task
    async def simple_handler(
        # ast-grep-ignore: no-dict-any - task handler context has dynamic external dependency fields
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has dynamic mixed-type fields
        payload: dict[str, Any],
    ) -> None:
        pass

    worker.register_task_handler("simple", simple_handler)

    db_context = Database(engine=db_engine)
    await db_context.tasks.enqueue(
        task_id="activity_test",
        task_type="simple",
        payload={},
    )

    # Small delay to ensure task is committed (important for postgres)
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
    await asyncio.sleep(0.1)

    # Create wake up event and run worker to process task
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))
    wake_up_event.set()  # Wake up worker to process task

    # Wait for task to complete
    await wait_for_tasks_to_complete(
        engine=db_engine,
        timeout_seconds=10.0,
        task_ids={"activity_test"},
    )

    # Stop worker
    worker.shutdown_event.set()
    wake_up_event.set()  # Wake up worker if it's waiting
    worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await worker_task

    # Activity should have been updated
    assert worker.last_activity is not None, "last_activity is None"
    # Allow for small timing differences
    if worker.last_activity < initial_activity:
        diff = (initial_activity - worker.last_activity).total_seconds()
        assert diff < 1.0, (
            f"last_activity {worker.last_activity} is {diff}s before initial {initial_activity}"
        )


@pytest.mark.asyncio
async def test_health_check_properties(db_engine: AsyncEngine) -> None:
    """Test properties that health monitoring would check."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Create wake up event and start worker without any tasks
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))

    try:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
        await asyncio.sleep(0.5)

        # Last activity should be recent
        assert worker.last_activity is not None
        time_since_activity = (datetime.now(UTC) - worker.last_activity).total_seconds()
        assert time_since_activity < 10  # Should have been updated recently

    finally:
        worker.shutdown_event.set()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


@pytest.mark.asyncio
async def test_shutdown_stops_worker(db_engine: AsyncEngine) -> None:
    """Test that shutdown event stops the worker cleanly."""
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        shutdown_event_instance=asyncio.Event(),  # Create fresh shutdown event for test
    )

    # Create wake up event and start worker
    wake_up_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(wake_up_event))
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Testing task worker timing behavior
    await asyncio.sleep(0.1)

    # Set shutdown event
    worker.shutdown_event.set()

    # Worker should stop within reasonable time
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except TimeoutError:
        pytest.fail("Worker did not stop after shutdown event")

    # Task should be done
    assert worker_task.done()
