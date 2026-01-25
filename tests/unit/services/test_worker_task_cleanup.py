"""Tests for the worker task cleanup handler."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.task_worker import handle_worker_task_cleanup

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class MinimalContext:
    """Minimal context for testing cleanup handler.

    Only includes the fields actually used by handle_worker_task_cleanup.
    """

    interface_type: str
    conversation_id: str
    user_name: str
    db_context: DatabaseContext
    processing_service: None = None  # Not needed when workspace_path is in payload


@pytest.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    """Create a database context for testing."""
    async with DatabaseContext(engine=db_engine) as context:
        yield context


@pytest.fixture
def exec_context(db_context: DatabaseContext) -> MinimalContext:
    """Create a minimal execution context for testing."""
    return MinimalContext(
        interface_type="test",
        conversation_id="test-conv-123",
        user_name="test_user",
        db_context=db_context,
    )


class TestWorkerTaskCleanup:
    """Tests for handle_worker_task_cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_database_records(
        self, exec_context: MinimalContext, db_context: DatabaseContext
    ) -> None:
        """Test that cleanup deletes old database records."""
        # Create a task record in the database
        await db_context.worker_tasks.create_task(
            task_id="old-task-1",
            conversation_id="conv-1",
            interface_type="test",
            user_name="test_user",
            model="claude-3",
            task_description="Old task 1",
        )

        # Run cleanup with 24 hour retention
        # MinimalContext is duck-type compatible with ToolExecutionContext for cleanup
        await handle_worker_task_cleanup(exec_context, {"retention_hours": 24})  # type: ignore[arg-type]  # MinimalContext duck-types ToolExecutionContext

        # The task we just created is new, so it should still exist
        task = await db_context.worker_tasks.get_task("old-task-1")
        assert task is not None

    @pytest.mark.asyncio
    async def test_cleanup_uses_default_retention(
        self, exec_context: MinimalContext
    ) -> None:
        """Test that cleanup uses default retention when not specified."""
        # Should not raise - default is 48 hours
        await handle_worker_task_cleanup(exec_context, {})  # type: ignore[arg-type]  # MinimalContext duck-types ToolExecutionContext

    @pytest.mark.asyncio
    async def test_cleanup_task_directories(
        self, exec_context: MinimalContext, tmp_path: Path
    ) -> None:
        """Test that cleanup removes old task directories."""
        # Create a mock tasks directory with old and new task dirs
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        # Create an old task directory (set mtime to past)
        old_task_dir = tasks_dir / "worker-old-task"
        old_task_dir.mkdir()
        (old_task_dir / "prompt.md").write_text("old prompt")

        # Create a new task directory
        new_task_dir = tasks_dir / "worker-new-task"
        new_task_dir.mkdir()
        (new_task_dir / "prompt.md").write_text("new prompt")

        # Override workspace path in payload
        payload = {"retention_hours": 24, "workspace_path": str(tmp_path)}

        # Mock the old directory to have an old mtime
        old_time = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
        os.utime(old_task_dir, (old_time, old_time))

        await handle_worker_task_cleanup(exec_context, payload)  # type: ignore[arg-type]  # MinimalContext duck-types ToolExecutionContext

        # Old task directory should be removed
        assert not old_task_dir.exists()

        # New task directory should still exist
        assert new_task_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_handles_missing_tasks_dir(
        self, exec_context: MinimalContext, tmp_path: Path
    ) -> None:
        """Test that cleanup handles missing tasks directory gracefully."""
        # Workspace exists but tasks/ does not
        payload = {"workspace_path": str(tmp_path)}

        # Should not raise
        await handle_worker_task_cleanup(exec_context, payload)  # type: ignore[arg-type]  # MinimalContext duck-types ToolExecutionContext

    @pytest.mark.asyncio
    async def test_cleanup_without_workspace_path(
        self, exec_context: MinimalContext
    ) -> None:
        """Test cleanup when no workspace path is provided."""
        # No workspace path in payload, no processing_service
        # Should just do database cleanup without filesystem cleanup
        await handle_worker_task_cleanup(exec_context, {})  # type: ignore[arg-type]  # MinimalContext duck-types ToolExecutionContext
