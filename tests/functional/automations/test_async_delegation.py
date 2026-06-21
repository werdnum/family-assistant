"""Tests for asynchronous profile delegation runs."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypedDict, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update

from family_assistant.a2a.client import (
    A2AClientError,
    A2APermanentError,
    A2ATaskNotFoundError,
)
from family_assistant.config_models import ToolsConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing import PENDING, RemoteSubmission
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage import message_history_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.delegation_runs import delegation_runs_table
from family_assistant.task_worker import (
    DelegatedProfileRunPayload,
    DelegationNotificationError,
    TaskWorker,
)

# The inline-delivery helpers are module-internal but are exercised directly here
# to cover the tool-side fast path (notified-at marking and empty-text attachment
# delivery) without standing up a concurrent worker loop.
from family_assistant.tools.services import (
    _completed_delegation_result,  # noqa: PLC2701
    _inline_delegation_result,  # noqa: PLC2701
    delegate_to_service_tool,
    get_delegation_status_tool,
    list_delegations_tool,
)
from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing import ProcessingService
    from family_assistant.telegram.protocols import ConfirmationUIManager

TEST_INTERFACE_TYPE = "test_interface"
TEST_CONVERSATION_ID = "async_delegation_chat"
TEST_USER_NAME = "AsyncDelegationTester"


class FakeDelegationCall(TypedDict):
    """Call fields asserted by the fake target service."""

    request_confirmation_callback: object
    subconversation_id: object


class FakeConfirmationRequest(TypedDict):
    """Confirmation fields captured by the fake UI manager."""

    conversation_id: str
    interface_type: str
    turn_id: str | None
    prompt_text: str
    tool_name: str
    # ast-grep-ignore: no-dict-any - confirmation requests carry arbitrary tool arguments
    tool_args: dict[str, Any]
    timeout: float
    target_user_id: str | None
    tool_call_id: str | None
    source_message_internal_id: int | None
    wait_for_durable_execution: bool


class FakeConfirmationUIManager:
    """Confirmation manager that records and approves delegated confirmations."""

    def __init__(self) -> None:
        self.requests: list[FakeConfirmationRequest] = []

    async def request_confirmation(
        self,
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        # ast-grep-ignore: no-dict-any - confirmation requests carry arbitrary tool arguments
        tool_args: dict[str, Any],
        timeout: float,
        target_user_id: str | None = None,
        tool_call_id: str | None = None,
        source_message_internal_id: int | None = None,
        wait_for_durable_execution: bool = True,
    ) -> ConfirmationOutcome:
        self.requests.append(
            FakeConfirmationRequest(
                conversation_id=conversation_id,
                interface_type=interface_type,
                turn_id=turn_id,
                prompt_text=prompt_text,
                tool_name=tool_name,
                tool_args=tool_args,
                timeout=timeout,
                target_user_id=target_user_id,
                tool_call_id=tool_call_id,
                source_message_internal_id=source_message_internal_id,
                wait_for_durable_execution=wait_for_durable_execution,
            )
        )
        return ConfirmationOutcome(kind="approved")

    async def send_existing_confirmation_request(
        self,
        conversation_id: str,
        request_id: str,
        prompt_text: str,
    ) -> ConfirmationOutcome:
        _ = conversation_id
        _ = request_id
        _ = prompt_text
        return ConfirmationOutcome(kind="completed")


class FakeDelegatableService:
    """Minimal delegatable target service for async delegation tests."""

    kind = "local"

    def __init__(
        self,
        *,
        request_confirmation: bool = False,
        attachment_registry: AttachmentRegistry | None = None,
    ) -> None:
        self.service_config = SimpleNamespace(
            id="target_profile",
            allowed_delegation_sources=["source_profile"],
        )
        self.request_confirmation = request_confirmation
        self.attachment_registry = attachment_registry
        self.calls: list[FakeDelegationCall] = []

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401
        self.calls.append(cast("FakeDelegationCall", kwargs))
        if self.request_confirmation:
            delegated_turn_id = "delegated_tool_turn"
            await kwargs["db_context"].message_history.add_message(
                UserMessage(content="delegated target request"),
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                timestamp=SystemClock().now(),
                turn_id=delegated_turn_id,
                processing_profile_id=self.service_config.id,
                subconversation_id=kwargs["subconversation_id"],
                user_id="async-delegation-user",
            )
            callback = kwargs["request_confirmation_callback"]
            assert callback is not None
            callback_context = ToolExecutionContext(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                user_name=TEST_USER_NAME,
                user_id="async-delegation-user",
                turn_id=delegated_turn_id,
                db_context=kwargs["db_context"],
                processing_service=None,
                clock=SystemClock(),
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
                camera_backend=None,
                timezone=ZoneInfo("UTC"),
                request_confirmation_callback=callback,
                confirmation_ui_managers=kwargs["confirmation_ui_managers"],
            )
            outcome = await callback(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                turn_id=delegated_turn_id,
                tool_name="confirmable_delegated_tool",
                call_id="confirmable_call_1",
                tool_args={"action": "write"},
                timeout_seconds=42.0,
                context=callback_context,
            )
            assert outcome.kind == "approved"
        attachment_ids: list[str] | None = None
        if self.attachment_registry is not None:
            attachment = (
                await self.attachment_registry.store_and_register_tool_attachment(
                    file_content=b"delegated attachment",
                    filename="delegated-output.txt",
                    content_type="text/plain",
                    tool_name="target_profile_tool",
                    description="Delegated output",
                    conversation_id=kwargs["conversation_id"],
                    db_context=kwargs["db_context"],
                )
            )
            attachment_ids = [attachment.attachment_id]
        return ChatInteractionResult.success(
            text_reply="background delegation done",
            attachment_ids=attachment_ids,
        )


class AttachmentVisibilityChatInterface:
    """Chat interface that verifies attachment rows are visible while notifying."""

    def __init__(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        self.db_engine = db_engine
        self.attachment_registry = attachment_registry
        self.sent_attachment_ids: list[str] | None = None
        self.visible_attachment_ids: list[str] = []

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> str | None:
        _ = (conversation_id, text, parse_mode, reply_to_interface_id)
        self.sent_attachment_ids = attachment_ids
        if attachment_ids:
            async with DatabaseContext(engine=self.db_engine) as db_context:
                visible = await self.attachment_registry.get_attachments(
                    db_context,
                    attachment_ids,
                )
            self.visible_attachment_ids = list(visible)
        return "external_message_id"


def _source_processing_service(
    target_service: FakeDelegatableService,
    *,
    async_delegation_enabled: bool = True,
) -> ProcessingService:
    source_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="source_profile",
            tools_config=ToolsConfig(
                async_delegation_enabled=async_delegation_enabled,
                delegate_handoff_after_seconds=15.0,
                delegate_status_poll_seconds=0.05,
            ),
            visibility_grants=None,
            default_note_visibility_labels=None,
        ),
        processing_services_registry={"target_profile": target_service},
        home_assistant_client=None,
        attachment_registry=None,
    )
    return cast("ProcessingService", source_service)


def _tool_context(
    db_context: DatabaseContext,
    processing_service: ProcessingService,
    chat_interface: ChatInterface | None = None,
    confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
    attachment_registry: AttachmentRegistry | None = None,
    in_script: bool = False,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
        user_id="async-delegation-user",
        turn_id="turn_async_delegation",
        db_context=db_context,
        processing_service=processing_service,
        clock=SystemClock(),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        chat_interfaces={TEST_INTERFACE_TYPE: chat_interface}
        if chat_interface
        else None,
        confirmation_ui_managers=confirmation_ui_managers,
        in_script=in_script,
    )


@pytest.mark.asyncio
async def test_delegate_to_service_background_reference_and_completion_notification(
    db_engine: AsyncEngine,
) -> None:
    target_service = FakeDelegatableService(request_confirmation=True)
    processing_service = _source_processing_service(target_service)
    confirmation_manager = FakeConfirmationUIManager()
    confirmation_ui_managers: dict[str, ConfirmationUIManager] = {
        TEST_INTERFACE_TYPE: confirmation_manager
    }
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    async with DatabaseContext(engine=db_engine) as db_context:
        source_message_internal_id = await db_context.message_history.add_message(
            UserMessage(content="delegate this"),
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=TEST_CONVERSATION_ID,
            timestamp=SystemClock().now(),
            turn_id="turn_async_delegation",
            user_id="async-delegation-user",
        )
        result = await delegate_to_service_tool(
            exec_context=_tool_context(
                db_context,
                processing_service,
                chat_interface,
                confirmation_ui_managers,
            ),
            target_service_id="target_profile",
            user_request="do this in the background",
            delivery_hint="background",
        )

    assert source_message_internal_id is not None
    assert result.text is not None
    assert "running in the background" in result.text
    assert "do not call get_delegation_status in a loop" in result.text.lower()
    assert isinstance(result.data, dict)
    delegation_id = result.data["delegation_id"]
    assert isinstance(delegation_id, str)
    assert delegation_id.startswith("delegation_")

    worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        confirmation_ui_managers=confirmation_ui_managers,
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            {
                "delegation_id": delegation_id,
                "interface_type": TEST_INTERFACE_TYPE,
                "conversation_id": TEST_CONVERSATION_ID,
                "user_name": TEST_USER_NAME,
            },
        )

    assert len(target_service.calls) == 1
    assert target_service.calls[0]["request_confirmation_callback"] is not None
    assert target_service.calls[0]["subconversation_id"]
    assert len(confirmation_manager.requests) == 1
    confirmation_request = confirmation_manager.requests[0]
    assert confirmation_manager.requests == [
        FakeConfirmationRequest(
            conversation_id=TEST_CONVERSATION_ID,
            interface_type=TEST_INTERFACE_TYPE,
            turn_id="turn_async_delegation",
            prompt_text="Confirm execution of tool: confirmable_delegated_tool",
            tool_name="confirmable_delegated_tool",
            tool_args={"action": "write"},
            timeout=42.0,
            target_user_id="async-delegation-user",
            tool_call_id="confirmable_call_1",
            source_message_internal_id=confirmation_request[
                "source_message_internal_id"
            ],
            wait_for_durable_execution=False,
        )
    ]
    delegated_source_message_internal_id = confirmation_request[
        "source_message_internal_id"
    ]
    assert delegated_source_message_internal_id is not None
    assert delegated_source_message_internal_id != source_message_internal_id
    chat_interface.send_message.assert_awaited_once()
    sent_kwargs = chat_interface.send_message.await_args.kwargs
    assert delegation_id in sent_kwargs["text"]
    assert "background delegation done" in sent_kwargs["text"]

    async with DatabaseContext(engine=db_engine) as db_context:
        delegated_source_row = await db_context.message_history.get_row_by_internal_id(
            delegated_source_message_internal_id
        )
        assert delegated_source_row is not None
        assert delegated_source_row["turn_id"] == "delegated_tool_turn"
        assert delegated_source_row["processing_profile_id"] == "target_profile"
        assert (
            delegated_source_row["subconversation_id"]
            == target_service.calls[0]["subconversation_id"]
        )

        status_result = await get_delegation_status_tool(
            _tool_context(db_context, processing_service, chat_interface),
            delegation_id=delegation_id,
        )
        assert isinstance(status_result.data, dict)
        assert status_result.data["status"] == "completed"
        assert status_result.data["result_text"] == "background delegation done"

        list_result = await list_delegations_tool(
            _tool_context(db_context, processing_service, chat_interface)
        )
        assert isinstance(list_result.data, list)
        assert list_result.data[0]["delegation_id"] == delegation_id

        notification_rows = await db_context.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
            .where(message_history_table.c.role == "assistant")
            .where(message_history_table.c.content.like(f"%{delegation_id}%"))
        )
        assert len(notification_rows) == 1
        assert notification_rows[0]["subconversation_id"] is None


async def _create_run(
    db_context: DatabaseContext,
    *,
    delegation_id: str,
    interface_type: str = TEST_INTERFACE_TYPE,
) -> str:
    """Create a queued delegation run and return its delegation_id."""
    await db_context.delegation_runs.create_run({
        "delegation_id": delegation_id,
        "task_id": f"task_{delegation_id}",
        "source_profile_id": "source_profile",
        "target_service_id": "target_profile",
        "interface_type": interface_type,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_id": "async-delegation-user",
        "user_name": TEST_USER_NAME,
        "source_turn_id": "turn_async_delegation",
        "subconversation_id": f"sub_{delegation_id}",
        "request_text": "do the thing",
        "content_parts_json": [],
    })
    return delegation_id


def _payload(delegation_id: str) -> DelegatedProfileRunPayload:
    return DelegatedProfileRunPayload(
        delegation_id=delegation_id,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
    )


def _build_worker(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    chat_interface: ChatInterface,
    confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
) -> TaskWorker:
    return TaskWorker(
        processing_service=processing_service,
        chat_interface=chat_interface,
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        confirmation_ui_managers=confirmation_ui_managers,
    )


@pytest.mark.asyncio
async def test_worker_does_not_notify_when_caller_did_not_hand_off(
    db_engine: AsyncEngine,
) -> None:
    """If the caller never handed off, it delivers inline; the worker must not notify."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_no_handoff")

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _payload("delegation_no_handoff"),
        )

    chat_interface.send_message.assert_not_awaited()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_no_handoff"
        )
        assert run is not None
        assert run["status"] == "completed"
        assert run["notified_at"] is None


@pytest.mark.asyncio
async def test_terminal_run_renotifies_when_not_yet_notified(
    db_engine: AsyncEngine,
) -> None:
    """A terminal run whose notification never landed is re-notified on retry (C3)."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_renotify")
        await db_context.delegation_runs.mark_handed_off(
            "delegation_renotify", clock.now()
        )
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_renotify",
            result_text="already done",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _payload("delegation_renotify"),
        )

    # The terminal run was not re-executed, but the notification was delivered.
    assert target_service.calls == []
    chat_interface.send_message.assert_awaited_once()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_renotify"
        )
        assert run is not None
        assert run["notified_at"] is not None


@pytest.mark.asyncio
async def test_failed_delivery_is_not_recorded_as_notified(
    db_engine: AsyncEngine,
) -> None:
    """A chat delivery that fails (send_message -> None) leaves the run unnotified.

    ChatInterface.send_message returns None when delivery fails (invalid chat,
    Bot API error, ...). The terminal run must stay notified_at NULL so it is
    retried, and the speculative message-history row must be rolled back rather
    than left dangling.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = None

    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_send_fails")
        await db_context.delegation_runs.mark_handed_off(
            "delegation_send_fails", clock.now()
        )
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_send_fails",
            result_text="done but undeliverable",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        with pytest.raises(DelegationNotificationError):
            await worker.handle_delegated_profile_run(
                _tool_context(db_context, processing_service, chat_interface),
                _payload("delegation_send_fails"),
            )

    chat_interface.send_message.assert_awaited_once()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_send_fails"
        )
        assert run is not None
        assert run["notified_at"] is None
        # The notification message row was rolled back, not left dangling.
        rows = await db_context.fetch_all(
            select(message_history_table).where(
                message_history_table.c.conversation_id == TEST_CONVERSATION_ID
            )
        )
        assert rows == []


@pytest.mark.asyncio
async def test_worker_commits_delegated_attachments_before_notification(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """Generated attachment rows are committed before non-web notification delivery."""
    attachment_storage = tmp_path / "attachments"
    attachment_storage.mkdir()
    attachment_registry = AttachmentRegistry(
        storage_path=str(attachment_storage),
        db_engine=db_engine,
        config=None,
    )
    target_service = FakeDelegatableService(attachment_registry=attachment_registry)
    processing_service = _source_processing_service(target_service)
    chat_interface = AttachmentVisibilityChatInterface(
        db_engine,
        attachment_registry,
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_with_attachment")
        await db_context.delegation_runs.mark_handed_off(
            "delegation_with_attachment",
            SystemClock().now(),
        )

    worker = _build_worker(
        db_engine,
        processing_service,
        cast("ChatInterface", chat_interface),
    )
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(
                db_context,
                processing_service,
                cast("ChatInterface", chat_interface),
            ),
            _payload("delegation_with_attachment"),
        )

    assert chat_interface.sent_attachment_ids is not None
    assert chat_interface.visible_attachment_ids == chat_interface.sent_attachment_ids

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_with_attachment"
        )
        assert run is not None
        assert run["notified_at"] is not None
        assert run["result_attachment_ids_json"] == chat_interface.sent_attachment_ids


@pytest.mark.asyncio
async def test_running_run_is_failed_not_reexecuted(db_engine: AsyncEngine) -> None:
    """A run found 'running' on entry was interrupted; fail it, don't re-run (C6)."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_interrupted")
        await db_context.delegation_runs.mark_handed_off(
            "delegation_interrupted", clock.now()
        )
        await db_context.delegation_runs.mark_running(
            "delegation_interrupted", clock.now()
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _payload("delegation_interrupted"),
        )

    assert target_service.calls == []
    chat_interface.send_message.assert_awaited_once()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_interrupted"
        )
        assert run is not None
        assert run["status"] == "failed"
        assert run["error"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_reaper_fails_and_notifies_stale_run(
    db_engine: AsyncEngine,
    status: str,
) -> None:
    """The cleanup task fails and notifies runs stranded queued or running (C21).

    The run is never handed off, so this also exercises the reaper's
    force-notify path: a reaped run has no live caller to deliver inline.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    clock = SystemClock()
    stale_created_at = clock.now() - timedelta(hours=2)
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_stale")
        if status == "running":
            await db_context.delegation_runs.mark_running(
                "delegation_stale", stale_created_at
            )
        # Backdate created_at so the reaper's created_at threshold matches.
        await db_context.execute_with_retry(
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == "delegation_stale")
            .values(created_at=stale_created_at)
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {"running_timeout_seconds": 60.0},
        )

    chat_interface.send_message.assert_awaited_once()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id("delegation_stale")
        assert run is not None
        assert run["status"] == "failed"
        assert run["notified_at"] is not None


@pytest.mark.asyncio
async def test_reaper_leaves_recent_runs_untouched(db_engine: AsyncEngine) -> None:
    """A freshly-created run is not reaped (its created_at is within threshold)."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_recent")

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {"running_timeout_seconds": 3600.0},
        )

    chat_interface.send_message.assert_not_awaited()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id("delegation_recent")
        assert run is not None
        assert run["status"] == "queued"


@pytest.mark.asyncio
async def test_mark_running_only_transitions_queued_runs(
    db_engine: AsyncEngine,
) -> None:
    """mark_running is conditional on ``queued`` so the reaper can't be raced.

    A run the stale-run reaper already failed (or a sibling worker already
    started) must not be resurrected to ``running`` and re-executed.
    """
    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_guard")
        # First claim succeeds while still queued.
        started = await db_context.delegation_runs.mark_running(
            "delegation_guard", clock.now()
        )
        assert started is not None
        assert started["status"] == "running"
        # A second claim (now running) matches no row.
        assert (
            await db_context.delegation_runs.mark_running(
                "delegation_guard", clock.now()
            )
            is None
        )

        # A run the reaper already failed cannot be resurrected to running.
        await _create_run(db_context, delegation_id="delegation_reaped")
        await db_context.delegation_runs.mark_failed(
            delegation_id="delegation_reaped",
            error="reaped",
            completed_at=clock.now(),
        )
        assert (
            await db_context.delegation_runs.mark_running(
                "delegation_reaped", clock.now()
            )
            is None
        )
        reaped = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_reaped"
        )
        assert reaped is not None
        assert reaped["status"] == "failed"


@pytest.mark.asyncio
async def test_cleanup_recovers_terminal_unnotified_run(
    db_engine: AsyncEngine,
) -> None:
    """A terminal run stranded unnotified (caller crashed before handoff) is recovered.

    The caller died after the run finished but before delivering inline or
    claiming the handoff, so handed_off_at is NULL and the worker's gated notify
    skipped it; reap_stale (queued/running only) never sees a terminal run. The
    cleanup's recovery sweep delivers it.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    clock = SystemClock()
    stale_completed_at = clock.now() - timedelta(hours=2)
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_stranded")
        # Terminal, never handed off, never notified, finished long ago.
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_stranded",
            result_text="orphaned result",
            result_attachment_ids=[],
            completed_at=stale_completed_at,
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {"running_timeout_seconds": 60.0},
        )

    chat_interface.send_message.assert_awaited_once()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_stranded"
        )
        assert run is not None
        assert run["notified_at"] is not None


@pytest.mark.asyncio
async def test_inline_delivery_marks_run_notified(db_engine: AsyncEngine) -> None:
    """Delivering a terminal result inline records notified_at.

    The inline result is returned to the model as the tool output rather than
    posted to the conversation, so handed_off_at stays NULL. Marking notified_at
    stops the cleanup sweep's find_terminal_unnotified backstop from re-delivering
    the same result into the conversation once the run ages past its window.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    clock = SystemClock()

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_inline")
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_inline",
            result_text="fast inline result",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id("delegation_inline")
        assert run is not None
        assert run["notified_at"] is None
        result = await _inline_delegation_result(
            _tool_context(db_context, processing_service),
            target_service_id="target_profile",
            run=run,
        )
        assert result is not None
        assert result.text == "fast inline result"

    async with DatabaseContext(engine=db_engine) as db_context:
        marked = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_inline"
        )
        assert marked is not None
        assert marked["notified_at"] is not None
        assert marked["handed_off_at"] is None


@pytest.mark.asyncio
async def test_cleanup_does_not_redeliver_inline_delivered_run(
    db_engine: AsyncEngine,
) -> None:
    """An inline-delivered run is not re-delivered by the cleanup sweep.

    Once inline delivery has marked the run notified, find_terminal_unnotified
    must skip it even after it ages past the completed_at window — otherwise every
    fast inline delegation would get a duplicate completion notification ~1h later.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    clock = SystemClock()
    stale_completed_at = clock.now() - timedelta(hours=2)
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_inline_aged")
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_inline_aged",
            result_text="delivered inline",
            result_attachment_ids=[],
            completed_at=stale_completed_at,
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_inline_aged"
        )
        assert run is not None
        # The caller delivers the terminal result inline, which marks it notified.
        await _inline_delegation_result(
            _tool_context(db_context, processing_service),
            target_service_id="target_profile",
            run=run,
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {"running_timeout_seconds": 60.0},
        )

    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_result_delivers_attachments_when_text_empty(
    db_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A completed delegation with attachments but no text still delivers them inline.

    Parity with the async notification path: an empty textual reply must not drop
    attachments (e.g. a data_visualization delegation that returns only a chart).
    """
    attachment_storage = tmp_path / "attachments"
    attachment_storage.mkdir()
    attachment_registry = AttachmentRegistry(
        storage_path=str(attachment_storage),
        db_engine=db_engine,
        config=None,
    )
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    clock = SystemClock()

    async with DatabaseContext(engine=db_engine) as db_context:
        stored = await attachment_registry.store_and_register_tool_attachment(
            file_content=b"chart bytes",
            filename="chart.png",
            content_type="image/png",
            tool_name="data_visualization",
            description="Generated chart",
            conversation_id=TEST_CONVERSATION_ID,
            db_context=db_context,
        )
        await _create_run(db_context, delegation_id="delegation_attach_no_text")
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_attach_no_text",
            result_text=None,
            result_attachment_ids=[stored.attachment_id],
            completed_at=clock.now(),
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(
            "delegation_attach_no_text"
        )
        assert run is not None
        result = await _completed_delegation_result(
            _tool_context(
                db_context,
                processing_service,
                attachment_registry=attachment_registry,
            ),
            target_service_id="target_profile",
            run=run,
        )

    assert result.attachments is not None
    assert len(result.attachments) == 1
    assert result.attachments[0].attachment_id == stored.attachment_id
    assert "no textual response" in (result.text or "")


@pytest.mark.asyncio
async def test_delegation_runs_synchronously_when_async_disabled(
    db_engine: AsyncEngine,
) -> None:
    """With async_delegation_enabled=False the target runs inline.

    The kill switch reverts to the pre-async behavior: the result is returned
    directly from the tool call and no durable delegation run is created, so no
    worker handoff, notification, or reaper machinery is involved.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(
        target_service, async_delegation_enabled=False
    )
    chat_interface = AsyncMock(spec=ChatInterface)

    async with DatabaseContext(engine=db_engine) as db_context:
        result = await delegate_to_service_tool(
            exec_context=_tool_context(db_context, processing_service, chat_interface),
            target_service_id="target_profile",
            user_request="do it now",
        )

    assert result.text == "background delegation done"
    assert len(target_service.calls) == 1

    async with DatabaseContext(engine=db_engine) as db_context:
        runs = await db_context.delegation_runs.list_for_conversation(
            conversation_id=TEST_CONVERSATION_ID,
            interface_type=TEST_INTERFACE_TYPE,
            status=None,
            limit=10,
        )
        assert runs == []


@pytest.mark.asyncio
async def test_delegation_runs_synchronously_inside_a_script(
    db_engine: AsyncEngine,
) -> None:
    """Inside a script (in_script=True) delegation runs inline even when async is on.

    A script is synchronous code; an async handoff that posts the result as a later
    conversation message is useless to it. The result is returned directly and no
    durable delegation run is created.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)

    async with DatabaseContext(engine=db_engine) as db_context:
        result = await delegate_to_service_tool(
            exec_context=_tool_context(
                db_context,
                processing_service,
                chat_interface,
                in_script=True,
            ),
            target_service_id="target_profile",
            user_request="do it now",
        )

    assert result.text == "background delegation done"
    assert len(target_service.calls) == 1

    async with DatabaseContext(engine=db_engine) as db_context:
        runs = await db_context.delegation_runs.list_for_conversation(
            conversation_id=TEST_CONVERSATION_ID,
            interface_type=TEST_INTERFACE_TYPE,
            status=None,
            limit=10,
        )
        assert runs == []


@pytest.mark.asyncio
async def test_status_tools_report_disabled_when_async_off(
    db_engine: AsyncEngine,
) -> None:
    """get_delegation_status/list_delegations explain that async delegation is off."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(
        target_service, async_delegation_enabled=False
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        status_result = await get_delegation_status_tool(
            _tool_context(db_context, processing_service),
            delegation_id="delegation_anything",
        )
        list_result = await list_delegations_tool(
            _tool_context(db_context, processing_service),
        )

    assert "disabled" in (status_result.text or "")
    assert "disabled" in (list_result.text or "")
    assert list_result.data == []


@pytest.mark.asyncio
async def test_status_tools_nudge_to_stop_polling_while_pending(
    db_engine: AsyncEngine,
) -> None:
    """A still-running delegation tells the model to wait for the notification, not poll."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_pending")

        status_result = await get_delegation_status_tool(
            _tool_context(db_context, processing_service),
            delegation_id="delegation_pending",
        )
        list_result = await list_delegations_tool(
            _tool_context(db_context, processing_service),
        )

    assert isinstance(status_result.data, dict)
    assert status_result.data["status"] == "queued"
    assert "do not poll in a loop" in (status_result.text or "").lower()
    assert "do not poll in a loop" in (list_result.text or "").lower()


@pytest.mark.asyncio
async def test_status_tools_omit_nudge_once_terminal(
    db_engine: AsyncEngine,
) -> None:
    """Once a delegation is terminal, the status tools drop the stop-polling nudge."""
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    clock = SystemClock()

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_done")
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_done",
            result_text="all finished",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )

        status_result = await get_delegation_status_tool(
            _tool_context(db_context, processing_service),
            delegation_id="delegation_done",
        )
        list_result = await list_delegations_tool(
            _tool_context(db_context, processing_service),
        )

    assert isinstance(status_result.data, dict)
    assert status_result.data["status"] == "completed"
    assert "do not poll in a loop" not in (status_result.text or "").lower()
    assert "do not poll in a loop" not in (list_result.text or "").lower()


@pytest.mark.asyncio
async def test_api_delegation_completion_stored_in_history(
    db_engine: AsyncEngine,
) -> None:
    """API/iOS delegations persist the completion to history, not a chat send.

    For history-based interfaces (web/api/ios) there is no real ChatInterface
    (it would be NullChatInterface and drop the result). The terminal result
    must be stored in message history and marked notified without send_message.
    """
    target_service = FakeDelegatableService()
    processing_service = _source_processing_service(target_service)
    chat_interface = AsyncMock(spec=ChatInterface)

    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(
            db_context, delegation_id="delegation_api", interface_type="api"
        )
        await db_context.delegation_runs.mark_handed_off("delegation_api", clock.now())
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_api",
            result_text="api result",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _payload("delegation_api"),
        )

    chat_interface.send_message.assert_not_awaited()
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id("delegation_api")
        assert run is not None
        assert run["notified_at"] is not None
        rows = await db_context.fetch_all(
            select(message_history_table).where(
                message_history_table.c.conversation_id == TEST_CONVERSATION_ID,
                message_history_table.c.interface_type == "api",
            )
        )
        assert len(rows) == 1
        assert "api result" in rows[0]["content"]


@pytest.mark.asyncio
async def test_mark_handed_off_is_refused_once_terminal(
    db_engine: AsyncEngine,
) -> None:
    """The handoff claim wins only while non-terminal, so it never strands a result (C1)."""
    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(db_context, delegation_id="delegation_claimable")
        claimed = await db_context.delegation_runs.mark_handed_off(
            "delegation_claimable", clock.now()
        )
        assert claimed is True

        await _create_run(db_context, delegation_id="delegation_terminal")
        await db_context.delegation_runs.mark_completed(
            delegation_id="delegation_terminal",
            result_text="done",
            result_attachment_ids=[],
            completed_at=clock.now(),
        )
        refused = await db_context.delegation_runs.mark_handed_off(
            "delegation_terminal", clock.now()
        )
        assert refused is False


@pytest.mark.asyncio
async def test_web_delegation_can_request_confirmation(db_engine: AsyncEngine) -> None:
    """A web-interface delegation gets a confirmation callback from the web manager (C7)."""
    target_service = FakeDelegatableService(request_confirmation=True)
    processing_service = _source_processing_service(target_service)
    confirmation_manager = FakeConfirmationUIManager()
    confirmation_ui_managers: dict[str, ConfirmationUIManager] = {
        "web": confirmation_manager
    }
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    async with DatabaseContext(engine=db_engine) as db_context:
        await _create_run(
            db_context, delegation_id="delegation_web", interface_type="web"
        )
        await db_context.delegation_runs.mark_handed_off(
            "delegation_web", SystemClock().now()
        )

    worker = _build_worker(
        db_engine, processing_service, chat_interface, confirmation_ui_managers
    )
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(
                db_context,
                processing_service,
                chat_interface,
                confirmation_ui_managers,
            ),
            {
                "delegation_id": "delegation_web",
                "interface_type": "web",
                "conversation_id": TEST_CONVERSATION_ID,
                "user_name": TEST_USER_NAME,
            },
        )

    assert len(target_service.calls) == 1
    assert target_service.calls[0]["request_confirmation_callback"] is not None
    assert len(confirmation_manager.requests) == 1
    assert confirmation_manager.requests[0]["interface_type"] == "web"
    assert confirmation_manager.requests[0]["tool_name"] == "confirmable_delegated_tool"


@pytest.mark.asyncio
async def test_get_messages_after_defaults_to_main_conversation_only(
    db_engine: AsyncEngine,
) -> None:
    clock = SystemClock()
    after = clock.now()

    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.message_history.add_message(
            AssistantMessage(content="main message"),
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=TEST_CONVERSATION_ID,
            timestamp=clock.now(),
        )
        await db_context.message_history.add_message(
            AssistantMessage(content="delegated scratch message"),
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=TEST_CONVERSATION_ID,
            timestamp=clock.now(),
            subconversation_id="delegated-subconversation",
        )

        main_messages = await db_context.message_history.get_messages_after_as_dict(
            conversation_id=TEST_CONVERSATION_ID,
            after=after,
            interface_type=TEST_INTERFACE_TYPE,
        )
        all_messages = await db_context.message_history.get_messages_after_as_dict(
            conversation_id=TEST_CONVERSATION_ID,
            after=after,
            interface_type=TEST_INTERFACE_TYPE,
            subconversation_id="*",
        )

    assert [message["content"] for message in main_messages] == ["main message"]
    assert [message["content"] for message in all_messages] == [
        "main message",
        "delegated scratch message",
    ]


class FakePollableService:
    """Minimal pollable (remote) target for submit-then-poll worker tests.

    Implements the PollableDelegationService protocol with deterministic,
    in-memory submit/poll/cancel behaviour — a fake, not a mock: no patching,
    the worker drives it through the real protocol.
    """

    kind = "remote"

    def __init__(
        self,
        *,
        poll_results: list[ChatInteractionResult | object] | None = None,
        submit_terminal: ChatInteractionResult | None = None,
        submit_error: BaseException | None = None,
        submit_errors: list[BaseException | None] | None = None,
    ) -> None:
        self.service_config = SimpleNamespace(
            id="target_profile",
            allowed_delegation_sources=["source_profile"],
        )
        self._poll_results = list(poll_results or [])
        self._submit_terminal = submit_terminal
        self._submit_error = submit_error
        # A per-call sequence of submit outcomes (None = success) lets a test make
        # the first submit fail and a later re-submit land; falls back to the
        # single submit_error (applied to every call) when not provided.
        self._submit_errors = list(submit_errors) if submit_errors is not None else None
        self._next_task_seq = 1
        # Each entry is conversation_id, subconversation_id, assigned_id. The
        # assigned_id is None for a submit that raised before the remote
        # assigned an id.
        self.submitted: list[tuple[str, str | None, str | None]] = []
        self.cancelled: list[str] = []
        self.inline_calls = 0

    async def handle_chat_interaction(self, **kwargs: object) -> ChatInteractionResult:
        # A pollable target is always driven through submit_async/poll_async; the
        # kwargs are never inspected here, so object (not Any) types them and the
        # method just records the (forbidden) inline call.
        _ = kwargs
        self.inline_calls += 1
        raise AssertionError("a pollable target must not run inline")

    def remote_context_id(
        self, conversation_id: str, subconversation_id: str | None
    ) -> str | None:
        return f"{subconversation_id or conversation_id}:remote"

    async def submit_async(
        self,
        content_parts: object,
        *,
        conversation_id: str,
        subconversation_id: str | None,
    ) -> RemoteSubmission:
        _ = content_parts
        error = self._submit_error
        if self._submit_errors is not None:
            error = self._submit_errors.pop(0) if self._submit_errors else None
        if error is not None:
            # Record the failed attempt too, so tests can count submit calls.
            self.submitted.append((conversation_id, subconversation_id, None))
            raise error
        # Per A2A spec the client sends no task id on create; the remote assigns
        # one. Mint a fresh server-side id for each successful submit.
        assigned_id = f"srv-{self._next_task_seq}"
        self._next_task_seq += 1
        self.submitted.append((conversation_id, subconversation_id, assigned_id))
        return RemoteSubmission(
            remote_task_id=assigned_id,
            remote_context_id=self.remote_context_id(
                conversation_id, subconversation_id
            ),
            terminal_result=self._submit_terminal,
        )

    async def poll_async(
        self, remote_task_id: str, remote_context_id: str | None
    ) -> ChatInteractionResult | object:
        _ = (remote_task_id, remote_context_id)
        item = self._poll_results.pop(0)
        # An injected exception lets a test exercise transient vs permanent
        # poll-error handling.
        if isinstance(item, BaseException):
            raise item
        return item

    async def cancel_async(self, remote_task_id: str) -> None:
        self.cancelled.append(remote_task_id)


def _delegation_payload(delegation_id: str) -> DelegatedProfileRunPayload:
    return {
        "delegation_id": delegation_id,
        "interface_type": TEST_INTERFACE_TYPE,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
    }


async def _start_background_delegation(
    db_engine: AsyncEngine,
    processing_service: ProcessingService,
    chat_interface: ChatInterface,
) -> str:
    """Run delegate_to_service in background mode; return the delegation id."""
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.message_history.add_message(
            UserMessage(content="delegate"),
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=TEST_CONVERSATION_ID,
            timestamp=SystemClock().now(),
            turn_id="turn_async_delegation",
            user_id="async-delegation-user",
        )
        result = await delegate_to_service_tool(
            exec_context=_tool_context(db_context, processing_service, chat_interface),
            target_service_id="target_profile",
            user_request="do this remotely",
            delivery_hint="background",
        )
    assert isinstance(result.data, dict)
    return cast("str", result.data["delegation_id"])


@pytest.mark.asyncio
async def test_pollable_delegation_submits_and_enqueues_poll(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    assert len(target.submitted) == 1
    assert target.inline_calls == 0
    # The worker submits without a client task id (A2A spec §3.4.2); the remote
    # assigns one, which the worker reconciles into the run for polling.
    assigned_task_id = target.submitted[0][2]
    assert assigned_task_id is not None
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["remote_task_id"] == assigned_task_id
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) == 1
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pollable_delegation_retry_reattaches_without_duplicate_submit(
    db_engine: AsyncEngine,
) -> None:
    # A delegated_profile_run retry of an awaiting_remote run that already learned
    # its remote id re-attaches by enqueuing a poll, NOT by re-submitting (which
    # would create a duplicate remote task).
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
    assert len(target.submitted) == 1
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        stored_id = run["remote_task_id"]
        assert stored_id is not None

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
    # No second submit: the retry re-attached via a poll, keeping the same id.
    assert len(target.submitted) == 1
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["remote_task_id"] == stored_id
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) >= 1


@pytest.mark.asyncio
async def test_pollable_delegation_retry_resubmits_when_id_unknown(
    db_engine: AsyncEngine,
) -> None:
    # A retry of an awaiting_remote run whose submit response was lost (NULL
    # remote id) re-submits to (re)create the task and learn its id.
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    # Claim awaiting_remote with a NULL id (the submit response was never seen).
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.delegation_runs.mark_awaiting_remote(
            delegation_id,
            remote_task_id=None,
            remote_context_id=None,
            started_at=SystemClock().now(),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
    # The retry re-submitted and reconciled the remote-assigned id.
    assert len(target.submitted) == 1
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["remote_task_id"] == target.submitted[0][2]


@pytest.mark.asyncio
async def test_pollable_delegation_transient_submit_error_recovers_via_poll(
    db_engine: AsyncEngine,
) -> None:
    # A transient submit error (the request may have landed but the response was
    # lost): keep the run awaiting_remote and schedule a poll to reconcile.
    target = FakePollableService(submit_error=A2AClientError("response lost"))
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    assert len(target.submitted) == 1
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) == 1
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pollable_delegation_deterministic_submit_error_fails_fast(
    db_engine: AsyncEngine,
) -> None:
    # A deterministic submit error (bad auth / protocol error): fail fast with
    # the real error instead of polling until the cap.
    target = FakePollableService(submit_error=A2APermanentError("bad auth"))
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "failed"
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert polls == []
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_reenqueues_lost_poll_for_young_run(
    db_engine: AsyncEngine,
) -> None:
    # A young awaiting_remote run whose poll task was lost gets a fresh poll from
    # the cleanup pass rather than waiting for the cap.
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    # Claim awaiting_remote directly (no poll enqueued = a "lost" poll).
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.delegation_runs.mark_awaiting_remote(
            delegation_id,
            remote_task_id="rt-lost",
            remote_context_id=None,
            started_at=SystemClock().now(),
        )
        polls_before = await db_context.tasks.get_all(task_type="delegation_poll")
        assert polls_before == []

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {},
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) == 1
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_does_not_reenqueue_poll_for_null_id_run_within_grace(
    db_engine: AsyncEngine,
) -> None:
    # A NULL-id awaiting_remote run still WITHIN the submit grace is likely
    # mid-first-submit; the reaper must NOT re-enqueue a poll for it, which would
    # re-submit and race the in-flight submit into a duplicate remote task.
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.delegation_runs.mark_awaiting_remote(
            delegation_id,
            remote_task_id=None,
            remote_context_id=None,
            started_at=SystemClock().now(),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {},
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert polls == []
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
    assert target.submitted == []
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_reenqueues_poll_for_null_id_run_past_grace(
    db_engine: AsyncEngine,
) -> None:
    # A NULL-id awaiting_remote run PAST the submit grace (but within the cap) is
    # stuck — its first submit has returned and its only poll was lost — so the
    # reaper re-enqueues a poll to recover it rather than leaving it idle to the
    # cap. The poll then re-submits (NULL id) and reconciles a remote id.
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.delegation_runs.mark_awaiting_remote(
            delegation_id,
            remote_task_id=None,
            remote_context_id=None,
            # Past the 300s submit grace but well within the 3600s cap.
            started_at=SystemClock().now() - timedelta(minutes=10),
        )

    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {},
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) == 1
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pollable_delegation_polls_to_completion(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService(
        poll_results=[
            PENDING,
            ChatInteractionResult.success(text_reply="remote done"),
        ]
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    poll_payload = _delegation_payload(delegation_id)
    # First poll: still pending -> reschedules, bumps the attempt counter.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            poll_payload,
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["poll_attempts"] == 1
    chat_interface.send_message.assert_not_awaited()

    # Second poll: terminal -> finalize and notify.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            poll_payload,
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["result_text"] == "remote done"
    chat_interface.send_message.assert_awaited_once()
    assert "remote done" in chat_interface.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_pollable_delegation_synchronous_remote_completes_on_submit(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService(
        submit_terminal=ChatInteractionResult.success(text_reply="sync remote done")
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["result_text"] == "sync remote done"
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert polls == []
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reap_stale_awaiting_remote_cancels_and_fails(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService()
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        # Age the run past the wall-clock cap so the reaper gives up on it.
        await db_context.execute_with_retry(
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(started_at=SystemClock().now() - timedelta(hours=2))
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_run_cleanup(
            _tool_context(db_context, processing_service, chat_interface),
            {},
        )

    assert target.cancelled == [target.submitted[0][2]]
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "failed"
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_poll_past_cap_delivers_terminal_result(
    db_engine: AsyncEngine,
) -> None:
    # A poll that fires after the cap (backoff / scheduler delay) but finds the
    # remote already terminal must DELIVER the result, not fail it as a timeout:
    # the task may have finished just before the cap.
    target = FakePollableService(
        poll_results=[ChatInteractionResult.success(text_reply="finished just in time")]
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        # Age the run past the cap so the poll fires "late".
        await db_context.execute_with_retry(
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(started_at=SystemClock().now() - timedelta(hours=2))
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["result_text"] == "finished just in time"
    # The terminal task was delivered, not cancelled.
    assert target.cancelled == []
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_poll_past_cap_cancels_when_still_pending(
    db_engine: AsyncEngine,
) -> None:
    # A poll past the cap that finds the remote STILL pending cancels + fails it.
    target = FakePollableService(poll_results=[PENDING])
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        await db_context.execute_with_retry(
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(started_at=SystemClock().now() - timedelta(hours=2))
        )

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "failed"
    assert target.cancelled == [target.submitted[0][2]]
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_pollable_delegation_poll_transient_error_reschedules(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService(
        poll_results=[
            A2AClientError("network blip"),
            ChatInteractionResult.success(text_reply="recovered"),
        ]
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    poll_payload = _delegation_payload(delegation_id)
    # Transient A2AClientError -> stays awaiting_remote and reschedules.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            poll_payload,
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
    chat_interface.send_message.assert_not_awaited()

    # Next poll succeeds -> completed.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            poll_payload,
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"


@pytest.mark.asyncio
async def test_pollable_delegation_poll_permanent_error_fails_fast(
    db_engine: AsyncEngine,
) -> None:
    target = FakePollableService(
        poll_results=[A2APermanentError("bad auth / protocol error")]
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )

    # A non-transport error fails the run immediately rather than looping to cap.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "failed"
    chat_interface.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_pollable_delegation_poll_not_found_resubmits(
    db_engine: AsyncEngine,
) -> None:
    # The remote reports its task is not found (it lost the task, e.g. a restart).
    # Rather than failing, the worker re-submits (no client id; the remote assigns
    # a fresh one), reconciles the new id, and keeps polling.
    target = FakePollableService(poll_results=[A2ATaskNotFoundError("task not found")])
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
    assert len(target.submitted) == 1
    first_task_id = target.submitted[0][2]

    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
    # The poll re-submitted (creating a fresh remote task with a new id) and
    # enqueued another poll — it did not fail the run.
    assert len(target.submitted) == 2
    new_task_id = target.submitted[1][2]
    assert new_task_id is not None
    assert new_task_id != first_task_id
    async with DatabaseContext(engine=db_engine) as db_context:
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        # The run reconciled the newly-assigned remote id.
        assert run["remote_task_id"] == new_task_id
        polls = await db_context.tasks.get_all(task_type="delegation_poll")
        assert len(polls) >= 1
    chat_interface.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pollable_delegation_recovers_when_submit_never_landed(
    db_engine: AsyncEngine,
) -> None:
    # The full recovery chain Codex flagged: the first message/send fails
    # transiently, so the run is awaiting_remote with no remote id yet. The
    # scheduled poll sees the NULL id and re-submits; the re-submit lands and the
    # run polls through to completion — a momentary outage during submit must not
    # become a permanent failure.
    target = FakePollableService(
        submit_errors=[A2AClientError("connection reset"), None],
        poll_results=[
            ChatInteractionResult.success(text_reply="recovered after resubmit"),
        ],
    )
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "external_message_id"

    delegation_id = await _start_background_delegation(
        db_engine, processing_service, chat_interface
    )
    worker = _build_worker(db_engine, processing_service, chat_interface)
    # Submit fails transiently -> awaiting_remote (NULL id) + poll enqueued.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegated_profile_run(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["remote_task_id"] is None
    assert len(target.submitted) == 1

    # Poll sees the NULL id -> re-submit (now lands) -> reconcile id -> working.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "awaiting_remote"
        assert run["remote_task_id"] == target.submitted[1][2]
    assert len(target.submitted) == 2
    chat_interface.send_message.assert_not_awaited()

    # Next poll: the recreated task is terminal -> finalize and notify.
    async with DatabaseContext(engine=db_engine) as db_context:
        await worker.handle_delegation_poll(
            _tool_context(db_context, processing_service, chat_interface),
            _delegation_payload(delegation_id),
        )
        run = await db_context.delegation_runs.get_by_delegation_id(delegation_id)
        assert run is not None
        assert run["status"] == "completed"
        assert run["result_text"] == "recovered after resubmit"
    chat_interface.send_message.assert_awaited_once()
