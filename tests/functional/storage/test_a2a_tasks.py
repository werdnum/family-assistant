"""Functional tests for A2A tasks repository."""

import asyncio
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
    async def test_create_task_if_absent_returns_existing(
        self, db_context: DatabaseContext
    ) -> None:
        created = await db_context.a2a_tasks.create_task_if_absent(
            task_id="dedupe-task",
            profile_id="profile-a",
            conversation_id="conv-1",
            context_id="ctx-1",
            status="working",
        )
        assert created is None

        existing = await db_context.a2a_tasks.create_task_if_absent(
            task_id="dedupe-task",
            profile_id="profile-b",
            conversation_id="conv-2",
            context_id="ctx-2",
            status="working",
        )
        assert existing is not None
        assert existing["task_id"] == "dedupe-task"
        assert existing["profile_id"] == "profile-a"
        assert existing["conversation_id"] == "conv-1"

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_create_task_if_absent_handles_concurrent_duplicates(
        self, db_engine: AsyncEngine
    ) -> None:
        async def create_one(profile_id: str) -> object:
            async with DatabaseContext(engine=db_engine) as db_context:
                return await db_context.a2a_tasks.create_task_if_absent(
                    task_id="concurrent-dedupe-task",
                    profile_id=profile_id,
                    conversation_id=f"conv-{profile_id}",
                    context_id="ctx-1",
                    status="working",
                )

        results = await asyncio.gather(create_one("profile-a"), create_one("profile-b"))
        assert sum(result is None for result in results) == 1

        async with DatabaseContext(engine=db_engine) as db_context:
            row = await db_context.a2a_tasks.get_task("concurrent-dedupe-task")

        assert row is not None
        assert row["profile_id"] in {"profile-a", "profile-b"}

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

    @pytest.mark.asyncio
    async def test_terminal_status_is_not_overwritten(
        self, db_context: DatabaseContext
    ) -> None:
        """A 'failed' task (e.g. reaped) must not be flipped back by a late send."""
        await db_context.a2a_tasks.create_task(
            task_id="reaped", profile_id="p", conversation_id="c", status="working"
        )
        await db_context.a2a_tasks.update_task_status("reaped", status="failed")
        # A background send finishing after the reaper must not resurrect it.
        updated = await db_context.a2a_tasks.update_task_status(
            "reaped", status="completed", artifacts_json=[{"name": "late", "parts": []}]
        )
        assert updated is False
        row = await db_context.a2a_tasks.get_task("reaped")
        assert row is not None
        assert row["status"] == "failed"
