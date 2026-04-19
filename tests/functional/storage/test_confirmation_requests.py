"""Functional tests for durable tool confirmations."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationExpiredError,
    ConfirmationService,
)
from family_assistant.storage.context import DatabaseContext


def _service(db_engine: AsyncEngine) -> ConfirmationService:
    return ConfirmationService(
        db_context_factory=lambda: DatabaseContext(engine=db_engine)
    )


async def _create_request(
    db_engine: AsyncEngine,
    *,
    target_user_id: str = "user-1",
    expires_at: datetime | None = None,
) -> str:
    request = await _service(db_engine).create_request(
        target_user_id=target_user_id,
        tool_name="calendar.create_event",
        tool_args={"title": "Flight", "start": "2026-05-01T09:00:00-07:00"},
        tool_call_id="call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Flight",
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
    )
    return request["id"]


@pytest.mark.asyncio
async def test_create_request_is_visible_from_separate_context(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    created = await service.create_request(
        target_user_id="user-1",
        tool_name="calendar.create_event",
        tool_args={"title": "Ticket", "start": "2026-05-01T09:00:00-07:00"},
        tool_call_id="call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Ticket",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async with DatabaseContext(engine=db_engine) as db:
        fetched = await db.confirmation_requests.get(created["id"])

    assert fetched is not None
    assert fetched["status"] == "pending"
    assert fetched["target_user_id"] == "user-1"
    assert fetched["tool_args_json"]["title"] == "Ticket"


@pytest.mark.asyncio
async def test_approval_enqueues_execution_task_atomically(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine)

    approved = await service.approve_and_enqueue_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )

    expected_task_id = f"confirmation_tool_execution:{request_id}"
    assert approved["status"] == "approved"
    assert approved["resolved_by_user_id"] == "user-1"
    assert approved["resolved_via_interface"] == "web"
    assert approved["execution_task_id"] == expected_task_id

    async with DatabaseContext(engine=db_engine) as db:
        tasks = await db.tasks.get_all(
            status="pending",
            task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
        )

    assert len(tasks) == 1
    assert tasks[0]["task_id"] == expected_task_id
    assert tasks[0]["payload"] == {"confirmation_request_id": request_id}


@pytest.mark.asyncio
async def test_replayed_approval_does_not_enqueue_second_task(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine)

    first = await service.approve_and_enqueue_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )
    second = await service.approve_and_enqueue_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )

    assert second["id"] == first["id"]
    assert second["status"] == "approved"
    async with DatabaseContext(engine=db_engine) as db:
        tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert [task["task_id"] for task in tasks] == [
        f"confirmation_tool_execution:{request_id}"
    ]


@pytest.mark.asyncio
async def test_wrong_user_cannot_approve_request(db_engine: AsyncEngine) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine, target_user_id="user-1")

    with pytest.raises(ConfirmationAuthorizationError):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-2",
            approving_interface="web",
        )

    async with DatabaseContext(engine=db_engine) as db:
        request = await db.confirmation_requests.get(request_id)
        tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "pending"
    assert tasks == []


@pytest.mark.asyncio
async def test_reject_blocks_later_approval(db_engine: AsyncEngine) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine)

    rejected = await service.reject(
        request_id=request_id,
        rejecting_user_id="user-1",
        rejecting_interface="telegram",
    )

    assert rejected["status"] == "rejected"
    assert rejected["resolved_via_interface"] == "telegram"

    with pytest.raises(ConfirmationAlreadyResolvedError):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-1",
            approving_interface="web",
        )

    async with DatabaseContext(engine=db_engine) as db:
        tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert tasks == []


@pytest.mark.asyncio
async def test_mark_expired_expires_only_pending_requests(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    expired_id = await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    pending_id = await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    expired_count = await service.mark_expired(now=datetime.now(UTC))

    assert expired_count == 1
    async with DatabaseContext(engine=db_engine) as db:
        expired = await db.confirmation_requests.get(expired_id)
        pending = await db.confirmation_requests.get(pending_id)

    assert expired is not None
    assert expired["status"] == "expired"
    assert pending is not None
    assert pending["status"] == "pending"


@pytest.mark.asyncio
async def test_approval_of_expired_request_does_not_enqueue_task(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    request_id = await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    with pytest.raises(ConfirmationExpiredError):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-1",
            approving_interface="web",
        )

    async with DatabaseContext(engine=db_engine) as db:
        request = await db.confirmation_requests.get(request_id)
        tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "pending"
    assert tasks == []


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_approval(db_engine: AsyncEngine) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine)
    execution_task_id = f"confirmation_tool_execution:{request_id}"

    async with DatabaseContext(engine=db_engine) as db:
        await db.tasks.enqueue(
            task_id=execution_task_id,
            task_type="preexisting_task",
            payload={"reason": "force duplicate task id"},
        )

    with pytest.raises(RuntimeError, match="already exists"):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-1",
            approving_interface="web",
        )

    async with DatabaseContext(engine=db_engine) as db:
        request = await db.confirmation_requests.get(request_id)
        tasks = await db.tasks.get_all()

    assert request is not None
    assert request["status"] == "pending"
    assert request.get("execution_task_id") is None
    assert [task["task_id"] for task in tasks] == [execution_task_id]
