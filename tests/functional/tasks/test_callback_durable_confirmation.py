"""Functional tests: scheduled-callback wakeups can request durable confirmations.

A scheduled callback / reminder is a "notification" turn run by the task worker
with no live channel. Such turns used to be denied confirm-gated tools entirely.
They now receive a deferred durable-confirmation callback addressed to the
owner recorded on the ``llm_callback`` payload (``created_by_user_id``); when no
owner is recorded the confirm-gated tool reports it cannot be approved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.interfaces import ChatInterface
from family_assistant.processing.types import ChatInteractionResult
from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import LlmCallbackPayload, handle_llm_callback
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolExecutionContext,
)
from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

TEST_INTERFACE_TYPE = "test"
TEST_CONVERSATION_ID = "callback_confirm_chat"
TEST_USER_NAME = "CallbackTester"


class CallbackCapturingService:
    """Fake processing service that exercises the wakeup confirmation callback."""

    def __init__(self) -> None:
        self.service_config = SimpleNamespace(
            id="callback_profile", allow_wake_llm=True
        )
        self.processing_services_registry: dict[str, object] = {}
        self.captured_callback: object = "unset"
        self.captured_user_id: object = "unset"
        self.confirmation_outcome: ConfirmationOutcome | None = None

    async def handle_chat_interaction(self, **kwargs: Any) -> ChatInteractionResult:  # noqa: ANN401 - test fake accepts the ProcessingService keyword surface
        self.captured_callback = kwargs["request_confirmation_callback"]
        self.captured_user_id = kwargs.get("user_id")
        db_context = cast("DatabaseContext", kwargs["db_context"])
        callback = kwargs["request_confirmation_callback"]
        if callback is not None:
            callback_context = ToolExecutionContext(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                user_name=TEST_USER_NAME,
                turn_id="callback_turn",
                db_context=db_context,
                processing_service=None,
                clock=SystemClock(),
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
                camera_backend=None,
                timezone=ZoneInfo("UTC"),
                processing_profile_id="callback_profile",
                request_confirmation_callback=callback,
                confirmation_ui_managers=kwargs["confirmation_ui_managers"],
                credential_resolvers=None,
                api_backend=None,
            )
            self.confirmation_outcome = await callback(
                interface_type=kwargs["interface_type"],
                conversation_id=kwargs["conversation_id"],
                turn_id="callback_turn",
                tool_name="delete_calendar_event",
                call_id="callback_confirm_call",
                tool_args={"event_id": "evt-callback"},
                timeout_seconds=42.0,
                context=callback_context,
            )
        return ChatInteractionResult.success(text_reply="callback handled")


def _exec_context(
    db_context: DatabaseContext,
    processing_service: CallbackCapturingService,
    chat_interface: ChatInterface,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
        turn_id="worker_turn",
        db_context=db_context,
        processing_service=cast("Any", processing_service),
        clock=SystemClock(),
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        chat_interface=chat_interface,
        credential_resolvers=None,
        api_backend=None,
    )


def _payload(*, created_by_user_id: str | None) -> LlmCallbackPayload:
    payload: LlmCallbackPayload = {
        "interface_type": TEST_INTERFACE_TYPE,
        "conversation_id": TEST_CONVERSATION_ID,
        "user_name": TEST_USER_NAME,
        "callback_context": "do the scheduled thing",
        "scheduling_timestamp": datetime.now(UTC).isoformat(),
    }
    if created_by_user_id is not None:
        payload["created_by_user_id"] = created_by_user_id
    return payload


@pytest.mark.asyncio
async def test_callback_with_owner_can_request_durable_confirmation(
    db_engine: AsyncEngine,
) -> None:
    """A scheduled callback owned by a user defers a confirm-gated tool call to a
    durable confirmation addressed to that owner."""
    processing_service = CallbackCapturingService()
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "sent_message_id"

    async with DatabaseContext(engine=db_engine) as db_context:
        await handle_llm_callback(
            _exec_context(db_context, processing_service, chat_interface),
            _payload(created_by_user_id="callback-owner"),
        )

    assert processing_service.captured_callback is not None
    # The owner is threaded as the turn's user_id too, so nested scheduled
    # actions (schedule_reminder, create_automation, ...) inherit the owner.
    assert processing_service.captured_user_id == "callback-owner"
    assert processing_service.confirmation_outcome is not None
    assert processing_service.confirmation_outcome.kind == "completed"
    assert processing_service.confirmation_outcome.action_attempted is False
    assert isinstance(processing_service.confirmation_outcome.result, str)
    assert "hasn't run yet" in processing_service.confirmation_outcome.result

    async with DatabaseContext(engine=db_engine) as db_context:
        pending = await db_context.confirmation_requests.list_pending_for_user(
            "callback-owner"
        )
    assert len(pending) == 1
    assert pending[0]["tool_name"] == "delete_calendar_event"
    assert pending[0]["origin_conversation_id"] == TEST_CONVERSATION_ID


@pytest.mark.asyncio
async def test_callback_without_owner_reports_tool_not_run(
    db_engine: AsyncEngine,
) -> None:
    """A legacy callback with no recorded owner still advertises confirm-gated
    tools, but calling one reports it cannot be approved and creates no request."""
    processing_service = CallbackCapturingService()
    chat_interface = AsyncMock(spec=ChatInterface)
    chat_interface.send_message.return_value = "sent_message_id"

    async with DatabaseContext(engine=db_engine) as db_context:
        await handle_llm_callback(
            _exec_context(db_context, processing_service, chat_interface),
            _payload(created_by_user_id=None),
        )

    assert processing_service.captured_callback is not None
    assert processing_service.confirmation_outcome is not None
    assert processing_service.confirmation_outcome.kind == "failed"
    assert isinstance(processing_service.confirmation_outcome.result, str)
    assert "no recorded owner" in processing_service.confirmation_outcome.result

    async with DatabaseContext(engine=db_engine) as db_context:
        rows = await db_context.confirmation_requests.list_pending_for_user("anyone")
    assert rows == []
