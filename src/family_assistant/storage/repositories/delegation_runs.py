"""Repository for asynchronous profile delegation runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NotRequired, Required, TypedDict, cast

from sqlalchemy import insert, or_, select, update
from sqlalchemy.sql import functions as func

from family_assistant.storage.delegation_runs import (
    RECONCILABLE_FAILURE_KINDS,
    TERMINAL_DELEGATION_STATUSES,
    DelegationLocalFailureKind,
    DelegationNotifyStage,
    DelegationRunStatus,
    delegation_runs_table,
)
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.processing.protocol import RemoteObservationMetadata
    from family_assistant.security.taint import TaintMetadata

__all__ = [
    "TERMINAL_DELEGATION_STATUSES",
    "DelegationLocalFailureKind",
    "DelegationRunCreate",
    "DelegationRunDict",
    "DelegationRunStatus",
    "DelegationRunSummary",
    "DelegationRunsRepository",
]


class DelegationRunCreate(TypedDict, total=False):
    """Input fields for creating a delegation run."""

    delegation_id: Required[str]
    task_id: Required[str]
    source_profile_id: Required[str]
    target_service_id: Required[str]
    interface_type: Required[str]
    conversation_id: Required[str]
    subconversation_id: Required[str]
    request_text: Required[str]
    content_parts_json: Required[list[ContentPartDict]]
    taint_state_json: TaintMetadata | None
    model_selection_json: dict[str, str | None] | None
    user_id: str | None
    user_name: str | None
    source_turn_id: str | None
    source_subconversation_id: str | None


class DelegationRunDict(TypedDict):
    """Delegation run row normalized from the database."""

    id: int
    delegation_id: str
    task_id: str
    status: DelegationRunStatus
    source_profile_id: str
    target_service_id: str
    interface_type: str
    conversation_id: str
    user_id: str | None
    user_name: str | None
    source_turn_id: str | None
    source_subconversation_id: str | None
    subconversation_id: str
    request_text: str
    content_parts_json: list[ContentPartDict]
    taint_state_json: TaintMetadata | None
    # Whatever the column decoded to, uninterpreted:
    # `ResolvedModelSelection.from_json` is what decides whether it is an
    # envelope, and it raises when it is not. Narrowing it here would let a
    # malformed one read as "no selection" and run at the default tier.
    model_selection_json: object | None
    handed_off_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    result_text: str | None
    result_attachment_ids_json: list[str] | None
    result_message_internal_id: int | None
    error: str | None
    notified_at: datetime | None
    notify_stage: DelegationNotifyStage
    notify_attempts: int
    notify_error: str | None
    notify_first_failed_at: datetime | None
    notify_last_failed_at: datetime | None
    remote_task_id: str | None
    remote_context_id: str | None
    poll_attempts: int
    local_failure_kind: DelegationLocalFailureKind | None
    cancel_requested_at: datetime | None
    cancel_confirmed_at: datetime | None
    remote_status: str | None
    remote_observed_at: datetime | None
    remote_observation_json: RemoteObservationMetadata | None
    reconcile_attempts: int
    reconciled_at: datetime | None
    late_recovered_at: datetime | None
    created_at: datetime


class DelegationRunSummary(TypedDict):
    """Compact delegation run summary returned by status tools."""

    delegation_id: str
    status: DelegationRunStatus
    target_service_id: str
    source_profile_id: str
    interface_type: str
    conversation_id: str
    request_text: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    handed_off_at: str | None
    result_text: NotRequired[str | None]
    error: NotRequired[str | None]


class DelegationRunsRepository(BaseRepository):
    """Repository for managing asynchronous delegation runs."""

    async def create_run(self, run: DelegationRunCreate) -> DelegationRunDict:
        """Create a new delegation run."""
        now = datetime.now(UTC)
        values = dict(run)
        values.update(status="queued", created_at=now, updated_at=now)
        stmt = (
            insert(delegation_runs_table)
            .values(**values)
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("create_delegation_run", stmt)
        row = result.one()
        return self._row_to_dict(dict(row))

    async def get_by_delegation_id(
        self, delegation_id: str
    ) -> DelegationRunDict | None:
        """Return a delegation run by public reference ID."""
        stmt = select(delegation_runs_table).where(
            delegation_runs_table.c.delegation_id == delegation_id
        )
        row = await self._db.fetch_one(stmt)
        if row is None:
            return None
        return self._row_to_dict(row)

    async def list_for_conversation(
        self,
        *,
        conversation_id: str,
        interface_type: str | None = None,
        status: str | None = None,
        limit: int = 10,
    ) -> list[DelegationRunDict]:
        """List recent delegation runs for a conversation."""
        bounded_limit = min(max(limit, 1), 50)
        stmt = (
            select(delegation_runs_table)
            .where(delegation_runs_table.c.conversation_id == conversation_id)
            .order_by(delegation_runs_table.c.created_at.desc())
            .limit(bounded_limit)
        )
        if interface_type is not None:
            stmt = stmt.where(delegation_runs_table.c.interface_type == interface_type)
        if status is not None:
            stmt = stmt.where(delegation_runs_table.c.status == status)

        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

    async def has_active_run_for_subconversation(
        self,
        *,
        conversation_id: str,
        subconversation_id: str,
    ) -> bool:
        """Return whether a non-terminal run already targets this subconversation.

        Used to reject a resume when another run is already executing (or queued)
        against the same delegated history, so two resumed runs cannot interleave
        messages and tool side effects in one subconversation.
        """
        stmt = (
            select(delegation_runs_table.c.id)
            .where(delegation_runs_table.c.conversation_id == conversation_id)
            .where(delegation_runs_table.c.subconversation_id == subconversation_id)
            .where(
                delegation_runs_table.c.status.notin_(
                    list(TERMINAL_DELEGATION_STATUSES)
                )
            )
            .limit(1)
        )
        row = await self._db.fetch_one(stmt)
        return row is not None

    async def get_latest_completed_run(
        self,
        *,
        conversation_id: str,
        subconversation_id: str,
        target_service_id: str,
    ) -> DelegationRunDict | None:
        """Return the most recent ``completed`` run for this delegated lineage.

        Used by a pollable local target (e.g. Deep Research) to chain a
        resumed delegation onto its prior run's remote state: a resume reuses
        the same ``subconversation_id`` (see ``_resolve_resume_subconversation``
        in ``tools/services.py``), and at most one lineage can occupy a given
        subconversation at a time, so this is unambiguous. Only ``completed``
        (not ``failed``) runs are eligible — chaining from a failed run's
        remote state doesn't make sense.
        """
        stmt = (
            select(delegation_runs_table)
            .where(delegation_runs_table.c.conversation_id == conversation_id)
            .where(delegation_runs_table.c.subconversation_id == subconversation_id)
            .where(delegation_runs_table.c.target_service_id == target_service_id)
            .where(delegation_runs_table.c.status == "completed")
            .order_by(delegation_runs_table.c.created_at.desc())
            .limit(1)
        )
        row = await self._db.fetch_one(stmt)
        if row is None:
            return None
        return self._row_to_dict(row)

    async def mark_running(
        self, delegation_id: str, started_at: datetime
    ) -> DelegationRunDict | None:
        """Mark a ``queued`` delegation run as running and return the updated row.

        Conditioned on the row still being ``queued`` so a run the stale-run
        reaper already failed — or one a sibling worker already claimed — is not
        resurrected to ``running`` and re-executed. Returns ``None`` when the row
        is no longer ``queued`` (already running/terminal) or is absent.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(delegation_runs_table.c.status == "queued")
            .values(
                status="running",
                started_at=started_at,
                updated_at=datetime.now(UTC),
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("mark_delegation_run_running", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def mark_awaiting_remote(
        self,
        delegation_id: str,
        *,
        remote_task_id: str | None,
        remote_context_id: str | None,
        started_at: datetime,
    ) -> DelegationRunDict | None:
        """Claim a ``queued`` run as ``awaiting_remote`` and store remote IDs.

        The submit-then-poll A2A path calls this BEFORE submitting (so the
        ``awaiting_remote`` retry-guard prevents a duplicate concurrent submit
        and the wall-clock cap starts), with ``remote_task_id=None`` because the
        remote assigns the id and the caller only learns it from the submit
        response (then reconciles it via :meth:`update_remote_task`). A run left
        ``awaiting_remote`` with a NULL id — a submit whose response was lost —
        is recovered by re-submitting on the next poll. Conditioned on the row
        still being ``queued`` so a reaped or sibling-claimed run is not
        resurrected. Returns ``None`` when the row is no longer ``queued`` or is
        absent.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(delegation_runs_table.c.status == "queued")
            .values(
                status="awaiting_remote",
                remote_task_id=remote_task_id,
                remote_context_id=remote_context_id,
                started_at=started_at,
                updated_at=datetime.now(UTC),
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging(
            "mark_delegation_run_awaiting_remote", stmt
        )
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def update_remote_task(
        self,
        delegation_id: str,
        *,
        remote_task_id: str,
        remote_context_id: str | None,
    ) -> DelegationRunDict | None:
        """Record the remote-assigned task id once the submit response is known.

        The submit path claims ``awaiting_remote`` with a NULL id, then calls
        this with the id the remote assigned so polling and cancellation target
        the real task.
        """
        return await self._update_run(
            delegation_id,
            remote_task_id=remote_task_id,
            remote_context_id=remote_context_id,
        )

    async def bump_poll_attempt(self, delegation_id: str, now: datetime) -> int | None:
        """Increment and return the poll attempt counter for a run.

        Returns the new count, or ``None`` if the run is absent.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(
                poll_attempts=delegation_runs_table.c.poll_attempts + 1,
                updated_at=now,
            )
            .returning(delegation_runs_table.c.poll_attempts)
        )
        result = await self._execute_with_logging("bump_delegation_poll_attempt", stmt)
        attempts = result.scalar_one_or_none()
        return int(attempts) if attempts is not None else None

    async def list_awaiting_remote(
        self, *, limit: int = 100
    ) -> list[DelegationRunDict]:
        """Return runs in ``awaiting_remote`` state, oldest first.

        Backstop for the recurring sweep that re-enqueues lost poll tasks.
        """
        bounded_limit = min(max(limit, 1), 500)
        stmt = (
            select(delegation_runs_table)
            .where(delegation_runs_table.c.status == "awaiting_remote")
            .order_by(delegation_runs_table.c.created_at.asc())
            .limit(bounded_limit)
        )
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

    async def mark_handed_off(
        self, delegation_id: str, handed_off_at: datetime
    ) -> bool:
        """Atomically claim the async handoff for a non-terminal run.

        Returns ``True`` if this caller won the handoff (the worker will deliver
        the terminal result via notification). Returns ``False`` if the run is
        already terminal or already handed off, in which case the caller should
        deliver the result inline.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(delegation_runs_table.c.handed_off_at.is_(None))
            .where(
                delegation_runs_table.c.status.notin_(
                    list(TERMINAL_DELEGATION_STATUSES)
                )
            )
            .values(handed_off_at=handed_off_at, updated_at=datetime.now(UTC))
        )
        result = await self._execute_with_logging("mark_delegation_handed_off", stmt)
        return result.rowcount > 0  # type: ignore[union-attr]

    async def mark_completed(
        self,
        *,
        delegation_id: str,
        result_text: str | None,
        result_attachment_ids: list[str],
        completed_at: datetime,
    ) -> DelegationRunDict | None:
        """Mark a non-terminal delegation run completed (atomic CAS).

        Conditioned on the run still being non-terminal so a concurrent finalizer
        (the cleanup reaper, or a racing poll) cannot be overwritten and an
        already-terminal run cannot be resurrected. Returns the updated row when
        this caller won the transition, else ``None``.
        """
        return await self._terminate(
            delegation_id,
            status="completed",
            result_text=result_text,
            result_attachment_ids_json=result_attachment_ids,
            completed_at=completed_at,
        )

    async def mark_failed(
        self,
        *,
        delegation_id: str,
        error: str,
        completed_at: datetime,
        local_failure_kind: DelegationLocalFailureKind | None = None,
    ) -> DelegationRunDict | None:
        """Mark a non-terminal delegation run failed (atomic CAS).

        Conditioned on the run still being non-terminal (see ``mark_completed``).
        Returns the updated row when this caller won the transition, else ``None``.

        ``local_failure_kind`` records *why this application* gave up, which is
        what decides whether the run is worth re-reading later. It is optional
        so a caller with nothing to say leaves it null rather than inventing a
        kind, and such a run is simply never reconciled.
        """
        return await self._terminate(
            delegation_id,
            status="failed",
            error=error,
            completed_at=completed_at,
            local_failure_kind=local_failure_kind,
        )

    async def mark_cancel_requested(
        self, delegation_id: str, *, now: datetime
    ) -> DelegationRunDict | None:
        """Record that cancellation was *asked for*, which is not that it happened.

        Only a later remote read can set ``cancel_confirmed_at``. Keeping the
        two apart is what stops a run reporting a cancellation the provider
        never performed -- the case where the provider instead carried on and
        completed.
        """
        return await self._update_run(delegation_id, cancel_requested_at=now)

    async def record_remote_observation(
        self,
        delegation_id: str,
        *,
        observation: RemoteObservationMetadata,
        observed_at: datetime,
        remote_status: str,
        cancel_confirmed: bool,
    ) -> DelegationRunDict | None:
        """Store the latest remote reading, refusing to go backwards.

        Guarded on ``remote_observed_at`` so a slow read that returns after a
        newer one cannot overwrite it: observations arrive from a poll, a
        reconciliation task and a recovery sweep, and out-of-order delivery
        between them is ordinary rather than exceptional. Returns ``None`` when
        the write was rejected as stale (or the run is gone).

        ``cancel_confirmed`` only ever sets the timestamp, never clears it: a
        provider that reported ``cancelled`` once and something else later did
        cancel the run, whatever it says now.
        """
        values: dict[str, object] = {
            "remote_observation_json": observation,
            "remote_observed_at": observed_at,
            "remote_status": remote_status[:64],
            "updated_at": observed_at,
        }
        if cancel_confirmed:
            values["cancel_confirmed_at"] = func.coalesce(
                delegation_runs_table.c.cancel_confirmed_at, observed_at
            )
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(
                or_(
                    delegation_runs_table.c.remote_observed_at.is_(None),
                    delegation_runs_table.c.remote_observed_at <= observed_at,
                )
            )
            .values(**values)
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("record_remote_observation", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def bump_reconcile_attempt(
        self, delegation_id: str, *, now: datetime
    ) -> int | None:
        """Increment and return the reconciliation read counter for a run."""
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(
                reconcile_attempts=delegation_runs_table.c.reconcile_attempts + 1,
                updated_at=now,
            )
            .returning(delegation_runs_table.c.reconcile_attempts)
        )
        result = await self._execute_with_logging("bump_reconcile_attempt", stmt)
        attempts = result.scalar_one_or_none()
        return int(attempts) if attempts is not None else None

    async def mark_reconciled(
        self, delegation_id: str, *, now: datetime
    ) -> DelegationRunDict | None:
        """Settle a run: it has stopped changing, or we have stopped looking.

        The single marker for "do not re-read this", whichever bound was
        reached, so the sweep has one predicate rather than re-deriving the
        bounds it was given.
        """
        return await self._update_run(delegation_id, reconciled_at=now)

    async def recover_late_completion(
        self,
        *,
        delegation_id: str,
        result_text: str | None,
        result_attachment_ids: list[str],
        recovered_at: datetime,
    ) -> DelegationRunDict | None:
        """Turn a locally failed run into a completed one, exactly once.

        The compare-and-set is the whole exactly-once guarantee: conditioned on
        the run still being ``failed`` with no recovery recorded, so concurrent
        reconcilers, a re-enqueued sweep and a retried task may all attempt it
        and precisely one wins. Returns the updated row to that winner.

        ``error`` is deliberately left alone. The original failure is the run's
        history and stays readable; ``late_recovered_at`` is what says the
        history was superseded. Delivery state is reset because the failure
        notice already went out and the result has not -- the run re-enters the
        ordinary terminal-delivery path as though it had just finished, which
        is also what applies the usual taint and review rules to it.

        ``completed_at`` moves to the recovery time so the unnotified-run sweep
        measures the delivery that is now owed, not the one already made.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(delegation_runs_table.c.status == "failed")
            .where(delegation_runs_table.c.late_recovered_at.is_(None))
            .values(
                status="completed",
                result_text=result_text,
                result_attachment_ids_json=result_attachment_ids,
                completed_at=recovered_at,
                late_recovered_at=recovered_at,
                reconciled_at=recovered_at,
                notified_at=None,
                notify_stage="initial",
                notify_attempts=0,
                notify_error=None,
                notify_first_failed_at=None,
                notify_last_failed_at=None,
                updated_at=recovered_at,
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("recover_late_completion", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def list_reconcilable(
        self,
        *,
        completed_after: datetime,
        completed_before: datetime,
        max_attempts: int,
        limit: int = 100,
    ) -> list[DelegationRunDict]:
        """Locally failed runs whose remote state is still worth re-reading.

        Bounded on every axis the sweep has: a run must have failed for a
        reason that leaves the provider possibly still holding it, must have a
        remote id to read, must not have settled, must not have exhausted its
        reads, and must lie inside the age window. Newest first, because a
        late completion is most likely on a run that failed recently.
        """
        bounded_limit = min(max(limit, 1), 500)
        stmt = (
            select(delegation_runs_table)
            .where(delegation_runs_table.c.status == "failed")
            .where(delegation_runs_table.c.reconciled_at.is_(None))
            .where(delegation_runs_table.c.remote_task_id.isnot(None))
            .where(
                delegation_runs_table.c.local_failure_kind.in_(
                    sorted(RECONCILABLE_FAILURE_KINDS)
                )
            )
            .where(delegation_runs_table.c.reconcile_attempts < max_attempts)
            .where(delegation_runs_table.c.completed_at > completed_after)
            .where(delegation_runs_table.c.completed_at <= completed_before)
            .order_by(delegation_runs_table.c.completed_at.desc())
            .limit(bounded_limit)
        )
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

    async def _terminate(
        self, delegation_id: str, **values: object
    ) -> DelegationRunDict | None:
        """Transition a non-terminal run to a terminal status (atomic CAS)."""
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .where(
                delegation_runs_table.c.status.notin_(
                    list(TERMINAL_DELEGATION_STATUSES)
                )
            )
            .values(**values, updated_at=datetime.now(UTC))
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("terminate_delegation_run", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def mark_notified(
        self,
        *,
        delegation_id: str,
        result_message_internal_id: int | None,
        notified_at: datetime,
    ) -> DelegationRunDict | None:
        """Record that a terminal delegation notification was delivered."""
        return await self._update_run(
            delegation_id,
            result_message_internal_id=result_message_internal_id,
            notified_at=notified_at,
        )

    async def reap_stale(
        self,
        *,
        now: datetime,
        created_before: datetime,
        error: str,
    ) -> list[DelegationRunDict]:
        """Fail non-terminal delegation runs created before ``created_before``.

        Covers both ``queued`` runs (whose owning task was lost before it ever
        started) and ``running`` runs (interrupted mid-flight), keyed on
        ``created_at`` so a run with no ``started_at`` is still reaped. Returns
        the rows that were transitioned so the caller can notify for each.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.status.in_(["queued", "running"]))
            .where(delegation_runs_table.c.created_at < created_before)
            .values(
                status="failed",
                error=error,
                completed_at=now,
                local_failure_kind="stranded",
                updated_at=now,
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("reap_stale_delegation_runs", stmt)
        return [self._row_to_dict(dict(row)) for row in result.all()]

    async def find_terminal_unnotified(
        self, *, completed_before: datetime
    ) -> list[DelegationRunDict]:
        """Return terminal runs whose completion notification was never delivered.

        A terminal run can be left unnotified when the caller crashed after the
        run finished but before delivering inline or claiming the handoff (so the
        worker's ``handed_off_at``-gated notification was skipped), or when a
        prior force-notify delivery failed. Gated on ``completed_at`` so a run a
        live inline caller is about to deliver within its short handoff window is
        not double-delivered.
        """
        stmt = (
            select(delegation_runs_table)
            .where(
                delegation_runs_table.c.status.in_(list(TERMINAL_DELEGATION_STATUSES))
            )
            .where(delegation_runs_table.c.notified_at.is_(None))
            .where(delegation_runs_table.c.notify_stage != "gave_up")
            .where(delegation_runs_table.c.completed_at < completed_before)
        )
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

    async def record_notify_failure(
        self, delegation_id: str, *, now: datetime
    ) -> DelegationRunDict | None:
        """Count a failed delivery attempt and stamp when they started.

        ``notify_first_failed_at`` is only set once, so it measures how long
        this run has been failing rather than when it last failed -- which is
        what decides that a transient failure has gone on too long to still be
        treated as one. ``notify_last_failed_at`` moves every time, so the wait
        before the next attempt can be measured from the most recent one.
        """
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(
                notify_attempts=delegation_runs_table.c.notify_attempts + 1,
                notify_first_failed_at=func.coalesce(
                    delegation_runs_table.c.notify_first_failed_at, now
                ),
                notify_last_failed_at=now,
                updated_at=now,
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("record_notify_failure", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def advance_notify_stage(
        self,
        delegation_id: str,
        *,
        stage: DelegationNotifyStage,
        now: datetime,
        notify_error: str | None = None,
    ) -> DelegationRunDict | None:
        """Move a run to its next delivery stage, committed before that send.

        Recording the stage first is what bounds the work: a retry that arrives
        after the stage was entered but before its send succeeded resumes at
        that send, rather than repeating the one that already failed.
        """
        values: dict[str, object] = {"notify_stage": stage, "updated_at": now}
        if notify_error is not None:
            values["notify_error"] = notify_error
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(**values)
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("advance_notify_stage", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    async def _update_run(
        self, delegation_id: str, **values: object
    ) -> DelegationRunDict | None:
        """Update a run row and return the updated row (or ``None`` if absent)."""
        update_values = {**values, "updated_at": datetime.now(UTC)}
        stmt = (
            update(delegation_runs_table)
            .where(delegation_runs_table.c.delegation_id == delegation_id)
            .values(**update_values)
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("update_delegation_run", stmt)
        row = result.one_or_none()
        return self._row_to_dict(dict(row)) if row is not None else None

    def summarize_run(self, run: DelegationRunDict) -> DelegationRunSummary:
        """Return a compact summary suitable for tool callers."""
        summary = DelegationRunSummary(
            delegation_id=run["delegation_id"],
            status=run["status"],
            target_service_id=run["target_service_id"],
            source_profile_id=run["source_profile_id"],
            interface_type=run["interface_type"],
            conversation_id=run["conversation_id"],
            request_text=run["request_text"],
            created_at=run["created_at"].isoformat(),
            started_at=self._to_iso(run["started_at"]),
            completed_at=self._to_iso(run["completed_at"]),
            handed_off_at=self._to_iso(run["handed_off_at"]),
        )
        if run["status"] == "completed":
            summary["result_text"] = run["result_text"]
        if run["status"] == "failed":
            summary["error"] = run["error"]
        return summary

    @staticmethod
    def _to_iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat()

    def _row_to_dict(self, row: Mapping[str, Any]) -> DelegationRunDict:
        """Normalize JSON and datetime fields from a delegation run row."""
        return DelegationRunDict(
            id=row["id"],
            delegation_id=row["delegation_id"],
            task_id=row["task_id"],
            status=row["status"],
            source_profile_id=row["source_profile_id"],
            target_service_id=row["target_service_id"],
            interface_type=row["interface_type"],
            conversation_id=row["conversation_id"],
            user_id=row.get("user_id"),
            user_name=row.get("user_name"),
            source_turn_id=row.get("source_turn_id"),
            source_subconversation_id=row.get("source_subconversation_id"),
            subconversation_id=row["subconversation_id"],
            request_text=row["request_text"],
            content_parts_json=self._json_list(row["content_parts_json"]),
            taint_state_json=self._json_mapping(row.get("taint_state_json")),
            model_selection_json=self._json_value(row.get("model_selection_json")),
            handed_off_at=row.get("handed_off_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            updated_at=row.get("updated_at"),
            result_text=row.get("result_text"),
            result_attachment_ids_json=self._json_str_list(
                row.get("result_attachment_ids_json")
            ),
            result_message_internal_id=row.get("result_message_internal_id"),
            error=row.get("error"),
            notified_at=row.get("notified_at"),
            notify_stage=row.get("notify_stage") or "initial",
            notify_attempts=row.get("notify_attempts") or 0,
            notify_error=row.get("notify_error"),
            notify_first_failed_at=row.get("notify_first_failed_at"),
            notify_last_failed_at=row.get("notify_last_failed_at"),
            remote_task_id=row.get("remote_task_id"),
            remote_context_id=row.get("remote_context_id"),
            poll_attempts=row.get("poll_attempts") or 0,
            local_failure_kind=row.get("local_failure_kind"),
            cancel_requested_at=row.get("cancel_requested_at"),
            cancel_confirmed_at=row.get("cancel_confirmed_at"),
            remote_status=row.get("remote_status"),
            remote_observed_at=row.get("remote_observed_at"),
            remote_observation_json=cast(
                "RemoteObservationMetadata | None",
                self._json_mapping(row.get("remote_observation_json")),
            ),
            reconcile_attempts=row.get("reconcile_attempts") or 0,
            reconciled_at=row.get("reconciled_at"),
            late_recovered_at=row.get("late_recovered_at"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _json_list(value: Any) -> list[ContentPartDict]:  # noqa: ANN401
        if isinstance(value, list):
            return cast("list[ContentPartDict]", value)
        if isinstance(value, str):
            loaded = json.loads(value)
            return (
                cast("list[ContentPartDict]", loaded)
                if isinstance(loaded, list)
                else []
            )
        return []

    @staticmethod
    def _json_mapping(value: Any) -> TaintMetadata | None:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        return cast("TaintMetadata", value) if isinstance(value, dict) else None

    @staticmethod
    def _json_value(value: Any) -> object | None:  # noqa: ANN401
        """Decode a JSON column and stop there.

        Deliberately shapes nothing: a payload coerced into the type a caller
        expects is a malformed payload that has become a plausible one, and the
        caller then acts on it. Whoever knows what the column means validates
        it -- and raises.
        """
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _json_str_list(value: Any) -> list[str] | None:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, list):
            return [str(item) for item in value]
        return None
