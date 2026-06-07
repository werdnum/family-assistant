"""Tests for asynchronous profile delegation runs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypedDict, cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from family_assistant.config_models import ToolsConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.storage import message_history_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import TaskWorker
from family_assistant.tools.services import (
    delegate_to_service_tool,
    get_delegation_status_tool,
    list_delegations_tool,
)
from family_assistant.tools.types import ConfirmationOutcome, ToolExecutionContext
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
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

    def __init__(self, *, request_confirmation: bool = False) -> None:
        self.service_config = SimpleNamespace(
            id="target_profile",
            allowed_delegation_sources=["source_profile"],
        )
        self.request_confirmation = request_confirmation
        self.calls: list[FakeDelegationCall] = []

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401
        self.calls.append(cast("FakeDelegationCall", kwargs))
        if self.request_confirmation:
            callback = kwargs["request_confirmation_callback"]
            assert callback is not None
            callback_context = ToolExecutionContext(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                user_name=TEST_USER_NAME,
                user_id="async-delegation-user",
                turn_id="delegated_tool_turn",
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
                turn_id="delegated_tool_turn",
                tool_name="confirmable_delegated_tool",
                call_id="confirmable_call_1",
                tool_args={"action": "write"},
                timeout_seconds=42.0,
                context=callback_context,
            )
            assert outcome.kind == "approved"
        return ChatInteractionResult.success(text_reply="background delegation done")


def _source_processing_service(
    target_service: FakeDelegatableService,
) -> ProcessingService:
    source_service = SimpleNamespace(
        service_config=SimpleNamespace(
            id="source_profile",
            tools_config=ToolsConfig(
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
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        chat_interfaces={TEST_INTERFACE_TYPE: chat_interface}
        if chat_interface
        else None,
        confirmation_ui_managers=confirmation_ui_managers,
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
    assert "Delegation is still running" in result.text
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
            source_message_internal_id=source_message_internal_id,
        )
    ]
    chat_interface.send_message.assert_awaited_once()
    sent_kwargs = chat_interface.send_message.await_args.kwargs
    assert delegation_id in sent_kwargs["text"]
    assert "background delegation done" in sent_kwargs["text"]

    async with DatabaseContext(engine=db_engine) as db_context:
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
