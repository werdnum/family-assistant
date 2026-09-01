"""Functional tests for queue-backed durable confirmation execution."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace  # pylint: disable=no-name-in-module
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from family_assistant import task_worker as task_worker_module
from family_assistant.config_models import ToolCallReviewConfig, ToolsConfig
from family_assistant.embeddings import MockEmbeddingGenerator
from family_assistant.interfaces import ChatDeliveryError
from family_assistant.llm.messages import UserMessage
from family_assistant.processing.types import (
    ChatInteractionResult,
    ChatInteractionStatus,
)
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationService,
)
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.services.deferred_tool_confirmation import (
    create_deferred_tool_confirmation,
)
from family_assistant.services.tool_call_review import ToolCallReviewer
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import tasks_table
from family_assistant.task_worker import TaskWorker, handle_confirmation_tool_execution
from family_assistant.tools.infrastructure import (
    PolicyEnforcingToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.metadata import ToolDescriptor, ToolTag
from family_assistant.tools.policy import (
    PolicyEngine,
    PolicyRule,
    ToolMatcher,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.services import delegate_to_service_tool
from family_assistant.tools.types import (
    ToolAttachment,
    ToolCallReviewAuthorization,
    ToolExecutionContext,
    ToolResult,
)
from tests.helpers import wait_for_condition, wait_for_tasks_to_complete

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.processing import ProcessingService
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

DELEGATION_TOOL_DEFINITION: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "delegate_to_service",
        "description": "Delegate a request to another profile.",
        "parameters": {
            "type": "object",
            "properties": {
                "target_service_id": {"type": "string"},
                "user_request": {"type": "string"},
                "confirm_delegation": {"type": "boolean"},
            },
            "required": ["target_service_id", "user_request"],
        },
    },
}


class RecordingToolsProvider:
    """Fake tool provider that records executions."""

    def __init__(self) -> None:
        # ast-grep-ignore: no-dict-any - fake tool calls preserve arbitrary tool arguments
        self.calls: list[tuple[str, dict[str, Any], str | None, str | None, str]] = []
        self.contexts: list[ToolExecutionContext] = []

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
        self.contexts.append(context)
        return f"executed:{arguments['value']}"

    async def close(self) -> None:
        return None


class CountingReviewLLM:
    """Reviewer LLM fake that records any unexpected structured invocation."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        del messages, response_model, max_retries
        self.calls += 1
        raise AssertionError("Durable review authorization must bypass the reviewer")


class DelegationReplayToolsProvider:
    """Descriptor provider that executes the real delegation tool."""

    def __init__(self) -> None:
        self.calls = 0
        self._descriptor = ToolDescriptor(
            name="delegate_to_service",
            definition=DELEGATION_TOOL_DEFINITION,
            tags=frozenset({ToolTag.DELEGATION, ToolTag.OUTPUT_UNSPECIFIED}),
            origin="local",
        )

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return [DELEGATION_TOOL_DEFINITION]

    async def get_tool_descriptors(self) -> list[ToolDescriptor]:
        return [self._descriptor]

    async def get_tool_descriptor(self, name: str) -> ToolDescriptor | None:
        return self._descriptor if name == self._descriptor.name else None

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool provider protocol accepts arbitrary JSON arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> ToolResult:
        assert name == "delegate_to_service"
        assert call_id is not None
        self.calls += 1
        return await delegate_to_service_tool(
            exec_context=context,
            target_service_id=cast("str", arguments["target_service_id"]),
            user_request=cast("str", arguments["user_request"]),
            confirm_delegation=cast("bool", arguments.get("confirm_delegation", False)),
        )

    async def close(self) -> None:
        return None


class RecordingDelegationTarget:
    """Synchronous target profile used by durable delegation replay."""

    def __init__(self) -> None:
        self.calls = 0
        self.service_config = SimpleNamespace(
            id="target-profile",
            allowed_delegation_sources=None,
        )

    async def handle_chat_interaction(self, **_kwargs: object) -> ChatInteractionResult:
        self.calls += 1
        return ChatInteractionResult(
            status=ChatInteractionStatus.SUCCESS,
            text_reply="durably delegated",
        )


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


class ReplayingConfirmationProvider(RecordingToolsProvider):
    """Probe the approved callback twice with the same stored call."""

    def __init__(self) -> None:
        super().__init__()
        self.outcome_kinds: list[str] = []

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - fake tool calls preserve arbitrary tool arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        assert context.request_confirmation_callback is not None
        assert call_id is not None
        attempted_arguments = [
            {**arguments, "value": "mismatched-payload"},
            arguments,
            arguments,
        ]
        for candidate_arguments in attempted_arguments:
            outcome = await context.request_confirmation_callback(
                interface_type=context.interface_type,
                conversation_id=context.conversation_id,
                turn_id=context.turn_id,
                tool_name=name,
                call_id=call_id,
                tool_args=candidate_arguments,
                timeout_seconds=1,
                context=context,
            )
            self.outcome_kinds.append(outcome.kind)
        return "replay probe complete"


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


class FailingDescriptorToolsProvider(RecordingDescriptorToolsProvider):
    """Descriptor provider that records an attempted call and then raises."""

    async def execute_tool(
        self,
        name: str,
        # ast-grep-ignore: no-dict-any - tool provider protocol accepts arbitrary JSON arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str:
        await super().execute_tool(name, arguments, context, call_id)
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
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        # ast-grep-ignore: no-asyncio-sleep-in-tests - fake chat I/O must yield to exercise cancellation cleanup
        await asyncio.sleep(0)
        _ = (parse_mode, on_behalf_of_user_id, taint_metadata)
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
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        await super().send_message(
            conversation_id=conversation_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
            on_behalf_of_user_id=on_behalf_of_user_id,
            taint_metadata=taint_metadata,
        )
        raise RuntimeError("chat send failed")


class UndeliveredChatInterface(RecordingChatInterface):
    """Fake chat interface whose sends are refused."""

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str:
        await super().send_message(
            conversation_id=conversation_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_interface_id=reply_to_interface_id,
            attachment_ids=attachment_ids,
            on_behalf_of_user_id=on_behalf_of_user_id,
            taint_metadata=taint_metadata,
        )
        raise ChatDeliveryError("the interface refused the message", transient=True)


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
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        _ = (
            conversation_id,
            text,
            parse_mode,
            reply_to_interface_id,
            attachment_ids,
            on_behalf_of_user_id,
            taint_metadata,
        )
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
    credential_resolvers: object | None = None,
    api_backend: object | None = None,
) -> ProcessingService:
    service_config = SimpleNamespace(
        id="test-profile",
        timezone=ZoneInfo("UTC"),
        visibility_grants=None,
        default_note_visibility_labels=None,
        required_note_visibility_labels=None,
        allowed_note_visibility_labels=None,
        allow_wake_llm=True,
        note_registry=None,
    )
    service = SimpleNamespace(
        kind="local",
        tools_provider=provider,
        service_config=service_config,
        attachment_registry=attachment_registry,
        home_assistant_client=None,
        camera_backend=None,
        credential_resolvers=credential_resolvers,
        api_backend=api_backend,
        processing_services_registry=None,
    )
    return cast("ProcessingService", service)


def _confirmation_service(db_engine: AsyncEngine) -> ConfirmationService:
    return ConfirmationService(db=Database(engine=db_engine))


async def _create_source_message(
    db_engine: AsyncEngine,
    *,
    processing_profile_id: str = "test-profile",
) -> int:
    db = Database(engine=db_engine)
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
    tool_name: str = "record_tool",
    tool_call_id: str = "call-record-tool",
    confirmation_prompt: str = "Run record_tool with value payload",
    tool_args: ToolArguments | None = None,
    origin_interface_type: str | None = None,
    origin_conversation_id: str | None = None,
    taint_state_json: TaintMetadata | None = None,
    sink_class: str | None = None,
    static_policy_reason: str | None = None,
    taint_policy_reason: str | None = None,
) -> str:
    resolved_tool_args: ToolArguments = (
        tool_args if tool_args is not None else {"value": "payload"}
    )
    request = await _confirmation_service(db_engine).create_request(
        target_user_id="user-1",
        tool_name=tool_name,
        tool_args=resolved_tool_args,
        tool_call_id=tool_call_id,
        source_message_internal_id=source_message_internal_id,
        confirmation_prompt=confirmation_prompt,
        expires_at=datetime_now_utc() + timedelta(hours=1),
        origin_interface_type=origin_interface_type,
        origin_conversation_id=origin_conversation_id,
        taint_state_json=taint_state_json,
        sink_class=sink_class,
        static_policy_reason=static_policy_reason,
        taint_policy_reason=taint_policy_reason,
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
    db = Database(engine=db_engine)
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
async def test_deferred_review_confirmation_persists_call_authorization(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    context = ToolExecutionContext(
        interface_type="automation",
        conversation_id="automation-1",
        user_name="Automation Owner",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        tool_call_review_authorization=ToolCallReviewAuthorization(
            tool_name="record_tool",
            call_id="reviewed-call",
            tool_args={"value": "reviewed-payload"},
            sink_class=SinkClass.ARTIFACT_WRITE.value,
            static_policy_reason="Static review requested confirmation.",
            taint_policy_reason="Unknown external content reached an artifact write.",
        ),
    )

    outcome = await create_deferred_tool_confirmation(
        context=context,
        tool_name="record_tool",
        call_id="reviewed-call",
        tool_args={"value": "reviewed-payload"},
        timeout_seconds=60,
        target_user_id="user-1",
        source_prefix="Automation requested approval.",
    )

    assert outcome.kind == "completed"
    pending = await db.confirmation_requests.list_pending_for_user("user-1")
    assert len(pending) == 1
    request = pending[0]
    assert request["sink_class"] == SinkClass.ARTIFACT_WRITE.value
    assert request["static_policy_reason"] == ("Static review requested confirmation.")
    assert request["taint_policy_reason"] == (
        "Unknown external content reached an artifact write."
    )


@pytest.mark.asyncio
async def test_approved_confirmation_callback_rejects_mismatch_and_replay(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = ReplayingConfirmationProvider()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=RecordingChatInterface(),
        task_id=task_id,
    )

    assert provider.outcome_kinds == ["rejected", "approved", "rejected"]
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
async def test_approved_confirmation_preserves_google_dependencies(
    db_engine: AsyncEngine,
) -> None:
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()
    credential_resolvers = object()
    api_backend = object()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(
            provider,
            credential_resolvers=credential_resolvers,
            api_backend=api_backend,
        ),
        chat_interface=RecordingChatInterface(),
        task_id=task_id,
    )

    assert len(provider.contexts) == 1
    executed_context = provider.contexts[0]
    assert executed_context.credential_resolvers is credential_resolvers
    assert executed_context.api_backend is api_backend


@pytest.mark.asyncio
async def test_approved_confirmation_executes_with_taint_tracker(
    db_engine: AsyncEngine,
) -> None:
    """Requests without recorded taint state still execute with a real tracker.

    A trackerless context would let the tool result be persisted without taint
    metadata; legacy requests (no taint_state_json) must instead start from an
    explicit empty state.
    """
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
    )
    task_id = await _approve_request(db_engine, request_id)
    provider = RecordingToolsProvider()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=RecordingChatInterface(),
        task_id=task_id,
    )

    assert len(provider.contexts) == 1
    executed_context = provider.contexts[0]
    assert executed_context.taint_tracker is not None
    assert (
        executed_context.taint_tracker.snapshot().max_tier
        == SourceTrustTier.TRUSTED_USER
    )


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
    wrapped_provider = RecordingDescriptorToolsProvider({ToolTag.OUTPUT_UNTRUSTED})
    provider = TaintTrackingToolsProvider(wrapped_provider)
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
    assert outcome.taint_metadata is not None
    assert (
        TurnTaintState.from_metadata(outcome.taint_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )
    assert wrapped_provider.calls == [
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
    wrapped_provider = FailingDescriptorToolsProvider({ToolTag.OUTPUT_UNTRUSTED})
    provider = TaintTrackingToolsProvider(wrapped_provider)
    chat_interface = RecordingChatInterface()

    db = Database(engine=db_engine)
    await db.execute(
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
    assert outcome.taint_metadata is not None
    assert (
        TurnTaintState.from_metadata(outcome.taint_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )
    assert wrapped_provider.calls == [
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
async def test_source_less_confirmation_delivers_to_origin_conversation(
    db_engine: AsyncEngine,
) -> None:
    """A confirmation with no source message (e.g. a scheduled callback or
    delegation wakeup) threads its result back to the recorded origin
    conversation, not just the user's primary Telegram chat."""
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=None,
        origin_interface_type="web",
        origin_conversation_id="origin-conversation-1",
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

    assert chat_interface.messages == [
        (
            "origin-conversation-1",
            "Approved action completed.\n\n"
            "Tool: record_tool\n\n"
            "Result:\nexecuted:payload",
            None,
        )
    ]
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
        attachment_ids[0], acting_user_id=None
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

    db = Database(engine=db_engine)
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
        sink_class=SinkClass.ARTIFACT_WRITE.value,
        static_policy_reason="Previously reviewed under static policy.",
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

    db = Database(engine=db_engine)
    await db.execute(
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
async def test_approved_review_confirmation_does_not_invoke_reviewer_twice(
    db_engine: AsyncEngine,
) -> None:
    """The durable human approval reuses the exact call's persisted judgment."""
    source_message_id = await _create_source_message(db_engine)
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
        tool_args={"value": "reviewed-payload"},
        sink_class=SinkClass.ARTIFACT_WRITE.value,
        static_policy_reason="Static review required human confirmation.",
    )
    task_id = await _approve_request(db_engine, request_id)
    wrapped_provider = RecordingDescriptorToolsProvider({ToolTag.STATE_CHANGING})
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=wrapped_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.REVIEW)
        ),
    )
    review_llm = CountingReviewLLM()
    reviewer = ToolCallReviewer(
        cast("LLMInterface", review_llm),
        ToolCallReviewConfig(),
    )
    provider = TaintTrackingToolsProvider(
        policy_provider,
        tool_call_reviewer=reviewer,
        review_config=ToolCallReviewConfig(),
    )
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=_processing_service(provider),
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert review_llm.calls == 0
    assert wrapped_provider.calls == [
        (
            "record_tool",
            {"value": "reviewed-payload"},
            "call-record-tool",
            "user-1",
            "web",
        )
    ]
    assert await _task_status(db_engine, task_id) == ("done", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_decision",
    [ToolPolicyDecision.REVIEW, ToolPolicyDecision.CONFIRM],
)
async def test_approved_delegate_confirmation_replays_exact_call_end_to_end(
    db_engine: AsyncEngine,
    policy_decision: ToolPolicyDecision,
) -> None:
    """An approved outer delegation also satisfies its internal confirmation."""
    tool_args: ToolArguments = {
        "target_service_id": "target-profile",
        "user_request": "Handle this approved request.",
        "confirm_delegation": True,
    }
    source_message_id = await _create_source_message(
        db_engine,
        processing_profile_id="source-profile",
    )
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
        tool_name="delegate_to_service",
        tool_call_id="approved-delegation-call",
        confirmation_prompt="Delegate this exact request to target-profile",
        tool_args=tool_args,
        sink_class=SinkClass.SANDBOX_NETWORK.value,
        static_policy_reason="Static review required human confirmation.",
    )
    task_id = await _approve_request(db_engine, request_id)
    wrapped_provider = DelegationReplayToolsProvider()
    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=wrapped_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=policy_decision)
        ),
    )
    review_llm = CountingReviewLLM()
    provider = TaintTrackingToolsProvider(
        policy_provider,
        tool_call_reviewer=ToolCallReviewer(
            cast("LLMInterface", review_llm),
            ToolCallReviewConfig(),
        ),
        review_config=ToolCallReviewConfig(),
        delegation_sink_classes={
            "target-profile": SinkClass.SANDBOX_NETWORK,
        },
    )
    target_service = RecordingDelegationTarget()
    source_service = cast(
        "SimpleNamespace",
        _processing_service_with_registry(
            provider=provider,
            service_id="source-profile",
            registry={"target-profile": target_service},
        ),
    )
    source_service.service_config.tools_config = ToolsConfig(
        async_delegation_enabled=False
    )
    chat_interface = RecordingChatInterface()

    await _run_worker_until_task_finishes(
        db_engine,
        processing_service=cast("ProcessingService", source_service),
        chat_interface=chat_interface,
        task_id=task_id,
    )

    assert review_llm.calls == 0
    assert wrapped_provider.calls == 1
    assert target_service.calls == 1
    assert chat_interface.messages == [
        (
            "web-conversation-1",
            "Approved action completed.\n\n"
            "Tool: delegate_to_service\n\n"
            "Result:\ndurably delegated",
            "web-message-1",
        )
    ]
    assert await _task_status(db_engine, task_id) == ("done", None)


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
    request_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="confirmed-email",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="confirmation request derived from external email",
        )
    )
    request_id = await _create_request(
        db_engine,
        source_message_internal_id=source_message_id,
        taint_state_json=request_taint.to_metadata(),
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

    db = Database(engine=db_engine)
    await db.execute(
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
    assert outcome.taint_metadata is not None
    assert (
        TurnTaintState.from_metadata(outcome.taint_metadata).max_tier
        == SourceTrustTier.UNKNOWN_EXTERNAL
    )
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
