"""Repository for durable tool confirmation requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from sqlalchemy import insert, select, update

from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.datetime_utils import normalize_datetime
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolArguments, ToolArgumentsView

ConfirmationStatus = Literal["pending", "approved", "rejected", "expired"]


class ConfirmationRequestRow(TypedDict):
    """Typed representation of a durable tool confirmation request row."""

    id: str
    target_user_id: str
    status: ConfirmationStatus
    tool_name: str
    tool_args_json: ToolArguments
    tool_call_id: str | None
    source_message_internal_id: int | None
    confirmation_prompt: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: str | None
    resolved_via_interface: str | None
    execution_task_id: str | None


class ConfirmationRequestsRepository(BaseRepository):
    """Repository for durable confirmation request persistence."""

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
        """Create a pending confirmation request."""
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")

        now = datetime.now(UTC)
        stmt = (
            insert(confirmation_requests_table)
            .values(
                id=request_id,
                target_user_id=target_user_id,
                status="pending",
                tool_name=tool_name,
                tool_args_json=dict(tool_args),
                tool_call_id=tool_call_id,
                source_message_internal_id=source_message_internal_id,
                confirmation_prompt=confirmation_prompt,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            .returning(confirmation_requests_table)
        )
        result = await self._execute_with_logging("create_confirmation_request", stmt)
        return self._row_to_typed(dict(result.mappings().one()))

    async def get(self, request_id: str) -> ConfirmationRequestRow | None:
        """Get a confirmation request by id."""
        stmt = select(confirmation_requests_table).where(
            confirmation_requests_table.c.id == request_id
        )
        row = await self._db.fetch_one(stmt)
        return self._row_to_typed(row) if row is not None else None

    async def list_pending_for_user(self, user_id: str) -> list[ConfirmationRequestRow]:
        """List pending confirmation requests for a target user."""
        now = datetime.now(UTC)
        stmt = (
            select(confirmation_requests_table)
            .where(
                confirmation_requests_table.c.target_user_id == user_id,
                confirmation_requests_table.c.status == "pending",
                confirmation_requests_table.c.expires_at > now,
            )
            .order_by(confirmation_requests_table.c.created_at.asc())
        )
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_typed(row) for row in rows]

    async def approve_pending(
        self,
        *,
        request_id: str,
        resolving_user_id: str,
        resolving_interface: str,
        execution_task_id: str,
        now: datetime,
    ) -> ConfirmationRequestRow | None:
        """Move a pending request to approved and store the execution task id."""
        stmt = (
            update(confirmation_requests_table)
            .where(
                confirmation_requests_table.c.id == request_id,
                confirmation_requests_table.c.status == "pending",
                confirmation_requests_table.c.expires_at > now,
            )
            .values(
                status="approved",
                updated_at=now,
                resolved_at=now,
                resolved_by_user_id=resolving_user_id,
                resolved_via_interface=resolving_interface,
                execution_task_id=execution_task_id,
            )
            .returning(confirmation_requests_table)
        )
        result = await self._execute_with_logging("approve_confirmation_request", stmt)
        row = result.mappings().one_or_none()
        return self._row_to_typed(dict(row)) if row is not None else None

    async def reject_pending(
        self,
        *,
        request_id: str,
        resolving_user_id: str,
        resolving_interface: str,
        now: datetime,
    ) -> ConfirmationRequestRow | None:
        """Move a pending request to rejected."""
        stmt = (
            update(confirmation_requests_table)
            .where(
                confirmation_requests_table.c.id == request_id,
                confirmation_requests_table.c.status == "pending",
                confirmation_requests_table.c.expires_at > now,
            )
            .values(
                status="rejected",
                updated_at=now,
                resolved_at=now,
                resolved_by_user_id=resolving_user_id,
                resolved_via_interface=resolving_interface,
            )
            .returning(confirmation_requests_table)
        )
        result = await self._execute_with_logging("reject_confirmation_request", stmt)
        row = result.mappings().one_or_none()
        return self._row_to_typed(dict(row)) if row is not None else None

    async def mark_expired(self, *, now: datetime) -> int:
        """Expire pending requests whose deadline has passed."""
        stmt = (
            update(confirmation_requests_table)
            .where(
                confirmation_requests_table.c.status == "pending",
                confirmation_requests_table.c.expires_at <= now,
            )
            .values(status="expired", updated_at=now)
        )
        result = await self._execute_with_logging("expire_confirmation_requests", stmt)
        return cast("int", result.rowcount)

    @staticmethod
    def _row_to_typed(row: dict[str, object]) -> ConfirmationRequestRow:
        row = dict(row)
        for field in ("expires_at", "created_at", "updated_at", "resolved_at"):
            row[field] = ConfirmationRequestsRepository._normalize_datetime_field(
                row.get(field)
            )
        return ConfirmationRequestRow(**row)  # type: ignore[typeddict-item]  # row keys match TypedDict

    @staticmethod
    def _normalize_datetime_field(value: object) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime | str):
            raise TypeError(f"Expected datetime or str, got {type(value).__name__}")
        return normalize_datetime(value)
