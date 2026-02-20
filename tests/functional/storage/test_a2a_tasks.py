"""Functional tests for A2A tasks repository."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    async with DatabaseContext(engine=db_engine) as db_ctx:
        yield db_ctx


class TestA2ATasksRepository:
    @pytest.mark.asyncio
    async def test_create_and_get_task(self, db_context: DatabaseContext) -> None:
        await db_context.a2a_tasks.create_task(
            task_id="test-1",
            profile_id="profile-a",
            conversation_id="conv-1",
            context_id="ctx-1",
            status="working",
            history_json=[{"role": "user", "parts": [{"type": "text", "text": "hi"}]}],
        )
        row = await db_context.a2a_tasks.get_task("test-1")
        assert row is not None
        assert row["status"] == "working"
        history = row.get("history_json")
        assert history is not None
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_cancel_does_not_get_overwritten(
        self, db_context: DatabaseContext
    ) -> None:
        """Once a task is canceled, update_task_status must not overwrite the status."""
        await db_context.a2a_tasks.create_task(
            task_id="race-test",
            profile_id="profile-a",
            conversation_id="conv-1",
            status="working",
        )

        canceled = await db_context.a2a_tasks.cancel_task("race-test")
        assert canceled is True

        row = await db_context.a2a_tasks.get_task("race-test")
        assert row is not None
        assert row["status"] == "canceled"

        # Simulate the streaming generator finishing after cancel —
        # update_task_status should NOT overwrite the canceled status
        updated = await db_context.a2a_tasks.update_task_status(
            task_id="race-test",
            status="completed",
            artifacts_json=[{"name": "response", "parts": []}],
        )
        assert updated is False

        row = await db_context.a2a_tasks.get_task("race-test")
        assert row is not None
        assert row["status"] == "canceled"
