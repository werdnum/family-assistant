"""Functional tests for queue-backed durable confirmation execution."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant.llm.messages import UserMessage
from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationService,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker, handle_confirmation_tool_execution
from family_assistant.tools.infrastructure import PolicyEnforcingToolsProvider
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from tests.helpers import wait_for_tasks_to_complete

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService
    from family_assistant.tools import ToolExecutionContext
    from family_assistant.tools.types import ToolDefinition

TEST_TOOL_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "record_tool",
        "description": "Records that the tool executed.",
        "parameters": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "description": "Value to record."}
            },
            "required": ["value"],
        },
    },
}


class RecordingToolsProvider:
    """Fake tool provider that records executions."""

    def __init__(self) -> None:
        # ast-grep-ignore: no-dict-any - fake tool calls preserve arbitrary tool arguments
        self.calls: list[tuple[str, dict[str, Any], str | None, str | None, str]] = []

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [TEST_TOOL_DEFINITION]

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool provider protocol accepts arbitrary JSON arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        self.calls.append((
            name,
            dict(arguments),
            call_id,
            context.user_id,
            context.interface_type,
        ))
        return f"executed:{arguments['value']}"

    async def close(self) -> None:
        return None


class RecordingDescriptorToolsProvider(RecordingToolsProvider):
    """Recording provider with policy descriptors."""

    def __init__(self, tags: set[ToolTag]) -> None:
        super().__init__()
        self._descriptor = ToolDescriptor(
            name="record_tool",
            definition=TEST_TOOL_DEFINITION,
            tags=frozenset(tags),
            origin="local",
        )

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return [self._descriptor]

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        if name == self._descriptor.name:
            return self._descriptor
        return None


class RecordingChatInterface:
    """Fake chat interface that records outbound messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        _ = parse_mode
        _ = attachment_ids
        self.messages.append((conversation_id, text, reply_to_interface_id))
        return f"chat-message-{len(self.messages)}"


def _processing_service(provider: object) -> ProcessingService:
    service = SimpleNamespace(
        tools_provider=provider,
        service_config=SimpleNamespace(
            id="test-profile",
            visibility_grants=None,
            default_note_visibility_labels=None,
        ),
        attachment_registry=None,
        home_assistant_client=None,
    )
    return cast("ProcessingService", service)


def _confirmation_service(db_engine: AsyncEngine) -> ConfirmationService:
    return ConfirmationService(
        db_context_factory=lambda: DatabaseContext(engine=db_engine)
    )


async def _create_source_message(db_engine: AsyncEngine) -> int:
    async with DatabaseContext(engine=db_engine) as db:
        internal_id = await db.message_history.add_message(
            UserMessage(content="Please run the confirmed tool."),
            interface_type="web",
            conversation_id="web-conversation-1",
            interface_message_id="web-message-1",
            timestamp=datetime_now_utc(),
            processing_profile_id="test-profile",
            user_id="user-1",
        )
    assert internal_id is not None
    return internal_id


def datetime_now_utc() -> datetime:
    return datetime.now(UTC)


async def _create_request(
    db_engine: AsyncEngine,
    *,
    source_message_internal_id: int | None,
    tool_args: dict[str, object] | None = None,
) -> str:
    request = await _confirmation_service(db_engine).create_request(
        target_user_id="user-1",
        tool_name="record_tool",
        tool_args=tool_args or {"value": "payload"},
        tool_call_id="call-record-tool",
        source_message_internal_id=source_message_internal_id,
        confirmation_prompt="Run record_tool with value payload",
        expires_at=datetime_now_utc() + timedelta(hours=1),
    )
    return request["id"]


async def _approve_request(db_engine: AsyncEngine, request_id: str) -> str:
    approved = await _confirmation_service(db_engine).approve_and_enqueue_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )
    execution_task_id = approved["execution_task_id"]
    assert execution_task_id is not None
    return execution_task_id


async def _run_worker_until_task_finishes(
    db_engine: AsyncEngine,
    *,
    processing_service: ProcessingService,
    chat_interface: RecordingChatInterface,
    task_id: str,
    allow_failures: bool = False,
) -> None:
    shutdown_event = asyncio.Event()
    wake_event = asyncio.Event()
    worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=cast("ChatInterface", chat_interface),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        chat_interfaces={"web": cast("ChatInterface", chat_interface)},
        handler_timeout=5.0,
    )
    worker.register_task_handler(
        CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
        handle_confirmation_tool_execution,
    )
    worker_task = asyncio.create_task(worker.run(wake_event))
    try:
        wake_event.set()
        await wait_for_tasks_to_complete(
            engine=db_engine,
            timeout_seconds=10.0,
            task_ids={task_id},
            allow_failures=allow_failures,
        )
    finally:
        shutdown_event.set()
        wake_event.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(worker_task, timeout=2.0)
        if not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task


async def _task_status(db_engine: AsyncEngine, task_id: str) -> tuple[str, str | None]:
    async with DatabaseContext(engine=db_engine) as db:
        row = await db.fetch_one(
            select(tasks_table.c.status, tasks_table.c.error).where(
                tasks_table.c.task_id == task_id
            )
        )
    assert row is not None
    return str(row["status"]), cast("str | None", row["error"])


@pytest.mark.asyncio
async def test_approved_confirmation_task_executes_stored_tool(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert provider.calls == [
        (
            "record_tool",
            {"value": "payload"},
            "call-record-tool",
            "user-1",
            "web",
        )
    ]
    assert chat_interface.messages == [
        (
            "web-conversation-1",
            "Approved action completed.\n\n"
            "Tool: record_tool\n\n"
            "Result:\nexecuted:payload",
            "web-message-1",
        )
    ]
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_confirmation_task_skips_non_approved_request(
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_request(db_engine, source_message_internal_id=None)
    task_id = f"manual-confirmation-execution:{request_id}"
    provider = RecordingToolsProvider()
    chat_interface = RecordingChatInterface()

    async with DatabaseContext(engine=db_engine) as db:
        await db.tasks.enqueue(
            task_id=task_id,
            task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
            payload={"confirmation_request_id": request_id},
            max_retries_override=0,
        )

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert provider.calls == []
    assert chat_interface.messages == []
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_confirmation_task_fails_closed_when_current_policy_denies_tool(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    wrapped_provider = RecordingDescriptorToolsProvider({ToolTag.STATE_CHANGING})
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=wrapped_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[],
            )
        ),
    )
    chat_interface = RecordingChatInterface()

    async with DatabaseContext(engine=db_engine) as db:
        await db.execute_with_retry(
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            .values(max_retries=0)
        )

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(policy_provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
    )

    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "denied by policy" in error
    assert wrapped_provider.calls == []
    assert chat_interface.messages == []


@pytest.mark.asyncio
async def test_approved_confirmation_satisfies_current_confirm_policy_once(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
        tool_args={"value": "requires-confirm"},
    )
    task_id = await _approve_request(db_engine, request_id)
    wrapped_provider = RecordingDescriptorToolsProvider({ToolTag.STATE_CHANGING})
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=wrapped_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[
                    PolicyRule(
                        match=ToolMatcher(tags_any=[ToolTag.STATE_CHANGING]),
                        decision=ToolPolicyDecision.CONFIRM,
                    )
                ],
            )
        ),
        confirmation_timeout=5.0,
    )
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(policy_provider),
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert wrapped_provider.calls == [
        (
            "record_tool",
            {"value": "requires-confirm"},
            "call-record-tool",
            "user-1",
            "web",
        )
    ]
    assert len(chat_interface.messages) == 1
    assert "executed:requires-confirm" in chat_interface.messages[0][1]
    assert await _task_status(db_engine, task_id) == ("done", None)
