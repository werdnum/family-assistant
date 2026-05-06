"""Functional tests for queue-backed durable confirmation execution."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant import task_worker as task_worker_module
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.llm.messages import UserMessage
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationService,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
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
from family_assistant.tools.types import ToolAttachment, ToolResult
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing import ProcessingService
    from family_assistant.tools import ToolExecutionContext
    from family_assistant.tools.types import ToolArguments, ToolDefinition

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


class FailingToolsProvider(RecordingToolsProvider):
    """Fake tool provider that raises during execution."""

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
        raise RuntimeError("tool exploded")


class AttachmentToolsProvider(RecordingToolsProvider):
    """Fake tool provider that returns a result attachment."""

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool provider protocol accepts arbitrary JSON arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolResult:
        self.calls.append((
            name,
            dict(arguments),
            call_id,
            context.user_id,
            context.interface_type,
        ))
        return ToolResult(
            text="created attachment",
            attachments=[
                ToolAttachment(
                    mime_type="text/plain",
                    content=b"confirmation attachment",
                    description="Confirmation output",
                )
            ],
        )


class BlockingToolsProvider(RecordingToolsProvider):
    """Fake tool provider that waits until cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._release = asyncio.Event()

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
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return "unexpected-release"


class RecordingChatInterface:
    """Fake chat interface that records outbound messages."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str | None]] = []
        self.attachment_ids: list[list[str] | None] = []

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - fake chat I/O must yield to exercise cancellation cleanup
        await asyncio.sleep(0)
        _ = parse_mode
        self.messages.append((conversation_id, text, reply_to_interface_id))
        self.attachment_ids.append(attachment_ids)
        return f"chat-message-{len(self.messages)}"


class FailingChatInterface(RecordingChatInterface):
    """Fake chat interface that raises on outbound send."""

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        await super().send_message(
            conversation_id=conversation_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
        )
        raise RuntimeError("chat send failed")


class UndeliveredChatInterface(RecordingChatInterface):
    """Fake chat interface that reports send failure with no exception."""

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        await super().send_message(
            conversation_id=conversation_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
        )
        return None


class BlockingChatInterface(RecordingChatInterface):
    """Fake chat interface that waits until cancelled."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._release = asyncio.Event()

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        _ = conversation_id
        _ = text
        _ = parse_mode
        _ = reply_to_interface_id
        _ = attachment_ids
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return "unexpected-release"


def _processing_service(
    provider: object,
    *,
    attachment_registry: object | None = None,
) -> ProcessingService:
    service_config = SimpleNamespace(
        id="test-profile",
        timezone=ZoneInfo("UTC"),
        visibility_grants=None,
        default_note_visibility_labels=None,
        note_registry=None,
    )
    service = SimpleNamespace(
        kind="local",
        tools_provider=provider,
        service_config=service_config,
        attachment_registry=attachment_registry,
        home_assistant_client=None,
        camera_backend=None,
        processing_services_registry=None,
    )
    return cast("ProcessingService", service)


def _confirmation_service(db_engine: AsyncEngine) -> ConfirmationService:
    return ConfirmationService(
        db_context_factory=lambda: DatabaseContext(engine=db_engine)
    )


async def _create_source_message(
    db_engine: AsyncEngine,
    *,
    processing_profile_id: str = "test-profile",
) -> int:
    async with DatabaseContext(engine=db_engine) as db:
        internal_id = await db.message_history.add_message(
            UserMessage(content="Please run the confirmed tool."),
            interface_type="web",
            conversation_id="web-conversation-1",
            interface_message_id="web-message-1",
            timestamp=datetime_now_utc(),
            processing_profile_id=processing_profile_id,
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
    tool_args: ToolArguments | None = None,
) -> str:
    resolved_tool_args: ToolArguments = (
        tool_args if tool_args is not None else {"value": "payload"}
    )
    request = await _confirmation_service(db_engine).create_request(
        target_user_id="user-1",
        tool_name="record_tool",
        tool_args=resolved_tool_args,
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
    confirmation_result_waiters: ConfirmationResultWaiterRegistry | None = None,
    handler_timeout: float = 5.0,
) -> None:
    shutdown_event = asyncio.Event()
    wake_event = asyncio.Event()
    worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=cast("ChatInterface", chat_interface),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MockEmbeddingGenerator(dimensions=10),
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
        chat_interfaces={"web": cast("ChatInterface", chat_interface)},
        handler_timeout=handler_timeout,
        confirmation_result_waiters=confirmation_result_waiters,
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
            await wait_for_condition(
                lambda: worker_task.done(),
                timeout=2.0,
                description="confirmation worker task to stop",
            )
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


def _processing_service_with_registry(
    *,
    provider: object,
    service_id: str,
    registry: dict[str, object] | None = None,
) -> ProcessingService:
    service = cast("SimpleNamespace", _processing_service(provider))
    service.service_config.id = service_id
    service.processing_services_registry = registry
    return cast("ProcessingService", service)


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
async def test_approved_confirmation_delivers_live_waiter_without_notification(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    confirmation_result_waiters = ConfirmationResultWaiterRegistry()
    waiter = confirmation_result_waiters.register(request_id)
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        confirmation_result_waiters=confirmation_result_waiters,
    )

    await wait_for_condition(
        lambda: waiter.done(),
        timeout=1.0,
        description="live confirmation waiter to resolve",
    )
    outcome = waiter.result()
    assert outcome.kind == "completed"
    assert outcome.result == "executed:payload"
    assert provider.calls == [
        (
            "record_tool",
            {"value": "payload"},
            "call-record-tool",
            "user-1",
            "web",
        )
    ]
    assert chat_interface.messages == []
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_confirmation_execution_failure_resolves_live_waiter(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    confirmation_result_waiters = ConfirmationResultWaiterRegistry()
    waiter = confirmation_result_waiters.register(request_id)
    task_id = await _approve_request(db_engine, request_id)
    provider = FailingToolsProvider()
    chat_interface = RecordingChatInterface()

    async with DatabaseContext(engine=db_engine) as db:
        await db.execute_with_retry(
            update(tasks_table)
            .where(tasks_table.c.task_id == task_id)
            .values(max_retries=0)
        )

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
        confirmation_result_waiters=confirmation_result_waiters,
    )

    await wait_for_condition(
        lambda: waiter.done(),
        timeout=1.0,
        description="live confirmation waiter to resolve after failure",
    )
    outcome = waiter.result()
    assert outcome.kind == "failed"
    assert (
        outcome.result == "Error executing approved tool 'record_tool': tool exploded"
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
    assert chat_interface.messages == []
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "tool exploded" in error


@pytest.mark.asyncio
async def test_confirmation_execution_failure_notifies_without_live_waiter(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = FailingToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
    )

    assert chat_interface.messages == [
        (
            "web-conversation-1",
            "Approved action failed.\n\n"
            "Tool: record_tool\n\n"
            "Error:\nError executing approved tool 'record_tool': tool exploded",
            "web-message-1",
        )
    ]
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "tool exploded" in error


@pytest.mark.asyncio
async def test_confirmation_execution_timeout_resolves_live_waiter(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    confirmation_result_waiters = ConfirmationResultWaiterRegistry()
    waiter = confirmation_result_waiters.register(request_id)
    task_id = await _approve_request(db_engine, request_id)
    provider = BlockingToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
        confirmation_result_waiters=confirmation_result_waiters,
        handler_timeout=1.0,
    )

    assert provider.started.is_set()
    assert provider.cancelled.is_set()
    await wait_for_condition(
        lambda: waiter.done(),
        timeout=1.0,
        description="live confirmation waiter to resolve after timeout",
    )
    outcome = waiter.result()
    assert outcome.kind == "failed"
    assert (
        outcome.result
        == "Error executing approved tool 'record_tool': execution was cancelled"
    )
    assert chat_interface.messages == []
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "TimeoutError" in error


@pytest.mark.asyncio
async def test_confirmation_execution_timeout_notifies_without_live_waiter(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = BlockingToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
        handler_timeout=1.0,
    )

    assert provider.started.is_set()
    assert provider.cancelled.is_set()
    assert chat_interface.messages == [
        (
            "web-conversation-1",
            "Approved action failed.\n\n"
            "Tool: record_tool\n\n"
            "Error:\nError executing approved tool 'record_tool': "
            "execution was cancelled",
            "web-message-1",
        )
    ]
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "TimeoutError" in error


@pytest.mark.asyncio
async def test_confirmation_execution_timeout_bounds_notification_cleanup(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_worker_module,
        "CONFIRMATION_CANCELLATION_CLEANUP_TIMEOUT",
        0.05,
    )
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = BlockingToolsProvider()
    chat_interface = BlockingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
        handler_timeout=1.0,
    )

    assert provider.started.is_set()
    assert provider.cancelled.is_set()
    assert chat_interface.started.is_set()
    assert chat_interface.cancelled.is_set()
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "TimeoutError" in error


@pytest.mark.asyncio
async def test_approved_confirmation_without_source_message_skips_notification(
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_request(db_engine, source_message_internal_id=None)
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
    assert chat_interface.messages == []
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_notification_failure_does_not_retry_confirmed_tool_execution(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    chat_interface = FailingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
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
    assert len(chat_interface.messages) == 1
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "ConfirmationNotificationError" in error


@pytest.mark.asyncio
async def test_undelivered_notification_fails_confirmed_tool_execution(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    chat_interface = UndeliveredChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
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
    assert len(chat_interface.messages) == 1
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "ConfirmationNotificationError" in error


@pytest.mark.asyncio
async def test_fallback_notification_preserves_tool_result_attachments(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path),
        db_engine=db_engine,
        config=None,
    )
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = AttachmentToolsProvider()
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(
            provider,
            attachment_registry=attachment_registry,
        ),
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
            "Result:\ncreated attachment",
            "web-message-1",
        )
    ]
    attachment_ids = chat_interface.attachment_ids[0]
    assert attachment_ids is not None
    assert len(attachment_ids) == 1
    attachment = await attachment_registry.get_attachment_with_context(
        attachment_ids[0]
    )
    assert attachment is not None
    assert attachment.mime_type == "text/plain"
    assert attachment.conversation_id == "web-conversation-1"
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
    assert chat_interface.messages == [
        (
            "web-conversation-1",
            "Approved action failed.\n\n"
            "Tool: record_tool\n\n"
            "Error:\nError executing approved tool 'record_tool': "
            "Tool 'record_tool' denied by policy: no matching rule (default)",
            "web-message-1",
        )
    ]


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


@pytest.mark.asyncio
async def test_confirmation_task_uses_processing_service_from_source_profile(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(
        db_engine,
        processing_profile_id="secondary-profile",
    )
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)

    default_provider = RecordingDescriptorToolsProvider({ToolTag.STATE_CHANGING})
    default_policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=default_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(
                default_decision=ToolPolicyDecision.DENY,
                rules=[],
            )
        ),
    )
    secondary_provider = RecordingToolsProvider()

    default_service = _processing_service_with_registry(
        provider=default_policy_provider,
        service_id="test-profile",
    )
    secondary_service = _processing_service_with_registry(
        provider=secondary_provider,
        service_id="secondary-profile",
    )
    registry = {
        "test-profile": default_service,
        "secondary-profile": secondary_service,
    }
    cast("SimpleNamespace", default_service).processing_services_registry = registry
    cast("SimpleNamespace", secondary_service).processing_services_registry = registry

    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=default_service,
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert default_provider.calls == []
    assert secondary_provider.calls == [
        (
            "record_tool",
            {"value": "payload"},
            "call-record-tool",
            "user-1",
            "web",
        )
    ]
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_confirmation_task_fails_when_source_profile_is_missing(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(
        db_engine,
        processing_profile_id="secondary-profile",
    )
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    confirmation_result_waiters = ConfirmationResultWaiterRegistry()
    waiter = confirmation_result_waiters.register(request_id)
    provider = RecordingToolsProvider()
    default_service_in_registry = _processing_service_with_registry(
        provider=provider,
        service_id="test-profile",
    )
    processing_service = _processing_service_with_registry(
        provider=provider,
        service_id="test-profile",
        registry={"test-profile": default_service_in_registry},
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
        processing_service=processing_service,
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
        confirmation_result_waiters=confirmation_result_waiters,
    )

    await wait_for_condition(
        lambda: waiter.done(),
        timeout=1.0,
        description="live confirmation waiter to resolve after context failure",
    )
    outcome = waiter.result()
    assert outcome.kind == "failed"
    assert isinstance(outcome.result, str)
    assert "secondary-profile" in outcome.result
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "secondary-profile" in error
    assert provider.calls == []
    assert chat_interface.messages == []


@pytest.mark.asyncio
async def test_context_failure_notifies_original_conversation_without_live_waiter(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(
        db_engine,
        processing_profile_id="secondary-profile",
    )
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    default_service_in_registry = _processing_service_with_registry(
        provider=provider,
        service_id="test-profile",
    )
    processing_service = _processing_service_with_registry(
        provider=provider,
        service_id="test-profile",
        registry={"test-profile": default_service_in_registry},
    )
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=processing_service,
        chat_interface=chat_interface,
        task_id=task_id,
        allow_failures=True,
    )

    assert provider.calls == []
    assert len(chat_interface.messages) == 1
    conversation_id, message, reply_to_interface_id = chat_interface.messages[0]
    assert conversation_id == "web-conversation-1"
    assert reply_to_interface_id == "web-message-1"
    assert "Approved action failed." in message
    assert "secondary-profile" in message
    status, error = await _task_status(db_engine, task_id)
    assert status == "failed"
    assert error is not None
    assert "secondary-profile" in error
