from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import select

from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.services.confirmation_waiters import (
    ConfirmationResultWaiterRegistry,
)
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.tasks import tasks_table
from family_assistant.web.routers.chat_api import (
    ToolConfirmationRequest,
    confirm_tool_execution,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from starlette.requests import Request


async def _create_confirmation(db_engine: AsyncEngine) -> str:
    service = ConfirmationService(
        db_context_factory=lambda: get_db_context(db_engine),
    )
    request = await service.create_request(
        target_user_id="test_user",
        tool_name="add_or_update_note",
        tool_args={"title": "Trip", "content": "Flight lands at 6pm"},
        tool_call_id="tool-call-123",
        source_message_internal_id=None,
        confirmation_prompt="Create a note for this itinerary?",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    return request["id"]


async def _task_exists(db_engine: AsyncEngine, task_id: str) -> bool:
    async with DatabaseContext(engine=db_engine) as db:
        row = await db.fetch_one(
            select(tasks_table.c.id).where(tasks_table.c.original_task_id == task_id)
        )
    return row is not None


@pytest.mark.asyncio
async def test_confirm_tool_decision_only_approval_does_not_enqueue_task(
    db_engine: AsyncEngine,
) -> None:
    request_id = await _create_confirmation(db_engine)
    waiters = ConfirmationResultWaiterRegistry()
    waiters.mark_decision_only(request_id)
    app_state = SimpleNamespace(
        database_engine=db_engine,
        confirmation_result_waiters=waiters,
    )
    request = cast(
        "Request",
        SimpleNamespace(app=SimpleNamespace(state=app_state)),
    )

    response = await confirm_tool_execution(
        ToolConfirmationRequest(
            request_id=request_id,
            approved=True,
            conversation_id=None,
            approving_interface="web",
        ),
        request,
        {"user_identifier": "test_user"},
    )

    assert response.success is True
    assert not await _task_exists(
        db_engine,
        f"confirmation_tool_execution:{request_id}",
    )

    async with DatabaseContext(engine=db_engine) as db:
        row = await db.fetch_one(
            select(confirmation_requests_table).where(
                confirmation_requests_table.c.id == request_id
            )
        )

    assert row is not None
    assert row["status"] == "approved"
    assert row["execution_task_id"] is None
