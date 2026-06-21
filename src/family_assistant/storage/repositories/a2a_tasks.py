"""Repository for A2A protocol task persistence.

Stores task state, conversation mapping, and artifacts for A2A interactions.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NotRequired, TypedDict

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
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata
from family_assistant.storage.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.sql.dml import Insert as SqlInsert

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
    ) -> None:
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

    async def create_task_if_absent(
        self,
        task_id: str,
        profile_id: str,
        conversation_id: str,
        context_id: str | None = None,
        status: str = "submitted",
        history_json: list[dict[str, object]] | None = None,
    ) -> A2ATaskRow | None:
        """Create a task, or return the existing row for a duplicate task ID."""
        existing = await self.get_task(task_id)
        if existing is not None:
            return existing

        if self._db.engine.dialect.name in {"postgresql", "sqlite"}:
            stmt = self._insert_task_ignoring_conflict(
                dialect_name=self._db.engine.dialect.name,
                task_id=task_id,
                profile_id=profile_id,
                conversation_id=conversation_id,
                context_id=context_id,
                status=status,
                history_json=history_json,
            )
            result = await self._execute_with_logging("create_a2a_task_if_absent", stmt)
            if result.rowcount > 0:  # type: ignore[attr-defined]  # CursorResult always has rowcount
                return None

            existing = await self.get_task(task_id)
            if existing is None:
                raise RuntimeError(
                    f"A2A task {task_id!r} insert conflicted but no existing row was found"
                )
            return existing

        conn = self._db.conn
        if conn is None:
            raise RuntimeError("No active database connection")

        stmt = insert(a2a_tasks_table).values(
            task_id=task_id,
            context_id=context_id,
            profile_id=profile_id,
            conversation_id=conversation_id,
            status=status,
            history_json=history_json,
        )

        savepoint = await conn.begin_nested()
        try:
            await conn.execute(stmt)
        except IntegrityError:
            await savepoint.rollback()
            existing = await self.get_task(task_id)
            if existing is None:
                raise
            return existing
        except Exception:
            await savepoint.rollback()
            raise
        else:
            await savepoint.commit()
            return None

    @staticmethod
    def _insert_task_ignoring_conflict(
        *,
        dialect_name: str,
        task_id: str,
        profile_id: str,
        conversation_id: str,
        context_id: str | None,
        status: str,
        history_json: list[dict[str, object]] | None,
    ) -> SqlInsert:
        values = {
            "task_id": task_id,
            "context_id": context_id,
            "profile_id": profile_id,
            "conversation_id": conversation_id,
            "status": status,
            "history_json": history_json,
        }
        index_elements = [a2a_tasks_table.c.task_id]
        if dialect_name == "postgresql":
            stmt = postgresql.insert(a2a_tasks_table)
        else:
            stmt = sqlite.insert(a2a_tasks_table)
        return stmt.values(**values).on_conflict_do_nothing(
            index_elements=index_elements
        )

    async def get_task(self, task_id: str) -> A2ATaskRow | None:
        """Get an A2A task by its task ID."""
        stmt = select(a2a_tasks_table).where(a2a_tasks_table.c.task_id == task_id)
        row = await self._db.fetch_one(stmt)
        if row is None:
            return None
        return self._row_to_typed(row)

    # Statuses a task cannot be moved out of: a terminal task must never be
    # resurrected or overwritten (e.g. a tasks/cancel racing a background send
    # that finishes just afterwards).
    _TERMINAL_STATUSES = ("completed", "failed", "canceled", "rejected")

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        artifacts_json: list[dict[str, object]] | None = None,
        history_json: list[dict[str, object]] | None = None,
    ) -> bool:
        """Update a non-terminal task's status and optionally artifacts/history.

        Conditioned on the task not already being terminal (atomic CAS) so a
        late-finishing background send cannot overwrite a row a tasks/cancel
        already finalized. Returns ``True`` only if a row changed.
        """
        values: dict[str, object] = {
            "status": status,
        }
        if artifacts_json is not None:
            values["artifacts_json"] = artifacts_json
        if history_json is not None:
            values["history_json"] = history_json

        stmt = (
            update(a2a_tasks_table)
            .where(
                a2a_tasks_table.c.task_id == task_id,
                a2a_tasks_table.c.status.notin_(self._TERMINAL_STATUSES),
            )
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
