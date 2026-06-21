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
    async def test_mark_awaiting_remote_from_queued(
        self, db_context: DatabaseContext
    ) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        # The submit-then-poll path claims queued -> awaiting_remote (with the
        # pre-generated remote id) before submitting.
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
    async def test_mark_awaiting_remote_is_guarded_on_queued(
        self, db_context: DatabaseContext
    ) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        first = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-1",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        assert first is not None
        # A second attempt no longer matches status == 'queued'.
        second = await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-2",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        assert second is None

    @pytest.mark.asyncio
    async def test_terminal_transition_is_conditional_on_non_terminal(
        self, db_context: DatabaseContext
    ) -> None:
        # mark_completed/mark_failed are atomic CAS on non-terminal status, so a
        # second finalizer (a racing reaper/poll) cannot clobber the result.
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        completed = await db_context.delegation_runs.mark_completed(
            delegation_id="d1",
            result_text="done",
            result_attachment_ids=[],
            completed_at=datetime.now(UTC),
        )
        assert completed is not None
        assert completed["status"] == "completed"

        # A subsequent fail on the already-completed run loses the race.
        not_failed = await db_context.delegation_runs.mark_failed(
            delegation_id="d1", error="timed out", completed_at=datetime.now(UTC)
        )
        assert not_failed is None
        run = await db_context.delegation_runs.get_by_delegation_id("d1")
        assert run is not None
        assert run["status"] == "completed"
        assert run["result_text"] == "done"

        # And a fail wins on a still-non-terminal run.
        await db_context.delegation_runs.create_run(_make_run("d2", "t2"))
        failed = await db_context.delegation_runs.mark_failed(
            delegation_id="d2", error="boom", completed_at=datetime.now(UTC)
        )
        assert failed is not None
        assert failed["status"] == "failed"

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
        await db_context.delegation_runs.mark_awaiting_remote(
            "d1",
            remote_task_id="rt-1",
            remote_context_id=None,
            started_at=datetime.now(UTC),
        )
        awaiting = await db_context.delegation_runs.list_awaiting_remote()
        ids = {run["delegation_id"] for run in awaiting}
        assert ids == {"d1"}
