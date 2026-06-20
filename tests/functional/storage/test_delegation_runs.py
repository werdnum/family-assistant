"""Functional tests for the delegation runs repository async-remote methods."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    async with DatabaseContext(engine=db_engine) as db_ctx:
        yield db_ctx


def _make_run(delegation_id: str, task_id: str) -> DelegationRunCreate:
    return DelegationRunCreate(
        delegation_id=delegation_id,
        task_id=task_id,
        source_profile_id="src",
        target_service_id="remote_profile",
        interface_type="web",
        conversation_id="conv-1",
        subconversation_id="sub-1",
        request_text="do something",
        content_parts_json=[],
    )


class TestDelegationRunsAsyncRemote:
    @pytest.mark.asyncio
    async def test_create_run_defaults(self, db_context: DatabaseContext) -> None:
        run = await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        assert run["status"] == "queued"
        assert run["remote_task_id"] is None
        assert run["remote_context_id"] is None
        assert run["poll_attempts"] == 0

    @pytest.mark.asyncio
    async def test_mark_awaiting_remote_from_running(
        self, db_context: DatabaseContext
    ) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        # The submit-then-poll path claims queued -> running before submitting.
        await db_context.delegation_runs.mark_running("d1", datetime.now(UTC))
        updated = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="remote-task-99",
            remote_context_id="remote-ctx-99",
            started_at=datetime.now(UTC),
        )
        assert updated is not None
        assert updated["status"] == "awaiting_remote"
        assert updated["remote_task_id"] == "remote-task-99"
        assert updated["remote_context_id"] == "remote-ctx-99"
        assert updated["started_at"] is not None

    @pytest.mark.asyncio
    async def test_mark_awaiting_remote_is_guarded_on_running(
        self, db_context: DatabaseContext
    ) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        # Still 'queued' (not claimed): the running-guarded transition is a no-op.
        not_claimed = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-0",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        assert not_claimed is None

        await db_context.delegation_runs.mark_running("d1", datetime.now(UTC))
        first = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-1",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        assert first is not None
        # A second attempt no longer matches status == 'running'.
        second = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-2",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        assert second is None

    @pytest.mark.asyncio
    async def test_bump_poll_attempt(self, db_context: DatabaseContext) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        now = datetime.now(UTC)
        assert await db_context.delegation_runs.bump_poll_attempt("d1", now) == 1
        assert await db_context.delegation_runs.bump_poll_attempt("d1", now) == 2
        assert (
            await db_context.delegation_runs.bump_poll_attempt("missing", now) is None
        )

    @pytest.mark.asyncio
    async def test_list_awaiting_remote(self, db_context: DatabaseContext) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        await db_context.delegation_runs.create_run(_make_run("d2", "t2"))
        await db_context.delegation_runs.mark_running("d1", datetime.now(UTC))
        await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-1",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        awaiting = await db_context.delegation_runs.list_awaiting_remote()
        ids = {run["delegation_id"] for run in awaiting}
        assert ids == {"d1"}
