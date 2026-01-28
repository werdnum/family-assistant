"""Repository for managing AI worker tasks in the database."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, NotRequired, TypedDict

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


class WorkerTaskDict(TypedDict):
    """Typed dictionary for worker task data returned from database queries.

    Note: datetime fields are converted to ISO format strings by _row_to_dict.
    """

    # Required fields (always present in database rows)
    id: int
    task_id: str
    conversation_id: str
    interface_type: str
    model: str
    task_description: str
    timeout_minutes: int
    status: str
    created_at: str  # ISO format string after _row_to_dict conversion
    context_files: list[str]  # Defaults to [] if not set
    output_files: list[str]  # Defaults to [] if not set

    # Optional fields (may be None)
    user_name: NotRequired[str | None]
    job_name: NotRequired[str | None]
    started_at: NotRequired[str | None]  # ISO format string
    completed_at: NotRequired[str | None]  # ISO format string
    duration_seconds: NotRequired[int | None]
    exit_code: NotRequired[int | None]
    summary: NotRequired[str | None]
    error_message: NotRequired[str | None]
    updated_at: NotRequired[str | None]  # ISO format string
    callback_token: NotRequired[str | None]  # Security token for webhook verification


# Define the worker_tasks table
worker_tasks_table = Table(
    "worker_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(100), nullable=False, unique=True, index=True),
    Column("conversation_id", String(255), nullable=False, index=True),
    Column("interface_type", String(50), nullable=False),
    Column("user_name", String(255), nullable=True),
    # Task configuration
    Column("model", String(50), nullable=False, server_default="claude"),
    Column("task_description", Text, nullable=False),
    Column(
        "context_files",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column("timeout_minutes", Integer, nullable=False, server_default="30"),
    # Status tracking
    Column("status", String(50), nullable=False, server_default="pending", index=True),
    Column("job_name", String(255), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("duration_seconds", Integer, nullable=True),
    # Results
    Column("exit_code", Integer, nullable=True),
    Column(
        "output_files",
        JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
        nullable=True,
    ),
    Column("summary", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    # Metadata
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("updated_at", DateTime(timezone=True), nullable=True, onupdate=func.now()),
    # Security
    Column("callback_token", String(64), nullable=True),
)


class WorkerTasksRepository(BaseRepository):
    """Repository for managing AI worker tasks."""

    async def create_task(
        self,
        task_id: str,
        conversation_id: str,
        interface_type: str,
        task_description: str,
        model: str = "claude",
        context_files: list[str] | None = None,
        timeout_minutes: int = 30,
        user_name: str | None = None,
        callback_token: str | None = None,
        # ast-grep-ignore: no-dict-any - Worker task data is dynamic JSON from database
    ) -> dict[str, Any]:
        """Create a new worker task record.

        Args:
            task_id: Unique task identifier
            conversation_id: ID of the originating conversation
            interface_type: Type of interface (telegram, web, etc.)
            task_description: Description of the task for the worker
            model: AI model to use (claude, gemini)
            context_files: List of file paths to include as context
            timeout_minutes: Maximum execution time
            user_name: Name of the user who initiated the task
            callback_token: Security token for webhook verification

        Returns:
            Dictionary containing the created task data
        """
        now = datetime.now(UTC)

        stmt = insert(worker_tasks_table).values(
            task_id=task_id,
            conversation_id=conversation_id,
            interface_type=interface_type,
            user_name=user_name,
            model=model,
            task_description=task_description,
            context_files=context_files or [],
            timeout_minutes=timeout_minutes,
            status="pending",
            created_at=now,
            callback_token=callback_token,
        )

        await self._execute_with_logging("create_task", stmt)
        self._logger.info(f"Created worker task: {task_id}")

        return {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "interface_type": interface_type,
            "user_name": user_name,
            "model": model,
            "task_description": task_description,
            "context_files": context_files or [],
            "timeout_minutes": timeout_minutes,
            "status": "pending",
            "created_at": now.isoformat(),
            "callback_token": callback_token,
        }

    # ast-grep-ignore: no-dict-any - Worker task data is dynamic JSON from database
    async def get_task(self, task_id: str) -> WorkerTaskDict | None:
        """Get a task by its ID.

        Args:
            task_id: The task ID to look up

        Returns:
            WorkerTaskDict containing task data, or None if not found
        """
        stmt = select(worker_tasks_table).where(worker_tasks_table.c.task_id == task_id)
        row = await self._db.fetch_one(stmt)

        if row:
            return self._row_to_dict(row)
        return None

    async def get_tasks_for_conversation(
        self,
        conversation_id: str,
        status: str | None = None,
        limit: int = 10,
    ) -> list[WorkerTaskDict]:
        """Get tasks for a conversation.

        Args:
            conversation_id: The conversation ID
            status: Optional status filter
            limit: Maximum number of tasks to return

        Returns:
            List of WorkerTaskDict
        """
        stmt = (
            select(worker_tasks_table)
            .where(worker_tasks_table.c.conversation_id == conversation_id)
            .order_by(worker_tasks_table.c.created_at.desc())
            .limit(limit)
        )

        if status:
            stmt = stmt.where(worker_tasks_table.c.status == status)

        rows = await self._db.fetch_all(stmt)
        return [self._row_to_dict(row) for row in rows]

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        job_name: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        duration_seconds: int | None = None,
        exit_code: int | None = None,
        # ast-grep-ignore: no-dict-any - output_files contains dynamic metadata from worker
        output_files: list[dict[str, Any]] | None = None,
        summary: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update task status and results.

        Args:
            task_id: The task ID to update
            status: New status (pending, submitted, running, success, failed, timeout, cancelled)
            job_name: Kubernetes Job name (if applicable)
            started_at: When the task started
            completed_at: When the task completed
            duration_seconds: Task duration in seconds
            exit_code: Exit code from the worker
            output_files: List of output file metadata
            summary: Summary of the task result
            error_message: Error message if failed

        Returns:
            True if task was updated, False if not found
        """
        # ast-grep-ignore: no-dict-any - SQLAlchemy update values are dynamic
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}

        if job_name is not None:
            values["job_name"] = job_name
        if started_at is not None:
            values["started_at"] = started_at
        if completed_at is not None:
            values["completed_at"] = completed_at
        if duration_seconds is not None:
            values["duration_seconds"] = duration_seconds
        if exit_code is not None:
            values["exit_code"] = exit_code
        if output_files is not None:
            values["output_files"] = output_files
        if summary is not None:
            values["summary"] = summary
        if error_message is not None:
            values["error_message"] = error_message

        stmt = (
            update(worker_tasks_table)
            .where(worker_tasks_table.c.task_id == task_id)
            .values(**values)
        )

        result = await self._execute_with_logging("update_task_status", stmt)
        # CursorResult.rowcount returns int but type stubs don't reflect this for async
        updated = result.rowcount > 0  # type: ignore[union-attr]

        if updated:
            self._logger.info(f"Updated worker task {task_id} to status: {status}")
        else:
            self._logger.warning(f"Worker task not found for update: {task_id}")

        return updated

    async def get_running_tasks_count(self) -> int:
        """Count currently running tasks (for concurrency limit).

        Returns:
            Number of tasks with status 'submitted' or 'running'
        """
        stmt = select(func.count(worker_tasks_table.c.id).label("count")).where(
            worker_tasks_table.c.status.in_(["submitted", "running"])
        )
        row = await self._db.fetch_one(stmt)
        return row["count"] if row else 0

    async def cleanup_old_tasks(self, retention_hours: int = 48) -> int:
        """Delete old task records.

        Args:
            retention_hours: Delete tasks older than this many hours

        Returns:
            Number of tasks deleted
        """
        cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)

        stmt = delete(worker_tasks_table).where(
            worker_tasks_table.c.created_at < cutoff
        )

        result = await self._execute_with_logging("cleanup_old_tasks", stmt)
        # CursorResult.rowcount returns int but type stubs don't reflect this for async
        deleted_count = result.rowcount  # type: ignore[union-attr]

        if deleted_count > 0:
            self._logger.info(f"Cleaned up {deleted_count} old worker tasks")

        return deleted_count

    @staticmethod
    def _require_str(value: str | None, field_name: str, task_id: str) -> str:
        """Require a non-null string value, failing fast if None.

        Used for database fields that are non-nullable but where the type
        system can't guarantee non-None (e.g., after datetime conversion).
        """
        if value is None:
            raise ValueError(
                f"{field_name} is required but was None for task {task_id}"
            )
        return value

    # ast-grep-ignore: no-dict-any - Input is raw database row from fetch_one/fetch_all
    def _row_to_dict(self, row: dict[str, Any]) -> WorkerTaskDict:
        """Normalize a database row dictionary to WorkerTaskDict.

        Parses JSON fields and converts datetime fields to ISO format strings.

        Args:
            row: Database row dict (from fetch_one/fetch_all)

        Returns:
            Normalized WorkerTaskDict
        """
        # Parse JSON fields
        context_files: list[str] = []
        if row.get("context_files"):
            try:
                if isinstance(row["context_files"], str):
                    context_files = json.loads(row["context_files"])
                elif isinstance(row["context_files"], list):
                    context_files = row["context_files"]
            except (json.JSONDecodeError, TypeError):
                pass

        output_files: list[str] = []
        if row.get("output_files"):
            try:
                if isinstance(row["output_files"], str):
                    output_files = json.loads(row["output_files"])
                elif isinstance(row["output_files"], list):
                    output_files = row["output_files"]
            except (json.JSONDecodeError, TypeError):
                pass

        # Convert datetime fields to ISO format strings
        def to_iso(value: Any) -> str | None:  # noqa: ANN401 - database returns mixed types
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, str):
                return value
            return str(value)

        return WorkerTaskDict(
            id=row["id"],
            task_id=row["task_id"],
            conversation_id=row["conversation_id"],
            interface_type=row["interface_type"],
            user_name=row.get("user_name"),
            model=row["model"],
            task_description=row["task_description"],
            context_files=context_files,
            timeout_minutes=row["timeout_minutes"],
            status=row["status"],
            job_name=row.get("job_name"),
            started_at=to_iso(row.get("started_at")),
            completed_at=to_iso(row.get("completed_at")),
            duration_seconds=row.get("duration_seconds"),
            exit_code=row.get("exit_code"),
            output_files=output_files,
            summary=row.get("summary"),
            error_message=row.get("error_message"),
            created_at=self._require_str(
                to_iso(row["created_at"]), "created_at", row["task_id"]
            ),
            updated_at=to_iso(row.get("updated_at")),
            callback_token=row.get("callback_token"),
        )
