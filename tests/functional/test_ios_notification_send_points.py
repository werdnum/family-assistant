"""Functional tests for notification dispatch at the integrated send points."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import TaskWorker
from family_assistant.utils.clock import SystemClock
from family_assistant.web.routers.webhooks import (
    _handle_worker_completion,  # noqa: PLC2701
)

if TYPE_CHECKING:
    from family_assistant.storage.types import TaskDict


class _RecordingDispatcher:
    """A fake NotificationDispatcher capturing dispatched notifications."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str, str]] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Any,  # noqa: ANN401
    ) -> None:
        self.calls.append((user_identifier, title, body))


@pytest.mark.asyncio
async def test_pending_confirmation_notifies_target_user(
    db_engine: AsyncEngine,
) -> None:
    """Creating a pending confirmation dispatches a notification to the target user."""
    dispatcher = _RecordingDispatcher()
    service = ConfirmationService(
        db_context_factory=lambda: DatabaseContext(engine=db_engine),
        notification_dispatcher=dispatcher,  # type: ignore[arg-type]
    )

    await service.create_request(
        target_user_id="user-1",
        tool_name="calendar.create_event",
        tool_args={"title": "Flight"},
        tool_call_id="call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Flight",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert dispatcher.calls == [
        ("user-1", "Confirmation needed", "Create calendar event: Flight")
    ]


@pytest.mark.asyncio
async def test_worker_completion_notifies_conversation_owner(
    db_engine: AsyncEngine,
) -> None:
    """A worker completion webhook notifies the conversation owner."""
    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db:
        # Establish the owning user via a user message in the conversation.
        await db.message_history.add_message(
            message=UserMessage(content="please do the thing"),
            interface_type="web",
            conversation_id="conv-1",
            timestamp=clock.now(),
            user_id="owner-1",
        )
        await db.worker_tasks.create_task(
            task_id="wt-1",
            conversation_id="conv-1",
            interface_type="web",
            task_description="do the thing",
            model="claude",
            context_files=[],
            timeout_minutes=30,
            user_name="Owner",
            callback_token=None,
        )

    dispatcher = _RecordingDispatcher()
    async with DatabaseContext(engine=db_engine) as db:
        await _handle_worker_completion(
            db,
            {"task_id": "wt-1", "outcome": "success"},
            notification_dispatcher=dispatcher,  # type: ignore[arg-type]
        )

    assert len(dispatcher.calls) == 1
    user_identifier, title, _ = dispatcher.calls[0]
    assert user_identifier == "owner-1"
    assert title == "Worker task complete"


@pytest.mark.asyncio
async def test_worker_completion_no_owner_does_not_notify(
    db_engine: AsyncEngine,
) -> None:
    """No notification is sent when the conversation owner cannot be resolved."""
    async with DatabaseContext(engine=db_engine) as db:
        await db.worker_tasks.create_task(
            task_id="wt-2",
            conversation_id="conv-unknown",
            interface_type="web",
            task_description="do the thing",
            model="claude",
            context_files=[],
            timeout_minutes=30,
            user_name=None,
            callback_token=None,
        )

    dispatcher = _RecordingDispatcher()
    async with DatabaseContext(engine=db_engine) as db:
        await _handle_worker_completion(
            db,
            {"task_id": "wt-2", "outcome": "success"},
            notification_dispatcher=dispatcher,  # type: ignore[arg-type]
        )

    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_task_failure_notifies_conversation_owner(
    db_engine: AsyncEngine,
) -> None:
    """A task failed after retries notifies the conversation owner."""
    clock = SystemClock()
    async with DatabaseContext(engine=db_engine) as db:
        await db.message_history.add_message(
            message=UserMessage(content="run the automation"),
            interface_type="telegram",
            conversation_id="chat-9",
            timestamp=clock.now(),
            user_id="owner-9",
        )

    dispatcher = _RecordingDispatcher()
    worker = TaskWorker(
        processing_service=MagicMock(),
        chat_interface=MagicMock(),
        calendar_config={},
        timezone=ZoneInfo("UTC"),
        embedding_generator=MagicMock(),
        engine=db_engine,
        notification_dispatcher=dispatcher,  # type: ignore[arg-type]
    )

    task = cast(
        "TaskDict",
        {
            "task_id": "t-1",
            "task_type": "script_execution",
            "payload": {"conversation_id": "chat-9", "interface_type": "telegram"},
        },
    )
    async with DatabaseContext(engine=db_engine) as db:
        await worker._notify_task_failure(db, task)  # noqa: SLF001

    assert len(dispatcher.calls) == 1
    user_identifier, title, _ = dispatcher.calls[0]
    assert user_identifier == "owner-9"
    assert title == "Task failed"
