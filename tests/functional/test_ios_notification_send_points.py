"""Functional tests for notification dispatch at the integrated send points.

These exercise public surfaces only: the ``notify_conversation`` helper, the
``ConfirmationService.create_request`` API, and the ``/webhook/event`` route.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm.messages import UserMessage
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.services.notification_targets import notify_conversation
from family_assistant.services.notifier import (
    CONFIRMATION_CATEGORY,
    MESSAGE_CATEGORY,
    NotificationMetadata,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.utils.clock import SystemClock
from family_assistant.web.app_creator import app as fastapi_app


class _RecordingNotifier:
    """A fake Notifier capturing dispatched notifications."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str, str]] = []
        self.metadata: list[NotificationMetadata | None] = []

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((user_identifier, title, body))
        self.metadata.append(metadata)


async def _add_user_message(
    db_engine: AsyncEngine, *, interface_type: str, conversation_id: str, user_id: str
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        await db.message_history.add_message(
            message=UserMessage(content="please do the thing"),
            interface_type=interface_type,
            conversation_id=conversation_id,
            timestamp=SystemClock().now(),
            user_id=user_id,
        )


@pytest.mark.asyncio
async def test_notify_conversation_resolves_and_dispatches(
    db_engine: AsyncEngine,
) -> None:
    """notify_conversation resolves the owning user and dispatches a notification."""
    await _add_user_message(
        db_engine, interface_type="web", conversation_id="conv-1", user_id="owner-1"
    )
    notifier = _RecordingNotifier()

    async with DatabaseContext(engine=db_engine) as db:
        dispatched = await notify_conversation(
            notifier,
            db,
            interface_type="web",
            conversation_id="conv-1",
            title="Title",
            body="Body",
        )

    assert dispatched is True
    assert notifier.calls == [("owner-1", "Title", "Body")]


@pytest.mark.asyncio
async def test_notify_conversation_no_owner_is_noop(db_engine: AsyncEngine) -> None:
    """No notification is dispatched when the owner cannot be resolved."""
    notifier = _RecordingNotifier()
    async with DatabaseContext(engine=db_engine) as db:
        dispatched = await notify_conversation(
            notifier,
            db,
            interface_type="web",
            conversation_id="conv-unknown",
            title="Title",
            body="Body",
        )

    assert dispatched is False
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_notify_conversation_disabled_is_noop(db_engine: AsyncEngine) -> None:
    """A disabled notifier is never invoked."""
    notifier = _RecordingNotifier(enabled=False)
    async with DatabaseContext(engine=db_engine) as db:
        dispatched = await notify_conversation(
            notifier,
            db,
            interface_type="web",
            conversation_id="conv-1",
            title="Title",
            body="Body",
        )

    assert dispatched is False
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_pending_confirmation_notifies_target_user(
    db_engine: AsyncEngine,
) -> None:
    """Creating a pending confirmation dispatches a notification to the target user."""
    notifier = _RecordingNotifier()
    service = ConfirmationService(
        db_context_factory=lambda: DatabaseContext(engine=db_engine),
        notifier=notifier,
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

    assert notifier.calls == [
        ("user-1", "Confirmation needed", "Create calendar event: Flight")
    ]
    metadata = notifier.metadata[0]
    assert metadata is not None
    assert metadata.category == CONFIRMATION_CATEGORY
    assert metadata.request_id is not None


@pytest.mark.asyncio
async def test_worker_completion_webhook_notifies_owner(
    db_engine: AsyncEngine,
) -> None:
    """A worker completion event posted to the webhook route notifies the owner."""
    await _add_user_message(
        db_engine, interface_type="web", conversation_id="conv-9", user_id="owner-9"
    )
    async with DatabaseContext(engine=db_engine) as db:
        await db.worker_tasks.create_task(
            task_id="wt-1",
            conversation_id="conv-9",
            interface_type="web",
            task_description="do the thing",
            model="claude",
            context_files=[],
            timeout_minutes=30,
            user_name="Owner",
            callback_token=None,
        )

    notifier = _RecordingNotifier()
    fastapi_app.state.database_engine = db_engine
    fastapi_app.state.notification_dispatcher = notifier
    try:
        transport = ASGITransport(app=fastapi_app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/webhook/event",
                json={
                    "event_type": "worker_completion",
                    "data": {"task_id": "wt-1", "outcome": "success"},
                },
            )
        assert response.status_code == 200
    finally:
        del fastapi_app.state.notification_dispatcher

    assert len(notifier.calls) == 1
    user_identifier, title, _ = notifier.calls[0]
    assert user_identifier == "owner-9"
    assert title == "Worker task complete"
    metadata = notifier.metadata[0]
    assert metadata is not None
    assert metadata.category == MESSAGE_CATEGORY
    assert metadata.conversation_id == "conv-9"
