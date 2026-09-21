"""Functional tests for worker task lifecycle robustness.

Tests reconciliation of stale tasks, cancel tool, stale task marking,
and cleanup protection for active tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from family_assistant.services.backends.mock import MockBackend
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.worker_tasks import worker_tasks_table
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.tools.worker import (
    cancel_worker_task_tool,
    reconcile_stale_tasks,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """Create a database context for testing."""
    context = Database(engine=db_engine)
    yield context


@pytest.fixture
def mock_backend() -> MockBackend:
    """Create a fresh MockBackend for each test."""
    return MockBackend()


def _make_exec_context(
    db_context: Database,
    conversation_id: str = "conv-test",
    processing_service: MagicMock | None = None,
) -> ToolExecutionContext:
    """Create a minimal ToolExecutionContext for testing."""
    if processing_service is None:
        processing_service = MagicMock()
        processing_service.app_config.ai_worker_config.backend_type = "mock"
        processing_service.app_config.ai_worker_config.workspace_mount_path = (
            "/tmp/test"
        )
        processing_service.app_config.ai_worker_config.docker = None
        processing_service.app_config.ai_worker_config.kubernetes = None

    return ToolExecutionContext(
        interface_type="test",
        conversation_id=conversation_id,
        user_name="test_user",
        turn_id=None,
        db_context=db_context,
        processing_service=processing_service,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _backdate_task(db_context: Database, task_id: str, age: timedelta) -> None:
    """Set a task's created_at to (now - age)."""
    old_time = datetime.now(UTC) - age
    stmt = (
        update(worker_tasks_table)
        .where(worker_tasks_table.c.task_id == task_id)
        .values(created_at=old_time)
    )
    await db_context.execute(stmt)


class TestReconcileStale:
    """Tests for reconcile_stale_tasks."""

    @pytest.mark.asyncio
    async def test_reconcile_marks_tasks_without_job_name(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Tasks in 'submitted' with no job_name should be marked failed."""
        await db_context.worker_tasks.create_task(
            task_id="orphan-1",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Orphan task",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="orphan-1", status="submitted"
        )

        reconciled = await reconcile_stale_tasks(db_context, mock_backend)

        assert reconciled == 1
        task = await db_context.worker_tasks.get_task("orphan-1")
        assert task is not None
        assert task["status"] == "failed"
        assert "no job_name" in (task.get("error_message") or "")

    @pytest.mark.asyncio
    async def test_reconcile_updates_from_backend_terminal_status(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Tasks whose backend job is terminal should be updated in DB."""
        job_id = await mock_backend.spawn_task(
            task_id="task-with-job",
            prompt_path="tasks/test/prompt.md",
            output_dir="tasks/test/output",
            webhook_url="http://test/webhook",
            model="claude",
            timeout_minutes=30,
        )

        await db_context.worker_tasks.create_task(
            task_id="task-with-job",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Task with job",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="task-with-job", status="submitted", job_name=job_id
        )

        mock_backend.complete_task(job_id, success=True)

        reconciled = await reconcile_stale_tasks(db_context, mock_backend)

        assert reconciled == 1
        task = await db_context.worker_tasks.get_task("task-with-job")
        assert task is not None
        assert task["status"] == "success"

    @pytest.mark.asyncio
    async def test_reconcile_leaves_active_backend_tasks(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Tasks that are still active in the backend should be left alone."""
        job_id = await mock_backend.spawn_task(
            task_id="still-running",
            prompt_path="tasks/test/prompt.md",
            output_dir="tasks/test/output",
            webhook_url="http://test/webhook",
            model="claude",
            timeout_minutes=30,
        )

        await db_context.worker_tasks.create_task(
            task_id="still-running",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Still running",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="still-running", status="submitted", job_name=job_id
        )

        reconciled = await reconcile_stale_tasks(db_context, mock_backend)

        assert reconciled == 0
        task = await db_context.worker_tasks.get_task("still-running")
        assert task is not None
        assert task["status"] == "submitted"

    @pytest.mark.asyncio
    async def test_reconcile_handles_unknown_job(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Tasks with a job_name that backend doesn't recognize should be marked failed."""
        await db_context.worker_tasks.create_task(
            task_id="ghost-job",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Ghost job",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="ghost-job", status="submitted", job_name="nonexistent-job-id"
        )

        reconciled = await reconcile_stale_tasks(db_context, mock_backend)

        assert reconciled == 1
        task = await db_context.worker_tasks.get_task("ghost-job")
        assert task is not None
        assert task["status"] == "failed"

    @pytest.mark.asyncio
    async def test_reconcile_no_active_tasks(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Reconciliation with no active tasks should return 0."""
        reconciled = await reconcile_stale_tasks(db_context, mock_backend)
        assert reconciled == 0


class TestCancelWorkerTask:
    """Tests for cancel_worker_task_tool."""

    @pytest.mark.asyncio
    async def test_cancel_active_task(
        self, db_context: Database, mock_backend: MockBackend
    ) -> None:
        """Cancelling an active task should update DB and cancel in backend."""
        job_id = await mock_backend.spawn_task(
            task_id="cancel-me",
            prompt_path="tasks/test/prompt.md",
            output_dir="tasks/test/output",
            webhook_url="http://test/webhook",
            model="claude",
            timeout_minutes=30,
        )

        await db_context.worker_tasks.create_task(
            task_id="cancel-me",
            conversation_id="conv-cancel",
            interface_type="test",
            task_description="Cancel this",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="cancel-me", status="submitted", job_name=job_id
        )

        exec_context = _make_exec_context(db_context, conversation_id="conv-cancel")

        result = await cancel_worker_task_tool(exec_context, task_id="cancel-me")
        data = result.get_data()

        assert isinstance(data, dict)
        assert data["status"] == "cancelled"

        task = await db_context.worker_tasks.get_task("cancel-me")
        assert task is not None
        assert task["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_terminal_task_returns_error(
        self, db_context: Database
    ) -> None:
        """Cancelling a task that already completed should return an error."""
        await db_context.worker_tasks.create_task(
            task_id="done-task",
            conversation_id="conv-done",
            interface_type="test",
            task_description="Already done",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="done-task", status="success"
        )

        exec_context = _make_exec_context(db_context, conversation_id="conv-done")

        result = await cancel_worker_task_tool(exec_context, task_id="done-task")
        data = result.get_data()

        assert isinstance(data, dict)
        assert "error" in data
        assert "terminal" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task_returns_error(
        self, db_context: Database
    ) -> None:
        """Cancelling a task that doesn't exist should return an error."""
        exec_context = _make_exec_context(db_context, conversation_id="conv-x")

        result = await cancel_worker_task_tool(exec_context, task_id="no-such-task")
        data = result.get_data()

        assert isinstance(data, dict)
        assert "error" in data


class TestMarkStaleTasks:
    """Tests for WorkerTasksRepository.mark_stale_tasks."""

    @pytest.mark.asyncio
    async def test_marks_old_submitted_tasks(self, db_context: Database) -> None:
        """Submitted tasks older than the timeout should be marked failed."""
        await db_context.worker_tasks.create_task(
            task_id="stale-submitted",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Stale submitted",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="stale-submitted", status="submitted"
        )
        await _backdate_task(db_context, "stale-submitted", timedelta(hours=2))

        marked = await db_context.worker_tasks.mark_stale_tasks(
            submitted_timeout_hours=1
        )

        assert marked == 1
        task = await db_context.worker_tasks.get_task("stale-submitted")
        assert task is not None
        assert task["status"] == "failed"
        assert "stuck in submitted" in (task.get("error_message") or "")

    @pytest.mark.asyncio
    async def test_leaves_recent_submitted_tasks(self, db_context: Database) -> None:
        """Recently submitted tasks should not be marked stale."""
        await db_context.worker_tasks.create_task(
            task_id="fresh-submitted",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Fresh submitted",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="fresh-submitted", status="submitted"
        )

        marked = await db_context.worker_tasks.mark_stale_tasks(
            submitted_timeout_hours=1
        )

        assert marked == 0
        task = await db_context.worker_tasks.get_task("fresh-submitted")
        assert task is not None
        assert task["status"] == "submitted"

    @pytest.mark.asyncio
    async def test_marks_overtime_running_tasks(self, db_context: Database) -> None:
        """Running tasks that exceed timeout+buffer should be marked failed."""
        await db_context.worker_tasks.create_task(
            task_id="overtime-running",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Overtime running",
            timeout_minutes=10,
        )
        await db_context.worker_tasks.update_task_status(
            task_id="overtime-running", status="running"
        )
        # 60 min > 10 min timeout + 30 min buffer = 40 min
        await _backdate_task(db_context, "overtime-running", timedelta(minutes=60))

        marked = await db_context.worker_tasks.mark_stale_tasks(
            running_buffer_minutes=30
        )

        assert marked == 1
        task = await db_context.worker_tasks.get_task("overtime-running")
        assert task is not None
        assert task["status"] == "failed"
        assert "exceeded timeout" in (task.get("error_message") or "")


class TestAtomicLifecycleTransitions:
    """Tests for lifecycle updates that can race backend callbacks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status", ["running", "success", "failed", "timeout", "cancelled"]
    )
    async def test_record_submission_preserves_newer_status(
        self, db_context: Database, status: str
    ) -> None:
        task_id = f"submission-{status}"
        await db_context.worker_tasks.create_task(
            task_id=task_id,
            conversation_id="conv-1",
            interface_type="test",
            task_description="Fast lifecycle callback",
        )
        await db_context.worker_tasks.update_task_status(task_id=task_id, status=status)

        updated = await db_context.worker_tasks.record_task_submission(
            task_id=task_id, job_name="backend-job"
        )

        assert updated is True
        task = await db_context.worker_tasks.get_task(task_id)
        assert task is not None
        assert task["status"] == status
        assert task.get("job_name") == "backend-job"

    @pytest.mark.asyncio
    async def test_record_submission_transitions_pending_task(
        self, db_context: Database
    ) -> None:
        await db_context.worker_tasks.create_task(
            task_id="submission-pending",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Normal submission",
        )

        updated = await db_context.worker_tasks.record_task_submission(
            task_id="submission-pending", job_name="backend-job"
        )

        assert updated is True
        task = await db_context.worker_tasks.get_task("submission-pending")
        assert task is not None
        assert task["status"] == "submitted"
        assert task.get("job_name") == "backend-job"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["success", "failed", "timeout", "cancelled"])
    async def test_mark_running_preserves_terminal_status(
        self, db_context: Database, status: str
    ) -> None:
        task_id = f"late-start-{status}"
        await db_context.worker_tasks.create_task(
            task_id=task_id,
            conversation_id="conv-1",
            interface_type="test",
            task_description="Late start callback",
        )
        await db_context.worker_tasks.update_task_status(task_id=task_id, status=status)

        updated = await db_context.worker_tasks.mark_task_running(
            task_id=task_id, started_at=datetime.now(UTC)
        )

        assert updated is False
        task = await db_context.worker_tasks.get_task(task_id)
        assert task is not None
        assert task["status"] == status
        assert task.get("started_at") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["pending", "submitted"])
    async def test_mark_running_transitions_prestart_status(
        self, db_context: Database, status: str
    ) -> None:
        task_id = f"start-{status}"
        await db_context.worker_tasks.create_task(
            task_id=task_id,
            conversation_id="conv-1",
            interface_type="test",
            task_description="Start callback",
        )
        if status == "submitted":
            await db_context.worker_tasks.update_task_status(
                task_id=task_id, status=status
            )
        started_at = datetime.now(UTC)

        updated = await db_context.worker_tasks.mark_task_running(
            task_id=task_id, started_at=started_at
        )

        assert updated is True
        task = await db_context.worker_tasks.get_task(task_id)
        assert task is not None
        assert task["status"] == "running"
        stored_started_at = task.get("started_at")
        assert stored_started_at is not None
        parsed_started_at = datetime.fromisoformat(stored_started_at)
        if parsed_started_at.tzinfo is None:
            parsed_started_at = parsed_started_at.replace(tzinfo=UTC)
        assert parsed_started_at == started_at


class TestCleanupProtectsActive:
    """Tests for cleanup_old_tasks skipping active tasks."""

    @pytest.mark.asyncio
    async def test_cleanup_skips_submitted_tasks(self, db_context: Database) -> None:
        """Old tasks in 'submitted' status should not be deleted by cleanup."""
        # Create two old tasks: one submitted (active), one success (terminal)
        await db_context.worker_tasks.create_task(
            task_id="old-submitted",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Old submitted",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="old-submitted", status="submitted"
        )

        await db_context.worker_tasks.create_task(
            task_id="old-success",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Old success",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="old-success", status="success"
        )

        # Backdate both to 72 hours ago
        for tid in ("old-submitted", "old-success"):
            await _backdate_task(db_context, tid, timedelta(hours=72))

        deleted = await db_context.worker_tasks.cleanup_old_tasks(retention_hours=48)

        # Only the terminal task should be deleted
        assert deleted == 1

        # Submitted task should survive
        task = await db_context.worker_tasks.get_task("old-submitted")
        assert task is not None
        assert task["status"] == "submitted"

        # Success task should be gone
        task = await db_context.worker_tasks.get_task("old-success")
        assert task is None

    @pytest.mark.asyncio
    async def test_cleanup_skips_running_tasks(self, db_context: Database) -> None:
        """Old tasks in 'running' status should not be deleted by cleanup."""
        await db_context.worker_tasks.create_task(
            task_id="old-running",
            conversation_id="conv-1",
            interface_type="test",
            task_description="Old running",
        )
        await db_context.worker_tasks.update_task_status(
            task_id="old-running", status="running"
        )
        await _backdate_task(db_context, "old-running", timedelta(hours=72))

        deleted = await db_context.worker_tasks.cleanup_old_tasks(retention_hours=48)

        assert deleted == 0
        task = await db_context.worker_tasks.get_task("old-running")
        assert task is not None
        assert task["status"] == "running"
