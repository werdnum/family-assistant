"""Repository for asynchronous profile delegation runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NotRequired, Required, TypedDict, cast

from sqlalchemy import insert, select, update

from family_assistant.storage.delegation_runs import (
    TERMINAL_DELEGATION_STATUSES,
    DelegationRunStatus,
    delegation_runs_table,
)
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.security.taint import TaintMetadata

__all__ = [
    "TERMINAL_DELEGATION_STATUSES",
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
    handed_off_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    result_text: str | None
    result_attachment_ids_json: list[str] | None
    result_message_internal_id: int | None
    error: str | None
    notified_at: datetime | None
    remote_task_id: str | None
    remote_context_id: str | None
    poll_attempts: int
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
        row = result.mappings().one()
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
        row = result.mappings().one_or_none()
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
        row = result.mappings().one_or_none()
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
        row = result.one_or_none()
        return int(row[0]) if row is not None else None

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
    ) -> DelegationRunDict | None:
        """Mark a non-terminal delegation run failed (atomic CAS).

        Conditioned on the run still being non-terminal (see ``mark_completed``).
        Returns the updated row when this caller won the transition, else ``None``.
        """
        return await self._terminate(
            delegation_id,
            status="failed",
            error=error,
            completed_at=completed_at,
        )

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
        row = result.mappings().one_or_none()
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
                updated_at=now,
            )
            .returning(delegation_runs_table)
        )
        result = await self._execute_with_logging("reap_stale_delegation_runs", stmt)
        return [self._row_to_dict(dict(row)) for row in result.mappings().all()]

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
            .where(delegation_runs_table.c.completed_at < completed_before)
        )
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

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
        row = result.mappings().one_or_none()
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
            remote_task_id=row.get("remote_task_id"),
            remote_context_id=row.get("remote_context_id"),
            poll_attempts=row.get("poll_attempts") or 0,
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
    def _json_str_list(value: Any) -> list[str] | None:  # noqa: ANN401
        if value is None:
            return None
        if isinstance(value, str):
            value = json.loads(value)
        if isinstance(value, list):
            return [str(item) for item in value]
        return None
