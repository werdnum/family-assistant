"""Resume in-progress turns after the process that ran them goes away.

A running turn holds a *lease*: a ``resume_interrupted_turn`` task scheduled a
short way into the future, whose due time a per-process heartbeat keeps pushing
back. A turn that ends normally deletes its lease. So a lease only comes due
when its process stopped heartbeating -- it crashed (the lease expires on its
own) or it shut down gracefully (it suspended its turns and handed their leases
off by making them due at once). Whichever process claims the due lease decides
from durable history whether the turn should continue, and if so relaunches it
through the resumer registered for the path that started it.

See docs/design/turn-resumption-across-restarts.md.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from family_assistant.llm.messages import AssistantMessage
from family_assistant.security.taint import merge_history_taint
from family_assistant.storage.tasks import TaskPriority
from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.tasks import TasksRepository
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

TURN_RESUME_TASK_TYPE = "resume_interrupted_turn"

# How far ahead a lease is due. Bounds how long after a crash the turn waits
# before another process picks it up, and must comfortably exceed the heartbeat
# interval so a single slow heartbeat doesn't hand a live turn away.
LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 30.0

# How long a graceful shutdown waits for turns to reach a loop boundary before
# cancelling them. Kept well inside Kubernetes' termination grace period.
SUSPEND_GRACE_SECONDS = 15.0
# After cancelling stragglers, how long to let their teardown run.
CANCEL_TEARDOWN_SECONDS = 5.0

# A turn that keeps taking its process down with it would otherwise be resumed
# forever.
MAX_RESUME_ATTEMPTS = 3

INTERRUPTED_TURN_MARKER = (
    "_Interrupted: the assistant restarted while working on this and could not "
    "pick it back up. Ask again if you still need it._"
)


class TurnResumePayload(BaseModel):
    """What relaunching a turn needs that its persisted rows don't record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Which registered resumer relaunches the turn: the path that started it,
    # which the interface type alone does not identify.
    resumer: str
    interface_type: str
    conversation_id: str
    turn_id: str
    user_id: str
    user_name: str
    processing_profile_id: str
    # The admitted model-tier envelope, as ``ResolvedModelSelection.to_json``.
    model_selection: dict[str, str | None] | None = None
    # How many times this turn has been relaunched before this run.
    attempt: int = 0

    def next_attempt(self) -> TurnResumePayload:
        return self.model_copy(update={"attempt": self.attempt + 1})


@dataclass(frozen=True, slots=True)
class TurnLease:
    task_id: str
    payload: TurnResumePayload


class TurnResumer(Protocol):
    """Relaunches the interrupted turns of one launch path."""

    async def resume(
        self, payload: TurnResumePayload, registry: TurnLeaseRegistry
    ) -> bool:
        """Relaunch the turn, arming its next lease through ``registry``.

        Returns False without launching when the conversation already has a
        running turn, which supersedes this one.
        """
        ...

    async def deliver_pending_reply(self, payload: TurnResumePayload) -> None:
        """Finish a turn whose reply was generated but may never have been sent.

        Called when the interrupted turn turns out to have a terminal reply. A
        path whose clients read replies from history has nothing to do; one
        that pushes replies out (a chat bot) sends it if it was not delivered.
        """
        ...


class ResumeDecision(Enum):
    RESUME = "resume"
    UNKNOWN_TURN = "unknown_turn"
    FINISHED = "finished"
    SUPERSEDED = "superseded"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXHAUSTED = "exhausted"
    NO_RESUMER = "no_resumer"


@dataclass(slots=True)
class _TrackedTurn:
    lease: TurnLease
    task: asyncio.Task[None]
    database: Database
    request_suspend: Callable[[], None]
    suspend_requested: bool = False

    def suspend(self) -> None:
        if not self.suspend_requested:
            self.suspend_requested = True
            self.request_suspend()


class TurnLeaseRegistry:
    """Process-wide bookkeeping for the leases of the turns running here."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        lease_seconds: float = LEASE_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        max_resume_attempts: int = MAX_RESUME_ATTEMPTS,
    ) -> None:
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._max_resume_attempts = max_resume_attempts
        self._tracked: dict[str, _TrackedTurn] = {}
        self._resumers: dict[str, TurnResumer] = {}
        self._release_tasks: set[asyncio.Task[None]] = set()
        self._suspending = False

    # ------------------------------------------------------------------ #
    # Lease lifecycle
    # ------------------------------------------------------------------ #

    def register_resumer(self, name: str, resumer: TurnResumer) -> None:
        self._resumers[name] = resumer

    async def arm(
        self, tasks: TasksRepository, payload: TurnResumePayload
    ) -> TurnLease:
        """Write a lease for a turn that is about to run.

        Takes the repository rather than a handle so a caller can arm the lease
        in the same transaction that makes the turn's prompt durable.
        """
        task_id = (
            f"turn_lease:{payload.turn_id}:{payload.attempt}:{uuid.uuid4().hex[:8]}"
        )
        await tasks.enqueue(
            task_id=task_id,
            task_type=TURN_RESUME_TASK_TYPE,
            payload=payload.model_dump(mode="json"),
            scheduled_at=self._lease_expiry(),
            priority=TaskPriority.INTERACTIVE,
        )
        return TurnLease(task_id=task_id, payload=payload)

    def track(
        self,
        lease: TurnLease,
        task: asyncio.Task[None],
        *,
        database: Database,
        request_suspend: Callable[[], None],
    ) -> None:
        """Keep a running turn's lease alive until its task finishes.

        When the task finishes the lease is deleted -- unless the turn was
        suspended, in which case it is kept for :meth:`hand_off`. A turn that
        happened to finish inside the suspension grace window keeps its lease
        too; the resume handler finds its terminal reply and does nothing.
        """
        entry = _TrackedTurn(
            lease=lease,
            task=task,
            database=database,
            request_suspend=request_suspend,
        )
        self._tracked[lease.payload.turn_id] = entry
        if self._suspending:
            entry.suspend()
        task.add_done_callback(lambda _task: self._on_turn_done(entry))

    def is_live(self, turn_id: str) -> bool:
        entry = self._tracked.get(turn_id)
        return entry is not None and not entry.task.done()

    def _on_turn_done(self, entry: _TrackedTurn) -> None:
        if entry.suspend_requested or not self._untrack(entry):
            return
        release = asyncio.ensure_future(self._release(entry))
        self._release_tasks.add(release)
        release.add_done_callback(self._release_tasks.discard)

    def _untrack(self, entry: _TrackedTurn) -> bool:
        """Stop tracking ``entry``; False if it was no longer tracked."""
        turn_id = entry.lease.payload.turn_id
        if self._tracked.get(turn_id) is not entry:
            return False
        del self._tracked[turn_id]
        return True

    async def release(self, lease: TurnLease) -> None:
        """Delete the lease of a turn that has ended, ahead of its task ending.

        For a launch path whose task outlives the turn -- one that goes on to
        deliver the reply, or to run another turn. A suspended turn keeps its
        lease for the hand-off, as it would when its task ended.
        """
        entry = self._tracked.get(lease.payload.turn_id)
        if entry is None or entry.suspend_requested or not self._untrack(entry):
            return
        await self._release(entry)

    @staticmethod
    async def _release(entry: _TrackedTurn) -> None:
        # A failed delete leaves the lease to come due; the resume handler then
        # finds the turn's terminal reply and does nothing, so this degrades to
        # one wasted task run rather than a resumed turn.
        try:
            await entry.database.tasks.delete_pending(entry.lease.task_id)
        except Exception:
            logger.warning(
                "Failed to release lease %s for turn %s",
                entry.lease.task_id,
                entry.lease.payload.turn_id,
                exc_info=True,
            )

    def _lease_expiry(self) -> datetime:
        return self._clock.now() + timedelta(seconds=self._lease_seconds)

    async def extend_all(self, db: Database) -> int:
        """Push back the due time of every lease held by a live turn here."""
        task_ids = [
            entry.lease.task_id
            for entry in self._tracked.values()
            if not entry.task.done()
        ]
        return await db.tasks.reschedule_pending(
            task_ids, self._clock.now() + timedelta(seconds=self._lease_seconds)
        )

    async def run_heartbeat(self, db: Database) -> None:
        """Extend live leases every heartbeat interval until cancelled."""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                await self.extend_all(db)
            except Exception:
                # A missed beat is absorbed by the lease margin; stopping the
                # loop would hand every live turn away a lease period later.
                logger.exception("Turn lease heartbeat failed")

    # ------------------------------------------------------------------ #
    # Graceful shutdown
    # ------------------------------------------------------------------ #

    async def suspend_all(
        self,
        grace_seconds: float = SUSPEND_GRACE_SECONDS,
        teardown_seconds: float = CANCEL_TEARDOWN_SECONDS,
    ) -> None:
        """Stop every turn here at a safe point, keeping its lease.

        Turns are asked to stop at their next loop boundary, which lets a
        running tool finish and record its result; whatever is still running
        after ``grace_seconds`` is cancelled. Turns registered after this call
        are asked to suspend as soon as they are tracked. Idempotent.
        """
        self._suspending = True
        entries = list(self._tracked.values())
        for entry in entries:
            entry.suspend()
        running = [entry.task for entry in entries if not entry.task.done()]
        if not running:
            return
        logger.info("Suspending %d in-progress turn(s) for shutdown", len(running))
        _done, pending = await asyncio.wait(running, timeout=grace_seconds)
        if not pending:
            return
        logger.warning(
            "Cancelling %d turn(s) still running after the %.0fs suspension grace",
            len(pending),
            grace_seconds,
        )
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=teardown_seconds)

    async def hand_off(self, db: Database) -> int:
        """Make the leases of suspended turns due now, so another process
        resumes them without waiting for them to expire.

        Must run after this process's task workers have stopped, or one of them
        could claim a lease and be cancelled along with it.
        """
        if self._release_tasks:
            await asyncio.gather(*self._release_tasks, return_exceptions=True)
        task_ids = [entry.lease.task_id for entry in self._tracked.values()]
        self._tracked.clear()
        if not task_ids:
            return 0
        moved = await db.tasks.reschedule_pending(task_ids, self._clock.now())
        logger.info("Handed off %d suspended turn lease(s)", moved)
        return moved

    # ------------------------------------------------------------------ #
    # Resuming
    # ------------------------------------------------------------------ #

    async def decide(self, db: Database, payload: TurnResumePayload) -> ResumeDecision:
        """Decide from durable state whether an interrupted turn should run on."""
        history = db.message_history
        user_row = await history.get_user_row_by_turn_id(payload.turn_id)
        if user_row is None:
            return ResumeDecision.UNKNOWN_TURN
        if await history.has_terminal_reply_for_turn(payload.turn_id):
            return ResumeDecision.FINISHED
        if await history.conversation_moved_past_turn(
            interface_type=payload.interface_type,
            conversation_id=payload.conversation_id,
            turn_id=payload.turn_id,
        ):
            return ResumeDecision.SUPERSEDED
        pending_confirmations = await db.confirmation_requests.list_pending_for_user(
            payload.user_id
        )
        if any(
            confirmation["source_message_internal_id"] == user_row["internal_id"]
            for confirmation in pending_confirmations
        ):
            return ResumeDecision.AWAITING_CONFIRMATION
        if payload.attempt >= self._max_resume_attempts:
            return ResumeDecision.EXHAUSTED
        if payload.resumer not in self._resumers:
            return ResumeDecision.NO_RESUMER
        return ResumeDecision.RESUME

    async def handle_resume_task(
        self, exec_context: ToolExecutionContext, raw_payload: object
    ) -> None:
        """Task handler for a lease that came due."""
        payload = TurnResumePayload.model_validate(raw_payload)
        db = exec_context.db_context
        live = self._tracked.get(payload.turn_id)
        if live is not None and not live.task.done():
            # The heartbeat lapsed (a stalled loop, a failed beat) but the turn
            # is still running here: keep it, and give it a fresh lease since
            # this one has just been claimed.
            live.lease = await self.arm(db.tasks, payload)
            logger.warning(
                "Lease for turn %s came due while the turn was still running; re-armed",
                payload.turn_id,
            )
            return

        decision = await self.decide(db, payload)
        logger.info(
            "Interrupted turn %s (conversation %s, attempt %d): %s",
            payload.turn_id,
            payload.conversation_id,
            payload.attempt,
            decision.value,
        )
        if decision in {ResumeDecision.EXHAUSTED, ResumeDecision.NO_RESUMER}:
            await persist_interrupted_marker(db, payload)
            return
        if decision is ResumeDecision.FINISHED and payload.resumer in self._resumers:
            await self._resumers[payload.resumer].deliver_pending_reply(payload)
            return
        if decision is not ResumeDecision.RESUME:
            return

        try:
            launched = await self._resumers[payload.resumer].resume(payload, self)
        except Exception:
            if exec_context.task_attempt is None or exec_context.task_attempt.is_final:
                await persist_interrupted_marker(db, payload)
            raise
        if not launched:
            logger.info(
                "Not resuming turn %s: conversation %s already has a running turn",
                payload.turn_id,
                payload.conversation_id,
            )


async def persist_interrupted_marker(db: Database, payload: TurnResumePayload) -> None:
    """Close a turn that will not be resumed, so it doesn't read as still pending."""
    turn_rows = await db.message_history.get_by_turn_id(payload.turn_id)
    await db.message_history.add_message(
        AssistantMessage(
            content=INTERRUPTED_TURN_MARKER,
            taint_metadata=merge_history_taint(turn_rows).to_metadata(),
        ),
        interface_type=payload.interface_type,
        conversation_id=payload.conversation_id,
        timestamp=datetime.now(UTC),
        turn_id=payload.turn_id,
        user_id=payload.user_id,
        processing_profile_id=payload.processing_profile_id,
    )
