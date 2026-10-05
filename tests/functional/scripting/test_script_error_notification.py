"""
Tests for script error notification via LLM wake.

When a script_execution task exhausts all retries, the TaskWorker should
enqueue an llm_callback task to notify the user about the failure.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.storage.types import TaskDict
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


def _make_processing_service(
    tools_provider: CompositeToolsProvider,
    *,
    service_id: str = "event_handler",
    allow_wake_llm: bool = True,
    processing_services_registry: dict[str, ProcessingService] | None = None,
) -> ProcessingService:
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="N/A")
        ),
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id=service_id,
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            allow_wake_llm=allow_wake_llm,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        processing_services_registry=processing_services_registry,
    )


async def _make_tools_provider() -> CompositeToolsProvider:
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"],
        },
    )
    provider = CompositeToolsProvider(providers=[local_provider])
    await provider.get_tool_definitions()
    return provider


SENTINEL_TASK_TYPE = "test_queue_sentinel"


async def _noop_handler(exec_context: object, payload: object) -> None:
    return None


async def _script_error_notifications(engine: AsyncEngine) -> list[TaskDict]:
    rows = await Database(engine=engine).fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    return [
        cast("TaskDict", r)
        for r in rows
        if r["task_id"].startswith("script_error_notify_")
    ]


async def _wait_for_notification_tasks(engine: AsyncEngine) -> list[TaskDict]:
    return await wait_for_condition(
        lambda: _script_error_notifications(engine),
        timeout=15,
        description="a script_error_notify llm_callback task",
    )


async def _wait_for_worker_to_finish_failure_handling(
    engine: AsyncEngine, new_task_event: asyncio.Event
) -> None:
    """Run a no-op task through the worker and wait for it to finish.

    The worker handles one task at a time, so it only dequeues this after
    returning from the previous task's failure handling, which is where any
    error notification is enqueued. Requires ``SENTINEL_TASK_TYPE`` to be
    registered on the worker.
    """
    sentinel_id = f"sentinel_{uuid.uuid4().hex[:8]}"
    await Database(engine=engine).tasks.enqueue(
        task_id=sentinel_id,
        task_type=SENTINEL_TASK_TYPE,
        payload={},
        priority=TaskPriority.INTERACTIVE,
    )
    new_task_event.set()
    await wait_for_tasks_to_complete(engine, task_ids={sentinel_id}, timeout_seconds=15)


# Flaky under xdist on SQLite: the llm_callback task can stall in `processing`
# (task-worker / SQLite write contention) and time out. See issue #889.
@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_script_failure_notification_is_processable(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """After max retries, a failed script_execution task enqueues an llm_callback notification
    that can be successfully processed by handle_llm_callback end-to-end."""
    tools_provider = await _make_tools_provider()
    processing_service = _make_processing_service(tools_provider)
    mock_chat = AsyncMock(spec=ChatInterface)

    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        mock_chat,
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    worker.register_task_handler("llm_callback", handle_llm_callback)

    # Enqueue a script that will always fail (syntax error) with max_retries=0
    # so it fails immediately without retrying
    task_id = f"test_fail_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="script_execution",
        payload={
            "script_code": "this is not valid python!!!",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "task_name": "Broken Automation",
            "automation_id": "42",
            "created_by_user_id": "script-owner",
            "config": {},
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    # Wait for the script task to fail
    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    # Wait for the notification task to appear
    notification_tasks = await _wait_for_notification_tasks(db_engine)
    assert len(notification_tasks) == 1, (
        f"Expected 1 notification task, found {len(notification_tasks)}"
    )

    notif = notification_tasks[0]
    assert notif["max_retries"] == 1
    notif_payload = notif["payload"]
    assert notif_payload is not None
    assert notif_payload["conversation_id"] == "test_conv"
    assert notif_payload["interface_type"] == "telegram"
    assert "scheduling_timestamp" in notif_payload
    assert "Broken Automation" in notif_payload["callback_context"]
    assert "automation 42" in notif_payload["callback_context"]
    assert "not valid python" in notif_payload["callback_context"]
    assert "Do NOT re-run the script" in notif_payload["callback_context"]
    # The error-summary turn is deliberately ownerless so its (untrusted) error
    # content cannot drive durable approvals for confirm-gated tools, even though
    # the failed script itself had a recorded owner.
    assert "created_by_user_id" not in notif_payload

    # Now let the worker process the llm_callback notification task.
    # This is the key part: if the payload is malformed (e.g. missing
    # scheduling_timestamp), handle_llm_callback will raise and the task will fail.
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_ids={notif["task_id"]}, timeout_seconds=15
    )


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_notify_on_failure_false_suppresses_notification(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Setting notify_on_failure=False in config suppresses the notification."""
    tools_provider = await _make_tools_provider()
    processing_service = _make_processing_service(tools_provider)

    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    worker.register_task_handler(SENTINEL_TASK_TYPE, _noop_handler)

    task_id = f"test_nonotify_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="script_execution",
        payload={
            "script_code": "this is not valid python!!!",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "config": {"notify_on_failure": False},
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    await _wait_for_worker_to_finish_failure_handling(db_engine, new_task_event)
    notification_tasks = await _script_error_notifications(db_engine)
    assert notification_tasks == [], (
        "Expected no notification tasks with notify_on_failure=False"
    )


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_llm_callback_failure_does_not_trigger_notification(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """An llm_callback task failure should NOT trigger error notification (loop prevention)."""
    tools_provider = await _make_tools_provider()
    processing_service = _make_processing_service(tools_provider)

    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )

    # Register a handler that always raises for llm_callback
    async def failing_llm_handler(
        exec_context: object, payload: dict[str, object]
    ) -> None:
        raise RuntimeError("LLM callback handler failed")

    worker.register_task_handler("llm_callback", failing_llm_handler)
    worker.register_task_handler(SENTINEL_TASK_TYPE, _noop_handler)

    task_id = f"test_llm_fail_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="llm_callback",
        payload={
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "callback_context": "Some callback",
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    await _wait_for_worker_to_finish_failure_handling(db_engine, new_task_event)
    notification_tasks = await _script_error_notifications(db_engine)
    assert notification_tasks == [], (
        "llm_callback failures should NOT spawn error notifications"
    )


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_notification_contains_event_data(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Notification payload includes event data when available."""
    tools_provider = await _make_tools_provider()
    processing_service = _make_processing_service(tools_provider)

    worker, new_task_event, shutdown_event = task_worker_manager(
        processing_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)

    task_id = f"test_evdata_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="script_execution",
        payload={
            "script_code": "raise_error('intentional failure')",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "task_name": "Event Script",
            "listener_id": "listener_99",
            "event_data": {
                "entity_id": "sensor.temperature",
                "new_state": {"state": "25.5"},
            },
            "config": {},
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    notification_tasks = await _wait_for_notification_tasks(db_engine)
    assert len(notification_tasks) == 1

    payload = notification_tasks[0]["payload"]
    assert payload is not None
    context = payload["callback_context"]
    assert "event listener listener_99" in context
    assert "sensor.temperature" in context


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_confined_profile_failure_skips_llm_notification(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A failed script stamped with an allow_wake_llm=False profile must not wake
    any LLM: the error notification is an llm_callback and would otherwise run
    with the failed script/error/event data in its prompt."""
    tools_provider = await _make_tools_provider()
    ops_service = _make_processing_service(
        tools_provider,
        service_id="ops_automation",
        allow_wake_llm=False,
    )
    default_service = _make_processing_service(
        tools_provider,
        service_id="default_assistant",
        processing_services_registry={"ops_automation": ops_service},
    )

    worker, new_task_event, shutdown_event = task_worker_manager(
        default_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)
    worker.register_task_handler(SENTINEL_TASK_TYPE, _noop_handler)

    task_id = f"test_confined_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="script_execution",
        payload={
            "script_code": "this is not valid python!!!",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "processing_profile_id": "ops_automation",
            "config": {},
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    await _wait_for_worker_to_finish_failure_handling(db_engine, new_task_event)
    notification_tasks = await _script_error_notifications(db_engine)
    assert notification_tasks == [], (
        "Expected no LLM notification for an allow_wake_llm=False profile"
    )


@pytest.mark.flaky(reruns=3, reruns_delay=2)
@pytest.mark.asyncio
async def test_stamped_profile_carried_into_notification(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """When the failed script's profile may wake the LLM, the notification is
    stamped with that profile so the woken turn keeps its tool policy and
    visibility confinement (never the worker's default trusted profile)."""
    tools_provider = await _make_tools_provider()
    creator_service = _make_processing_service(
        tools_provider,
        service_id="complex_tasks",
    )
    default_service = _make_processing_service(
        tools_provider,
        service_id="default_assistant",
        processing_services_registry={"complex_tasks": creator_service},
    )

    worker, new_task_event, shutdown_event = task_worker_manager(
        default_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("script_execution", handle_script_execution)

    task_id = f"test_stamped_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="script_execution",
        payload={
            "script_code": "this is not valid python!!!",
            "conversation_id": "test_conv",
            "interface_type": "telegram",
            "processing_profile_id": "complex_tasks",
            "config": {},
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )

    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_ids={task_id}, timeout_seconds=15
        )

    notification_tasks = await _wait_for_notification_tasks(db_engine)
    assert len(notification_tasks) == 1
    notif_payload = notification_tasks[0]["payload"]
    assert notif_payload is not None
    assert notif_payload["processing_profile_id"] == "complex_tasks"
