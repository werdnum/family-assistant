"""Functional tests for durable tool confirmations."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.confirmation_service import (
    CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    ConfirmationAlreadyResolvedError,
    ConfirmationAuthorizationError,
    ConfirmationExpiredError,
    ConfirmationService,
)
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.database import (
    Database,
    DatabaseTransaction,
    spawn_detached,
)
from family_assistant.storage.repositories.confirmation_requests import (
    ConfirmationRequestRow,
    ConfirmationRequestsRepository,
)
from family_assistant.tools.types import (
    ToolArgumentsView,
    ToolCallReviewAuthorization,
)

RaceMode = Literal[
    "reject_before_approve",
    "approve_before_reject",
    "expire_before_approve",
    "expire_before_reject",
]


def _service(db_engine: AsyncEngine) -> ConfirmationService:
    return ConfirmationService(db=Database(engine=db_engine))


def _racing_service(db_engine: AsyncEngine, race_mode: RaceMode) -> ConfirmationService:
    return ConfirmationService(db=_RacingDatabase(db_engine, race_mode))


class _RacingConfirmationRequestsRepository:
    def __init__(
        self,
        inner: ConfirmationRequestsRepository,
        db_engine: AsyncEngine,
        race_mode: RaceMode,
    ) -> None:
        self._inner = inner
        self._db_engine = db_engine
        self._race_mode = race_mode

    async def create(
        self,
        *,
        request_id: str,
        target_user_id: str,
        tool_name: str,
        tool_args: ToolArgumentsView,
        tool_call_id: str | None,
        source_message_internal_id: int | None,
        confirmation_prompt: str,
        expires_at: datetime,
    ) -> ConfirmationRequestRow:
        return await self._inner.create(
            request_id=request_id,
            target_user_id=target_user_id,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call_id=tool_call_id,
            source_message_internal_id=source_message_internal_id,
            confirmation_prompt=confirmation_prompt,
            expires_at=expires_at,
        )

    async def get(self, request_id: str) -> ConfirmationRequestRow | None:
        return await self._inner.get(request_id)

    async def list_pending_for_user(self, user_id: str) -> list[ConfirmationRequestRow]:
        return await self._inner.list_pending_for_user(user_id)

    async def approve_pending(
        self,
        *,
        request_id: str,
        resolving_user_id: str,
        resolving_interface: str,
        execution_task_id: str | None,
        now: datetime,
    ) -> ConfirmationRequestRow | None:
        if self._race_mode == "reject_before_approve":
            # A different actor entirely, so it must not inherit the approval's
            # transaction -- that is what spawn_detached is for.
            racing_db = Database(engine=self._db_engine)
            await spawn_detached(
                racing_db.confirmation_requests.reject_pending(
                    request_id=request_id,
                    resolving_user_id="racing-user",
                    resolving_interface="telegram",
                    now=now,
                )
            )
        if self._race_mode == "expire_before_approve":
            await self._expire_request(request_id=request_id, now=now)
        return await self._inner.approve_pending(
            request_id=request_id,
            resolving_user_id=resolving_user_id,
            resolving_interface=resolving_interface,
            execution_task_id=execution_task_id,
            now=now,
        )

    async def reject_pending(
        self,
        *,
        request_id: str,
        resolving_user_id: str,
        resolving_interface: str,
        now: datetime,
    ) -> ConfirmationRequestRow | None:
        if self._race_mode == "approve_before_reject":
            execution_task_id = f"confirmation_tool_execution:{request_id}"
            racing_db = Database(engine=self._db_engine)
            approved = await racing_db.confirmation_requests.approve_pending(
                request_id=request_id,
                resolving_user_id="racing-user",
                resolving_interface="web",
                execution_task_id=execution_task_id,
                now=now,
            )
            if approved is not None:
                await racing_db.tasks.enqueue(
                    task_id=execution_task_id,
                    task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
                    payload={"confirmation_request_id": request_id},
                    original_task_id=execution_task_id,
                )
        if self._race_mode == "expire_before_reject":
            await self._expire_request(request_id=request_id, now=now)
        return await self._inner.reject_pending(
            request_id=request_id,
            resolving_user_id=resolving_user_id,
            resolving_interface=resolving_interface,
            now=now,
        )

    async def mark_expired(self, *, now: datetime) -> int:
        return await self._inner.mark_expired(now=now)

    async def _expire_request(self, *, request_id: str, now: datetime) -> None:
        # Expiry arrives from outside the approval, so it runs detached.
        racing_db = Database(engine=self._db_engine)
        await spawn_detached(
            racing_db.execute(
                update(confirmation_requests_table)
                .where(confirmation_requests_table.c.id == request_id)
                .values(
                    expires_at=now - timedelta(microseconds=1),
                    updated_at=now,
                )
            )
        )


class _RacingTransaction(DatabaseTransaction):
    """A transaction whose confirmation-request writes race a concurrent resolver."""

    def __init__(
        self, database: Database, db_engine: AsyncEngine, race_mode: RaceMode
    ) -> None:
        super().__init__(database)
        self._racing = _RacingConfirmationRequestsRepository(
            super().confirmation_requests, db_engine, race_mode
        )

    @property
    def confirmation_requests(self) -> _RacingConfirmationRequestsRepository:  # type: ignore[override] # deliberately narrows to the racing double
        return self._racing


class _RacingDatabase(Database):
    """A handle whose transactions race a concurrent resolver."""

    def __init__(self, db_engine: AsyncEngine, race_mode: RaceMode) -> None:
        super().__init__(engine=db_engine)
        self._db_engine = db_engine
        self._race_mode: RaceMode = race_mode
        self._racing = _RacingConfirmationRequestsRepository(
            super().confirmation_requests, db_engine, race_mode
        )

    def transaction(self) -> _RacingTransaction:
        return _RacingTransaction(self, self._db_engine, self._race_mode)

    @property
    def confirmation_requests(self) -> _RacingConfirmationRequestsRepository:  # type: ignore[override] # deliberately narrows to the racing double
        return self._racing


async def _create_request(
    db_engine: AsyncEngine,
    *,
    target_user_id: str = "user-1",
    expires_at: datetime | None = None,
    decision_only: bool = False,
) -> str:
    request = await _service(db_engine).create_request(
        target_user_id=target_user_id,
        tool_name="calendar.create_event",
        tool_args={"title": "Flight", "start": "2026-05-01T09:00:00-07:00"},
        tool_call_id="call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Flight",
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
        decision_only=decision_only,
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

    db = Database(engine=db_engine)
    fetched = await db.confirmation_requests.get(created["id"])

    assert fetched is not None
    assert fetched["status"] == "pending"
    assert fetched["target_user_id"] == "user-1"
    assert fetched["tool_args_json"]["title"] == "Ticket"


@pytest.mark.asyncio
async def test_create_request_persists_exact_review_authorization_fields(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    tool_args = {"title": "Ticket", "start": "2026-05-01T09:00:00-07:00"}
    created = await service.create_request(
        target_user_id="user-1",
        tool_name="calendar.create_event",
        tool_args=tool_args,
        tool_call_id="reviewed-call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Ticket",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tool_call_review_authorization=ToolCallReviewAuthorization(
            tool_name="calendar.create_event",
            call_id="reviewed-call-1",
            tool_args=dict(tool_args),
            sink_class="artifact_write",
            static_policy_reason="Static policy delegated this call to review.",
            taint_policy_reason="External content reached an artifact write.",
        ),
    )

    fetched = await Database(engine=db_engine).confirmation_requests.get(created["id"])

    assert fetched is not None
    assert fetched["sink_class"] == "artifact_write"
    assert fetched["static_policy_reason"] == (
        "Static policy delegated this call to review."
    )
    assert fetched["taint_policy_reason"] == (
        "External content reached an artifact write."
    )

    mismatched = await service.create_request(
        target_user_id="user-1",
        tool_name="calendar.create_event",
        tool_args=tool_args,
        tool_call_id="different-call",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Ticket",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        tool_call_review_authorization=ToolCallReviewAuthorization(
            tool_name="calendar.create_event",
            call_id="reviewed-call-1",
            tool_args=dict(tool_args),
            sink_class="artifact_write",
            static_policy_reason="Must not persist for a different call.",
            taint_policy_reason=None,
        ),
    )
    mismatched_fetched = await Database(engine=db_engine).confirmation_requests.get(
        mismatched["id"]
    )
    assert mismatched_fetched is not None
    assert mismatched_fetched["sink_class"] is None
    assert mismatched_fetched["static_policy_reason"] is None
    assert mismatched_fetched["taint_policy_reason"] is None


@pytest.mark.asyncio
async def test_invalid_confirmation_status_is_rejected_by_database(
    db_engine: AsyncEngine,
) -> None:
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        db = Database(engine=db_engine)
        await db.execute(
            insert(confirmation_requests_table).values(
                id="confirm_invalid_status",
                target_user_id="user-1",
                status="invalid",
                tool_name="calendar.create_event",
                tool_args_json={"title": "Flight"},
                tool_call_id=None,
                source_message_internal_id=None,
                confirmation_prompt="Create calendar event: Flight",
                expires_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
        )


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

    db = Database(engine=db_engine)
    tasks = await db.tasks.get_all(
        status="pending",
        task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    )

    assert len(tasks) == 1
    assert tasks[0]["task_id"] == expected_task_id
    assert tasks[0]["payload"] == {"confirmation_request_id": request_id}
    assert tasks[0]["max_retries"] == 0


@pytest.mark.asyncio
async def test_decision_only_approval_does_not_enqueue_execution_task(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    request_id = await _create_request(db_engine)

    approved = await service.approve_without_enqueueing_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )

    assert approved["status"] == "approved"
    assert approved["resolved_by_user_id"] == "user-1"
    assert approved["resolved_via_interface"] == "web"
    assert approved["execution_task_id"] is None

    db = Database(engine=db_engine)
    tasks = await db.tasks.get_all(
        task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    )

    assert tasks == []


@pytest.mark.asyncio
async def test_durable_decision_only_approval_does_not_enqueue_execution_task(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    request = await service.create_request(
        target_user_id="user-1",
        tool_name="calendar.create_event",
        tool_args={"title": "Flight", "start": "2026-05-01T09:00:00-07:00"},
        tool_call_id="call-1",
        source_message_internal_id=None,
        confirmation_prompt="Create calendar event: Flight",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        decision_only=True,
    )

    approved = await service.approve_and_enqueue_execution(
        request_id=request["id"],
        approving_user_id="user-1",
        approving_interface="web",
    )

    assert approved["status"] == "approved"
    assert approved["decision_only"] is True
    assert approved["execution_task_id"] is None

    db = Database(engine=db_engine)
    tasks = await db.tasks.get_all(
        task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    )

    assert tasks == []


@pytest.mark.asyncio
async def test_durable_decision_only_flag_suppresses_enqueue(
    db_engine: AsyncEngine,
) -> None:
    """A decision_only request never enqueues execution, even via the enqueue path.

    The decision-only mode is recorded durably on the request, so an approval
    handled by a process/restart that lost the in-memory waiter — and therefore
    falls through to approve_and_enqueue_execution — must still not enqueue a
    confirmation_tool_execution task (which would run the tool outside the
    delegated turn that is resuming inline).
    """
    request_id = await _create_request(db_engine, decision_only=True)

    approved = await _service(db_engine).approve_and_enqueue_execution(
        request_id=request_id,
        approving_user_id="user-1",
        approving_interface="web",
    )

    assert approved["status"] == "approved"
    assert approved["execution_task_id"] is None
    db = Database(engine=db_engine)
    tasks = await db.tasks.get_all(
        task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE,
    )
    assert tasks == []


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
    db = Database(engine=db_engine)
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

    db = Database(engine=db_engine)
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

    db = Database(engine=db_engine)
    tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert tasks == []


@pytest.mark.postgres  # a second concurrent writer; SQLite serializes transaction scopes
@pytest.mark.asyncio
async def test_concurrent_rejection_during_approval_raises(
    db_engine: AsyncEngine,
) -> None:
    service = _racing_service(db_engine, "reject_before_approve")
    request_id = await _create_request(db_engine)

    with pytest.raises(ConfirmationAlreadyResolvedError):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-1",
            approving_interface="web",
        )

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)
    tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "rejected"
    assert tasks == []


@pytest.mark.asyncio
async def test_concurrent_approval_during_rejection_raises(
    db_engine: AsyncEngine,
) -> None:
    service = _racing_service(db_engine, "approve_before_reject")
    request_id = await _create_request(db_engine)

    with pytest.raises(ConfirmationAlreadyResolvedError):
        await service.reject(
            request_id=request_id,
            rejecting_user_id="user-1",
            rejecting_interface="telegram",
        )

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)
    tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "approved"
    assert [task["task_id"] for task in tasks] == [
        f"confirmation_tool_execution:{request_id}"
    ]


@pytest.mark.postgres  # a second concurrent writer; SQLite serializes transaction scopes
@pytest.mark.asyncio
async def test_expiry_during_approval_raises_expired_error(
    db_engine: AsyncEngine,
) -> None:
    service = _racing_service(db_engine, "expire_before_approve")
    request_id = await _create_request(db_engine)

    with pytest.raises(ConfirmationExpiredError):
        await service.approve_and_enqueue_execution(
            request_id=request_id,
            approving_user_id="user-1",
            approving_interface="web",
        )

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)
    tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "pending"
    assert tasks == []


@pytest.mark.asyncio
async def test_expiry_during_rejection_raises_expired_error(
    db_engine: AsyncEngine,
) -> None:
    service = _racing_service(db_engine, "expire_before_reject")
    request_id = await _create_request(db_engine)

    with pytest.raises(ConfirmationExpiredError):
        await service.reject(
            request_id=request_id,
            rejecting_user_id="user-1",
            rejecting_interface="telegram",
        )

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)
    tasks = await db.tasks.get_all(task_type=CONFIRMATION_TOOL_EXECUTION_TASK_TYPE)

    assert request is not None
    assert request["status"] == "pending"
    assert tasks == []


@pytest.mark.asyncio
async def test_list_pending_for_user_excludes_expired_requests(
    db_engine: AsyncEngine,
) -> None:
    service = _service(db_engine)
    await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    pending_id = await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    pending_requests = await service.list_pending_for_user(user_id="user-1")

    assert [request["id"] for request in pending_requests] == [pending_id]


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
    db = Database(engine=db_engine)
    expired = await db.confirmation_requests.get(expired_id)
    pending = await db.confirmation_requests.get(pending_id)

    assert expired is not None
    assert expired["status"] == "expired"
    assert pending is not None
    assert pending["status"] == "pending"


@pytest.mark.asyncio
async def test_mark_expired_rejects_naive_now(db_engine: AsyncEngine) -> None:
    service = _service(db_engine)
    request_id = await _create_request(
        db_engine,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.mark_expired(
            now=datetime(2026, 4, 19, 12, 0, 0),
        )

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)

    assert request is not None
    assert request["status"] == "pending"


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

    db = Database(engine=db_engine)
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

    db = Database(engine=db_engine)
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

    db = Database(engine=db_engine)
    request = await db.confirmation_requests.get(request_id)
    tasks = await db.tasks.get_all()

    assert request is not None
    assert request["status"] == "pending"
    assert request.get("execution_task_id") is None
    assert [task["task_id"] for task in tasks] == [execution_task_id]
