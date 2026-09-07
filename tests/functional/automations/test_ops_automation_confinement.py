"""Functional tests for ops_automation confinement (Phase 2).

Covers the execute_action wake_llm runtime guard and the cross-profile
update_automation denial against a real database.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
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
from family_assistant.storage.database import Database
from family_assistant.storage.message_history import message_history_table
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import (
    TaskWorker,
    _process_script_wake_llm,  # noqa: PLC2701  # testing the script wake_llm guard
    handle_llm_callback,
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

    from family_assistant.events.processor import EventListenerDict
    from family_assistant.scripting.monty_engine import WakeRequest


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
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
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
    """A confined script cannot escape via Monty's built-in wake_llm()."""
    db = Database(engine=db_engine)
    ctx = _exec_context(
        db,
        conversation_id="conv_script",
        processing_profile_id="ops_automation",
        allow_wake_llm=False,
    )
    wake_request: WakeRequest = {
        "context": {"message": "escape"},
        "include_event": False,
    }
    with pytest.raises(WakeLlmProfileError):
        await _process_script_wake_llm(
            exec_context=ctx,
            wake_contexts=[wake_request],
            event_data={},
            listener_id=None,
        )


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
    )
    new_task_event.set()

    with pytest.raises(RuntimeError, match="Task.*failed"):
        await wait_for_tasks_to_complete(
            db_engine, task_types={"llm_callback"}, timeout_seconds=15
        )


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


def _wake_listener(*, origin_profile_id: str | None) -> dict[str, object]:
    return {
        "id": 1,
        "name": "Confined Wake Listener",
        "source_id": "webhook",
        "conversation_id": "conv_event_wake",
        "interface_type": "telegram",
        "action_type": "wake_llm",
        "action_config": {"context": "event fired"},
        "processing_profile_id": origin_profile_id,
        "created_by_user_id": "user-1",
    }


@pytest.mark.asyncio
async def test_event_listener_wake_refused_for_confined_origin(
    db_engine: AsyncEngine,
) -> None:
    """The event_handler routing must not launder a wake the origin profile may
    not perform: a listener stamped with an allow_wake_llm=False profile is
    skipped instead of enqueueing an llm_callback."""
    processor = EventProcessor(
        sources={},
        get_db_context_func=lambda: Database(engine=db_engine),
        profile_wake_llm_flags={"ops_automation": False},
    )
    db = Database(engine=db_engine)
    await processor._execute_action_in_context(
        db,
        cast("EventListenerDict", _wake_listener(origin_profile_id="ops_automation")),
        {"event": "data"},
    )
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert rows == []


@pytest.mark.asyncio
async def test_event_listener_wake_allowed_origin_routes_to_event_handler(
    db_engine: AsyncEngine,
) -> None:
    processor = EventProcessor(
        sources={},
        get_db_context_func=lambda: Database(engine=db_engine),
        profile_wake_llm_flags={"default_assistant": True},
    )
    db = Database(engine=db_engine)
    await processor._execute_action_in_context(
        db,
        cast(
            "EventListenerDict",
            _wake_listener(origin_profile_id="default_assistant"),
        ),
        {"event": "data"},
    )
    rows = await db.fetch_all(
        select(tasks_table).where(tasks_table.c.task_type == "llm_callback")
    )
    assert len(rows) == 1
    raw_payload = rows[0]["payload"]
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    # Untrusted trigger: the woken turn runs under the restricted profile.
    assert payload["processing_profile_id"] == "event_handler"
