"""Functional tests for ops_automation confinement (Phase 2).

Covers the execute_action wake_llm runtime guard and the cross-profile
update_automation denial against a real database.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.actions import (
    ActionType,
    WakeLlmProfileError,
    execute_action,
)
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.scripting.errors import ScriptError
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import TaskPriority, tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.tools import CompositeToolsProvider
from family_assistant.tools.automations import (
    create_automation_tool,
    update_automation_tool,
)
from family_assistant.tools.tasks import schedule_future_callback_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine


def _exec_context(
    db_ctx: Database,
    *,
    conversation_id: str,
    processing_profile_id: str | None,
    allow_wake_llm: bool = True,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id=conversation_id,
        user_name="tester",
        turn_id="turn",
        db_context=db_ctx,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
        processing_profile_id=processing_profile_id,
        user_id="user-1",
        allow_wake_llm=allow_wake_llm,
    )


# --- execute_action wake_llm runtime guard ---


@pytest.mark.asyncio
async def test_execute_action_refuses_confined_wake_llm(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    with pytest.raises(WakeLlmProfileError):
        await execute_action(
            db_ctx=db,
            action_type=ActionType.WAKE_LLM,
            action_config={"context": "diagnostics summary"},
            conversation_id="conv",
            processing_profile_id="ops_automation",
            allow_wake_llm=False,
        )


@pytest.mark.asyncio
async def test_execute_action_allows_permitted_wake_llm(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    # A profile that permits waking the LLM enqueues without raising.
    await execute_action(
        db_ctx=db,
        action_type=ActionType.WAKE_LLM,
        action_config={"context": "hello"},
        conversation_id="conv",
        processing_profile_id="default_assistant",
        allow_wake_llm=True,
    )
    rows = await db.fetch_all(
        select(tasks_table.c.payload).where(tasks_table.c.task_type == "llm_callback")
    )
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["tool_call_review_trigger_type"] == "scheduled_callback"
    assert payload["tool_call_review_trigger_definition"] == "hello"
    assert payload["tool_call_review_trigger_payload_present"] is False


# --- create_automation wake_llm denial for confined profiles ---


@pytest.mark.asyncio
async def test_create_wake_llm_automation_denied_for_confined_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    ctx = _exec_context(
        db,
        conversation_id="conv_confined",
        processing_profile_id="ops_automation",
        allow_wake_llm=False,
    )
    result = await create_automation_tool(
        exec_context=ctx,
        name="Sneaky Wake",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "wake up"},
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "not permitted to wake" in data["error"].lower()


@pytest.mark.asyncio
async def test_create_wake_llm_automation_allowed_for_normal_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    ctx = _exec_context(
        db,
        conversation_id="conv_normal",
        processing_profile_id="default_assistant",
        allow_wake_llm=True,
    )
    result = await create_automation_tool(
        exec_context=ctx,
        name="Normal Wake",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "wake up"},
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" not in data

    # The scheduled wake carries its originating profile so handle_llm_callback
    # runs the turn under it rather than the worker default.
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert rows
    raw_payload = rows[0]["payload"]
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    assert payload["processing_profile_id"] == "default_assistant"


# --- script built-in wake_llm() escape closed for confined profiles ---


@pytest.mark.asyncio
async def test_script_wake_llm_refused_for_confined_profile(
    db_engine: AsyncEngine,
) -> None:
    """A script stamped with a confined profile cannot escape via Monty's
    built-in wake_llm(), even when the worker's default profile may wake."""
    ops_service = _worker_service(service_id="ops_automation", allow_wake_llm=False)
    default_service = _worker_service(
        service_id="default_assistant",
        registry={"ops_automation": ops_service},
    )
    db = Database(engine=db_engine)
    worker_ctx = replace(
        _exec_context(
            db,
            conversation_id="conv_script",
            processing_profile_id="default_assistant",
            allow_wake_llm=True,
        ),
        processing_service=default_service,
    )

    with pytest.raises(ScriptError, match="not permitted to wake the LLM") as exc_info:
        await handle_script_execution(
            worker_ctx,
            {
                "script_code": "wake_llm({'message': 'escape'})\n",
                "conversation_id": "conv_script",
                "processing_profile_id": "ops_automation",
            },
        )
    assert isinstance(exc_info.value.__cause__, WakeLlmProfileError)

    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert rows == []


# --- cross-profile update_automation denial ---


@pytest.mark.asyncio
async def test_cross_profile_update_denied(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    owner_ctx = _exec_context(
        db,
        conversation_id="conv_owner",
        processing_profile_id="ops_automation",
    )
    created = await create_automation_tool(
        exec_context=owner_ctx,
        name="Owned Schedule",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="script",
        action_config={"script_code": "x = 1\n"},
    )
    created_data = created.get_data()
    assert isinstance(created_data, dict)
    automation_id = int(created_data["id"])

    # A different profile cannot update the automation.
    other_ctx = _exec_context(
        db,
        conversation_id="conv_owner",
        processing_profile_id="default_assistant",
    )
    result = await update_automation_tool(
        exec_context=other_ctx,
        automation_id=automation_id,
        automation_type="schedule",
        description="hijacked",
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "owned by profile" in data["error"].lower()

    stored = await db.schedule_automations.get_by_id(automation_id)
    assert stored is not None
    assert stored["description"] != "hijacked"
    assert stored["processing_profile_id"] == "ops_automation"


@pytest.mark.asyncio
async def test_same_profile_update_allowed(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    owner_ctx = _exec_context(
        db,
        conversation_id="conv_same",
        processing_profile_id="ops_automation",
    )
    created = await create_automation_tool(
        exec_context=owner_ctx,
        name="Owned Schedule Same",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="script",
        action_config={"script_code": "x = 1\n"},
    )
    created_data = created.get_data()
    assert isinstance(created_data, dict)
    automation_id = int(created_data["id"])

    result = await update_automation_tool(
        exec_context=owner_ctx,
        automation_id=automation_id,
        automation_type="schedule",
        description="updated by owner",
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" not in data

    stored = await db.schedule_automations.get_by_id(automation_id)
    assert stored is not None
    assert stored["description"] == "updated by owner"
    assert stored["processing_profile_id"] == "ops_automation"


# --- schedule_future_callback wake guard ---


@pytest.mark.asyncio
async def test_schedule_future_callback_refused_for_confined_profile(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    ctx = _exec_context(
        db,
        conversation_id="conv_future_cb",
        processing_profile_id="ops_automation",
        allow_wake_llm=False,
    )
    result = await schedule_future_callback_tool(
        exec_context=ctx,
        callback_time=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        context="wake later",
    )
    assert result is not None
    assert result.startswith("Error:")
    assert "not permitted to wake the LLM" in result

    # Nothing was enqueued.
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert rows == []


# --- update_automation wake guard for existing wake_llm automations ---


@pytest.mark.asyncio
async def test_wake_llm_update_refused_for_confined_profile(
    db_engine: AsyncEngine,
) -> None:
    """A confined profile may not keep or reschedule an existing wake_llm automation.

    The cross-profile ownership check does not fire for legacy (unstamped)
    automations, so the wake guard must refuse the update instead.
    """
    db = Database(engine=db_engine)
    legacy_ctx = _exec_context(
        db,
        conversation_id="conv_wake_update",
        processing_profile_id=None,
    )
    created = await create_automation_tool(
        exec_context=legacy_ctx,
        name="Legacy Wake",
        automation_type="schedule",
        trigger_config={"recurrence_rule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"},
        action_type="wake_llm",
        action_config={"context": "wake up"},
    )
    created_data = created.get_data()
    assert isinstance(created_data, dict)
    automation_id = int(created_data["id"])

    confined_ctx = _exec_context(
        db,
        conversation_id="conv_wake_update",
        processing_profile_id="ops_automation",
        allow_wake_llm=False,
    )
    result = await update_automation_tool(
        exec_context=confined_ctx,
        automation_id=automation_id,
        automation_type="schedule",
        action_config={"context": "hijacked wake"},
    )
    data = result.get_data()
    assert isinstance(data, dict)
    assert "error" in data
    assert "not permitted to wake the llm" in data["error"].lower()

    stored = await db.schedule_automations.get_by_id(automation_id)
    assert stored is not None
    assert stored["action_config"] == {"context": "wake up"}


# --- execution-time wake guard and profile-consistent context in the worker ---


def _worker_service(
    *,
    service_id: str,
    allow_wake_llm: bool = True,
    timezone: ZoneInfo | None = None,
    registry: dict[str, ProcessingService] | None = None,
) -> ProcessingService:
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[], default_response=LLMOutput(content="Acknowledged.")
        ),
        tools_provider=CompositeToolsProvider(providers=[]),
        service_config=ProcessingServiceConfig(
            id=service_id,
            prompts={"system_prompt": "Test profile"},
            timezone=timezone or ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            allow_wake_llm=allow_wake_llm,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
        processing_services_registry=registry,
    )


@pytest.mark.asyncio
async def test_queued_wake_refused_for_confined_profile(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """An already-enqueued llm_callback stamped with an allow_wake_llm=False
    profile is refused at execution time (creation-path guards cannot cover
    legacy queue entries or a config that changed after scheduling)."""
    ops_service = _worker_service(service_id="ops_automation", allow_wake_llm=False)
    default_service = _worker_service(
        service_id="default_assistant",
        registry={"ops_automation": ops_service},
    )

    worker, new_task_event, _shutdown_event = task_worker_manager(
        default_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("llm_callback", handle_llm_callback)

    task_id = f"queued_wake_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="llm_callback",
        payload={
            "conversation_id": "conv_queued_wake",
            "interface_type": "telegram",
            "callback_context": "diagnostics summary",
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
            "processing_profile_id": "ops_automation",
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )
    new_task_event.set()

    with pytest.raises(
        RuntimeError,
        match=(
            r"WakeLlmProfileError: Refusing queued llm_callback for profile "
            r"'ops_automation': the profile is not permitted to wake the LLM"
        ),
    ):
        await wait_for_tasks_to_complete(
            db_engine, task_types={"llm_callback"}, timeout_seconds=15
        )

    history_rows = await db_ctx.fetch_all(
        select(message_history_table.c.role).where(
            message_history_table.c.conversation_id == "conv_queued_wake"
        )
    )
    assert history_rows == []


@pytest.mark.asyncio
async def test_routed_wake_renders_trigger_in_routed_profile_timezone(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """A routed wake's tainted user-role trigger uses the routed timezone."""
    routed_service = _worker_service(
        service_id="complex_tasks",
        timezone=ZoneInfo("Australia/Sydney"),
    )
    default_service = _worker_service(
        service_id="default_assistant",
        registry={"complex_tasks": routed_service},
    )

    worker, new_task_event, _shutdown_event = task_worker_manager(
        default_service,
        AsyncMock(spec=ChatInterface),
    )
    worker.register_task_handler("llm_callback", handle_llm_callback)

    task_id = f"routed_wake_{uuid.uuid4().hex[:8]}"
    db_ctx = Database(engine=db_engine)
    await db_ctx.tasks.enqueue(
        task_id=task_id,
        task_type="llm_callback",
        payload={
            "conversation_id": "conv_routed_wake",
            "interface_type": "telegram",
            "callback_context": "scheduled follow-up",
            "scheduling_timestamp": datetime.now(UTC).isoformat(),
            "processing_profile_id": "complex_tasks",
        },
        max_retries_override=0,
        priority=TaskPriority.INTERACTIVE,
    )
    new_task_event.set()

    await wait_for_tasks_to_complete(
        db_engine, task_types={"llm_callback"}, timeout_seconds=15
    )

    db_ctx = Database(engine=db_engine)
    rows = await db_ctx.fetch_all(
        select(message_history_table.c.content).where(
            message_history_table.c.conversation_id == "conv_routed_wake",
            # Callback payloads are deliberately user-role input: an
            # application-generated wrapper must not grant unattended content
            # system-instruction priority.
            message_history_table.c.role == "user",
        )
    )
    trigger_texts = [row["content"] for row in rows]
    assert any("The time is now" in text for text in trigger_texts)
    # Australia/Sydney renders as AEST/AEDT rather than the worker default UTC.
    assert any("AE" in text for text in trigger_texts if "The time is now" in text)


# --- event-listener origin wake guard ---


_WAKE_PROFILE_FLAGS = {"ops_automation": False, "default_assistant": True}


async def _create_wake_listener(
    db: Database, *, name: str, origin_profile_id: str
) -> int:
    """Store a wake_llm webhook listener directly, as a pre-existing or
    admin-created listener would be (bypassing the creation-path guard)."""
    return await db.events.create_event_listener(
        name=name,
        source_id="webhook",
        match_conditions={"event": "data"},
        conversation_id="conv_event_wake",
        action_type="wake_llm",
        action_config={"context": "event fired"},
        processing_profile_id=origin_profile_id,
        created_by_user_id="user-1",
    )


async def _deliver_webhook_event(db_engine: AsyncEngine) -> None:
    processor = EventProcessor(
        sources={},
        get_db_context_func=lambda: Database(engine=db_engine),
        profile_wake_llm_flags=_WAKE_PROFILE_FLAGS,
    )
    await processor.start()
    try:
        await processor.process_event("webhook", {"event": "data"})
    finally:
        await processor.stop()


@pytest.mark.asyncio
async def test_event_listener_wake_refused_for_confined_origin(
    db_engine: AsyncEngine,
) -> None:
    """The event_handler routing must not launder a wake the origin profile may
    not perform: a listener stamped with an allow_wake_llm=False profile is
    skipped instead of enqueueing an llm_callback, while other listeners
    matching the same event still fire."""
    db = Database(engine=db_engine)
    await _create_wake_listener(
        db, name="Confined Wake", origin_profile_id="ops_automation"
    )
    permitted_id = await _create_wake_listener(
        db, name="Permitted Wake", origin_profile_id="default_assistant"
    )

    await _deliver_webhook_event(db_engine)

    callbacks = await db.tasks.get_all(task_type="llm_callback")
    assert len(callbacks) == 1
    payload = callbacks[0]["payload"]
    assert payload is not None
    assert payload["callback_context"]["listener_id"] == permitted_id


@pytest.mark.asyncio
async def test_event_listener_wake_allowed_origin_routes_to_event_handler(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    listener_id = await _create_wake_listener(
        db, name="Permitted Wake", origin_profile_id="default_assistant"
    )

    await _deliver_webhook_event(db_engine)

    callbacks = await db.tasks.get_all(task_type="llm_callback")
    assert len(callbacks) == 1
    payload = callbacks[0]["payload"]
    assert payload is not None
    assert payload["callback_context"]["listener_id"] == listener_id
    # Untrusted trigger: the woken turn runs under the restricted profile.
    assert payload["processing_profile_id"] == "event_handler"
