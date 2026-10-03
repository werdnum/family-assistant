"""Tests for turn leases: arming, heartbeat, release, hand-off, and resume decisions.

See docs/design/turn-resumption-across-restarts.md.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.turn_resumption import (
    INTERRUPTED_TURN_MARKER,
    TURN_RESUME_TASK_TYPE,
    ResumeDecision,
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage.database import Database
from family_assistant.storage.types import TaskDict
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.helpers import wait_for_condition

LEASE_SECONDS = 120.0
INTERFACE = "web"
PROFILE = "default_assistant"
USER = "test_user"


def _payload(
    conversation_id: str, turn_id: str, *, attempt: int = 0, resumer: str = "fake"
) -> TurnResumePayload:
    return TurnResumePayload(
        resumer=resumer,
        interface_type=INTERFACE,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=USER,
        user_name="Test User",
        processing_profile_id=PROFILE,
        attempt=attempt,
    )


def _user(content: str) -> UserMessage:
    empty = TurnTaintState.empty().to_metadata()
    return UserMessage(
        content=content, taint_metadata=empty, authorship_taint_metadata=empty
    )


def _ids() -> tuple[str, str]:
    return f"conv_{uuid.uuid4().hex[:8]}", str(uuid.uuid4())


def _as_utc(value: datetime | None) -> datetime:
    assert value is not None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _leases(db: Database) -> list[TaskDict]:
    return await db.tasks.get_all(task_type=TURN_RESUME_TASK_TYPE)


async def _add_row(
    db: Database,
    message: UserMessage | AssistantMessage,
    *,
    conversation_id: str,
    turn_id: str,
) -> int | None:
    return await db.message_history.add_message(
        message,
        interface_type=INTERFACE,
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id=USER,
        processing_profile_id=PROFILE,
    )


async def _seed_interrupted_turn(
    db: Database, conversation_id: str, turn_id: str
) -> int:
    """A prompt plus a tool-calling assistant row whose result never landed."""
    user_row_id = await _add_row(
        db, _user("check my notes"), conversation_id=conversation_id, turn_id=turn_id
    )
    await _add_row(
        db,
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
        ),
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    assert user_row_id is not None
    return user_row_id


def _exec_context(db: Database) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="unknown",
        conversation_id="unknown",
        user_name="task_worker",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


class _RecordingResumer:
    def __init__(self, *, launched: bool = True) -> None:
        self.calls: list[TurnResumePayload] = []
        self.deliveries: list[TurnResumePayload] = []
        self._launched = launched

    async def resume(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> bool:
        self.calls.append(payload)
        return self._launched

    async def deliver_pending_reply(self, payload: TurnResumePayload) -> None:
        self.deliveries.append(payload)


def _parked_turn(release: asyncio.Event) -> "asyncio.Task[None]":
    async def turn() -> None:
        await release.wait()

    return asyncio.create_task(turn())


# --------------------------------------------------------------------------- #
# Lease lifecycle
# --------------------------------------------------------------------------- #


async def test_arm_writes_pending_lease_due_after_lease_period(
    db_engine: AsyncEngine,
) -> None:
    clock = MockClock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    registry = TurnLeaseRegistry(clock=clock, lease_seconds=LEASE_SECONDS)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()

    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))

    (row,) = await _leases(db)
    assert row["task_id"] == lease.task_id
    assert row["status"] == "pending"
    assert _as_utc(row["scheduled_at"]) == clock.now() + timedelta(
        seconds=LEASE_SECONDS
    )
    assert TurnResumePayload.model_validate(row["payload"]) == lease.payload


async def test_heartbeat_pushes_live_leases_forward(db_engine: AsyncEngine) -> None:
    clock = MockClock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    registry = TurnLeaseRegistry(clock=clock, lease_seconds=LEASE_SECONDS)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    release = asyncio.Event()
    task = _parked_turn(release)
    registry.track(lease, task, database=db, request_suspend=release.set)
    clock.advance(timedelta(seconds=90))

    await registry.extend_all(db)

    (row,) = await _leases(db)
    assert _as_utc(row["scheduled_at"]) == clock.now() + timedelta(
        seconds=LEASE_SECONDS
    )
    release.set()
    await task


async def test_turn_that_finishes_releases_its_lease(db_engine: AsyncEngine) -> None:
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    release = asyncio.Event()
    task = _parked_turn(release)
    registry.track(lease, task, database=db, request_suspend=release.set)

    release.set()
    await task

    async def lease_gone() -> bool:
        return not await _leases(db)

    await wait_for_condition(lease_gone, description="lease deleted")
    assert not registry.is_live(turn_id)


async def test_released_lease_is_deleted_while_its_task_runs_on(
    db_engine: AsyncEngine,
) -> None:
    """A path whose task outlives the turn releases the lease when the turn
    ends, not when the task does."""
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    release = asyncio.Event()
    task = _parked_turn(release)
    registry.track(lease, task, database=db, request_suspend=release.set)

    await registry.release(lease)

    assert await _leases(db) == []
    release.set()
    await task


async def test_releasing_a_suspended_turn_keeps_its_lease(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    release = asyncio.Event()
    task = _parked_turn(release)
    registry.track(lease, task, database=db, request_suspend=release.set)
    await registry.suspend_all(grace_seconds=5.0)

    await registry.release(lease)

    assert len(await _leases(db)) == 1


async def test_suspended_turn_lease_is_handed_off_due_now(
    db_engine: AsyncEngine,
) -> None:
    clock = MockClock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    registry = TurnLeaseRegistry(clock=clock, lease_seconds=LEASE_SECONDS)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    release = asyncio.Event()
    registry.track(
        lease, _parked_turn(release), database=db, request_suspend=release.set
    )
    await registry.suspend_all(grace_seconds=5.0)

    await registry.hand_off(db)

    (row,) = await _leases(db)
    assert row["status"] == "pending"
    assert _as_utc(row["scheduled_at"]) == clock.now()


async def test_suspend_cancels_turns_that_miss_the_grace_window(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    never = asyncio.Event()
    task = _parked_turn(never)
    registry.track(lease, task, database=db, request_suspend=lambda: None)

    await registry.suspend_all(grace_seconds=0.05, teardown_seconds=1.0)

    assert task.cancelled()


async def test_turn_tracked_during_shutdown_is_suspended_at_once(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    await registry.suspend_all()
    conversation_id, turn_id = _ids()
    lease = await registry.arm(db.tasks, _payload(conversation_id, turn_id))
    suspended = asyncio.Event()
    task = _parked_turn(suspended)

    registry.track(lease, task, database=db, request_suspend=suspended.set)

    assert suspended.is_set()
    await task


# --------------------------------------------------------------------------- #
# Resume decisions
# --------------------------------------------------------------------------- #


async def test_decide_resumes_an_interrupted_turn(db_engine: AsyncEngine) -> None:
    registry = TurnLeaseRegistry()
    registry.register_resumer("fake", _RecordingResumer())
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)

    decision = await registry.decide(db, _payload(conversation_id, turn_id))

    assert decision is ResumeDecision.RESUME


async def test_decide_skips_a_turn_with_a_terminal_reply(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    registry.register_resumer("fake", _RecordingResumer())
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)
    await _add_row(
        db,
        AssistantMessage(content="all done"),
        conversation_id=conversation_id,
        turn_id=turn_id,
    )

    decision = await registry.decide(db, _payload(conversation_id, turn_id))

    assert decision is ResumeDecision.FINISHED


async def test_decide_skips_a_turn_the_conversation_has_moved_past(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    registry.register_resumer("fake", _RecordingResumer())
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)
    await _add_row(
        db,
        _user("never mind, something else"),
        conversation_id=conversation_id,
        turn_id=str(uuid.uuid4()),
    )

    decision = await registry.decide(db, _payload(conversation_id, turn_id))

    assert decision is ResumeDecision.SUPERSEDED


async def test_decide_leaves_a_turn_waiting_on_confirmation(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    registry.register_resumer("fake", _RecordingResumer())
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    user_row_id = await _seed_interrupted_turn(db, conversation_id, turn_id)
    await db.confirmation_requests.create(
        request_id=str(uuid.uuid4()),
        target_user_id=USER,
        tool_name="list_notes",
        tool_args={},
        tool_call_id="call_1",
        source_message_internal_id=user_row_id,
        confirmation_prompt="Allow?",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    decision = await registry.decide(db, _payload(conversation_id, turn_id))

    assert decision is ResumeDecision.AWAITING_CONFIRMATION


async def test_decide_gives_up_after_max_attempts(db_engine: AsyncEngine) -> None:
    registry = TurnLeaseRegistry(max_resume_attempts=3)
    registry.register_resumer("fake", _RecordingResumer())
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)

    decision = await registry.decide(db, _payload(conversation_id, turn_id, attempt=3))

    assert decision is ResumeDecision.EXHAUSTED


async def test_decide_reports_an_unregistered_resumer(db_engine: AsyncEngine) -> None:
    registry = TurnLeaseRegistry()
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)

    decision = await registry.decide(db, _payload(conversation_id, turn_id))

    assert decision is ResumeDecision.NO_RESUMER


# --------------------------------------------------------------------------- #
# The task handler
# --------------------------------------------------------------------------- #


async def test_handler_relaunches_through_the_registered_resumer(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    resumer = _RecordingResumer()
    registry.register_resumer("fake", resumer)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)
    payload = _payload(conversation_id, turn_id)

    await registry.handle_resume_task(
        _exec_context(db), payload.model_dump(mode="json")
    )

    assert resumer.calls == [payload]


async def test_handler_closes_an_exhausted_turn_with_a_marker(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry(max_resume_attempts=1)
    resumer = _RecordingResumer()
    registry.register_resumer("fake", resumer)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)

    await registry.handle_resume_task(
        _exec_context(db),
        _payload(conversation_id, turn_id, attempt=1).model_dump(mode="json"),
    )

    rows = await db.message_history.get_by_turn_id(turn_id)
    assert isinstance(rows[-1], AssistantMessage)
    assert rows[-1].content == INTERRUPTED_TURN_MARKER
    assert not resumer.calls


async def test_handler_rearms_a_lease_for_a_turn_still_running_here(
    db_engine: AsyncEngine,
) -> None:
    registry = TurnLeaseRegistry()
    resumer = _RecordingResumer()
    registry.register_resumer("fake", resumer)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)
    payload = _payload(conversation_id, turn_id)
    lease = await registry.arm(db.tasks, payload)
    release = asyncio.Event()
    task = _parked_turn(release)
    registry.track(lease, task, database=db, request_suspend=release.set)
    # The worker claimed the lapsed lease before running the handler.
    await db.tasks.update_status(lease.task_id, "processing")

    await registry.handle_resume_task(
        _exec_context(db), payload.model_dump(mode="json")
    )

    pending = [row for row in await _leases(db) if row["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["task_id"] != lease.task_id
    assert not resumer.calls
    release.set()
    await task


async def test_handler_hands_a_finished_turn_to_its_resumer_for_delivery(
    db_engine: AsyncEngine,
) -> None:
    """A turn with a terminal reply is not run again, but its resumer gets the
    chance to send a reply that never went out."""
    registry = TurnLeaseRegistry()
    resumer = _RecordingResumer()
    registry.register_resumer("fake", resumer)
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_interrupted_turn(db, conversation_id, turn_id)
    await _add_row(
        db,
        AssistantMessage(content="all done"),
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    payload = _payload(conversation_id, turn_id)

    await registry.handle_resume_task(
        _exec_context(db), payload.model_dump(mode="json")
    )

    assert (resumer.calls, resumer.deliveries) == ([], [payload])
