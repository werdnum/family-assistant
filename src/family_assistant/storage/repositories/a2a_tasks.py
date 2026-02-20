"""Repository for A2A protocol task persistence.

Stores task state, conversation mapping, and artifacts for A2A interactions.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import NotRequired, TypedDict

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata
from family_assistant.storage.repositories.base import BaseRepository

logger = logging.getLogger(__name__)

a2a_tasks_table = Table(
    "a2a_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(100), nullable=False, unique=True, index=True),
    Column("context_id", String(100), nullable=True, index=True),
    Column("profile_id", String(100), nullable=False),
    Column("conversation_id", String(100), nullable=False),
    Column("status", String(50), nullable=False, server_default="submitted"),
    Column(
        "artifacts_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column(
        "history_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column(
        "metadata_json",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("updated_at", DateTime(timezone=True), nullable=True, onupdate=func.now()),
)


class A2ATaskRow(TypedDict):
    """Typed dictionary for A2A task data returned from database queries."""

    id: int
    task_id: str
    context_id: str | None
    profile_id: str
    conversation_id: str
    status: str
    artifacts_json: NotRequired[list[dict[str, object]] | None]
    history_json: NotRequired[list[dict[str, object]] | None]
    metadata_json: NotRequired[dict[str, object] | None]
    created_at: str  # ISO format after conversion
    updated_at: NotRequired[str | None]


class A2ATasksRepository(BaseRepository):
    """Repository for A2A task persistence."""

    async def create_task(
        self,
        task_id: str,
        profile_id: str,
        conversation_id: str,
        context_id: str | None = None,
        status: str = "submitted",
        history_json: list[dict[str, object]] | None = None,
    ) -> A2ATaskRow:
        """Create a new A2A task record."""
        stmt = insert(a2a_tasks_table).values(
            task_id=task_id,
            context_id=context_id,
            profile_id=profile_id,
            conversation_id=conversation_id,
            status=status,
            history_json=history_json,
        )
        await self._execute_with_logging("create_a2a_task", stmt)
        return A2ATaskRow(
            id=0,
            task_id=task_id,
            context_id=context_id,
            profile_id=profile_id,
            conversation_id=conversation_id,
            status=status,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def get_task(self, task_id: str) -> A2ATaskRow | None:
        """Get an A2A task by its task ID."""
        stmt = select(a2a_tasks_table).where(a2a_tasks_table.c.task_id == task_id)
        row = await self._db.fetch_one(stmt)
        if row is None:
            return None
        return self._row_to_typed(row)

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        artifacts_json: list[dict[str, object]] | None = None,
        history_json: list[dict[str, object]] | None = None,
    ) -> bool:
        """Update the status and optionally artifacts/history of a task."""
        values: dict[str, object] = {
            "status": status,
            "updated_at": datetime.now(UTC),
        }
        if artifacts_json is not None:
            values["artifacts_json"] = artifacts_json
        if history_json is not None:
            values["history_json"] = history_json

        stmt = (
            update(a2a_tasks_table)
            .where(a2a_tasks_table.c.task_id == task_id)
            .values(**values)
        )
        result = await self._execute_with_logging("update_a2a_task_status", stmt)
        return result.rowcount > 0  # type: ignore[attr-defined]  # CursorResult always has rowcount

    async def list_tasks(
        self,
        context_id: str | None = None,
        limit: int = 100,
    ) -> list[A2ATaskRow]:
        """List A2A tasks, optionally filtered by context ID."""
        stmt = (
            select(a2a_tasks_table)
            .order_by(a2a_tasks_table.c.created_at.desc())
            .limit(limit)
        )
        if context_id:
            stmt = stmt.where(a2a_tasks_table.c.context_id == context_id)
        rows = await self._db.fetch_all(stmt)
        return [self._row_to_typed(row) for row in rows]

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task (set status to 'canceled')."""
        stmt = (
            update(a2a_tasks_table)
            .where(
                a2a_tasks_table.c.task_id == task_id,
                a2a_tasks_table.c.status.in_([
                    "submitted",
                    "working",
                    "input-required",
                ]),
            )
            .values(status="canceled", updated_at=datetime.now(UTC))
        )
        result = await self._execute_with_logging("cancel_a2a_task", stmt)
        return result.rowcount > 0  # type: ignore[attr-defined]  # CursorResult always has rowcount

    async def cleanup_old_tasks(self, retention_hours: int = 168) -> int:
        """Delete tasks older than the retention period (default 7 days)."""
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
        stmt = delete(a2a_tasks_table).where(a2a_tasks_table.c.created_at < cutoff)
        result = await self._execute_with_logging("cleanup_a2a_tasks", stmt)
        return result.rowcount  # type: ignore[attr-defined]  # CursorResult always has rowcount

    # ast-grep-ignore: no-dict-any - Input is raw database row from fetch_one/fetch_all
    @staticmethod
    def _row_to_typed(row: dict[str, object]) -> A2ATaskRow:
        """Convert a database row to a typed dictionary."""
        row_dict = dict(row)
        for field in ("artifacts_json", "history_json", "metadata_json"):
            val = row_dict.get(field)
            if isinstance(val, str):
                row_dict[field] = json.loads(val)
        for field in ("created_at", "updated_at"):
            val = row_dict.get(field)
            if isinstance(val, datetime):
                row_dict[field] = val.isoformat()
        return A2ATaskRow(**row_dict)  # type: ignore[typeddict-item]  # row keys match TypedDict
