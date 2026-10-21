"""Functional tests for the delegation runs repository async-remote methods."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.storage.repositories.delegation_runs import DelegationRunCreate


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    db_ctx = Database(engine=db_engine)
    yield db_ctx


def _make_run(delegation_id: str, task_id: str) -> DelegationRunCreate:
    # Each run gets its own subconversation, as a fresh delegation would: the
    # partial unique index forbids two non-terminal runs sharing a subconversation.
    return DelegationRunCreate(
        delegation_id=delegation_id,
        task_id=task_id,
        source_profile_id="src",
        target_service_id="remote_profile",
        interface_type="web",
        conversation_id="conv-1",
        subconversation_id=f"sub-{delegation_id}",
        request_text="do something",
        content_parts_json=[],
    )


class TestDelegationRunsAsyncRemote:
    @pytest.mark.asyncio
    async def test_create_run_defaults(self, db_context: Database) -> None:
        run = await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        assert run["status"] == "queued"
        assert run["remote_task_id"] is None
        assert run["remote_context_id"] is None
        assert run["poll_attempts"] == 0

    @pytest.mark.asyncio
    async def test_mark_awaiting_remote_from_queued(self, db_context: Database) -> None:
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
        self, db_context: Database
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
        self, db_context: Database
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
    async def test_bump_poll_attempt(self, db_context: Database) -> None:
        await db_context.delegation_runs.create_run(_make_run("d1", "t1"))
        now = datetime.now(UTC)
        assert await db_context.delegation_runs.bump_poll_attempt("d1", now) == 1
        assert await db_context.delegation_runs.bump_poll_attempt("d1", now) == 2
        assert (
            await db_context.delegation_runs.bump_poll_attempt("missing", now) is None
        )

    @pytest.mark.asyncio
    async def test_list_awaiting_remote(self, db_context: Database) -> None:
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

    @pytest.mark.asyncio
    async def test_active_subconversation_index_rejects_second_active_run(
        self, db_engine: AsyncEngine
    ) -> None:
        # The partial unique index atomically serializes resumes: at most one
        # non-terminal run may target a given subconversation, so two concurrent
        # resumes cannot both create active runs that interleave in one history.
        db_ctx = Database(engine=db_engine)
        await db_ctx.delegation_runs.create_run(_make_run("d1", "t1"))

        conflicting = _make_run("d2", "t2")
        conflicting["subconversation_id"] = "sub-d1"
        with pytest.raises(IntegrityError):
            db_ctx = Database(engine=db_engine)
            await db_ctx.delegation_runs.create_run(conflicting)

    @pytest.mark.asyncio
    async def test_active_subconversation_index_allows_run_after_terminal(
        self, db_engine: AsyncEngine
    ) -> None:
        # Once the prior run is terminal it leaves the partial index, so a resume
        # reusing its subconversation is permitted.
        db_ctx = Database(engine=db_engine)
        await db_ctx.delegation_runs.create_run(_make_run("d1", "t1"))
        await db_ctx.delegation_runs.mark_completed(
            delegation_id="d1",
            result_text="done",
            result_attachment_ids=[],
            completed_at=datetime.now(UTC),
        )

        db_ctx = Database(engine=db_engine)
        resume = _make_run("d2", "t2")
        resume["subconversation_id"] = "sub-d1"
        created = await db_ctx.delegation_runs.create_run(resume)
        assert created["subconversation_id"] == "sub-d1"


@pytest.mark.asyncio
async def test_a_resumed_run_does_not_inherit_its_predecessors_children(
    db_context: Database,
) -> None:
    """A resume shares its history, not the delegations the failed run left behind."""
    runs = db_context.delegation_runs
    first = await runs.create_run(_make_run("parent-1", "t-parent-1"))
    await runs.mark_failed(
        delegation_id="parent-1", error="reaped", completed_at=datetime.now(UTC)
    )
    stale_child = await runs.create_run({
        **_make_run("child-of-1", "t-child-1"),
        "source_subconversation_id": first["subconversation_id"],
    })
    await runs.mark_handed_off(stale_child["delegation_id"], datetime.now(UTC))
    resumed = await runs.create_run({
        **_make_run("parent-2", "t-parent-2"),
        "subconversation_id": first["subconversation_id"],
    })
    own_child = await runs.create_run({
        **_make_run("child-of-2", "t-child-2"),
        "source_subconversation_id": first["subconversation_id"],
    })
    await runs.mark_handed_off(own_child["delegation_id"], datetime.now(UTC))

    children = await runs.list_undelivered_children(resumed)

    assert [child["delegation_id"] for child in children] == ["child-of-2"]


class TestLatestChainableRun:
    """Which earlier run in a lineage a resume chains onto."""

    @staticmethod
    async def _lineage_run(
        db_context: Database,
        delegation_id: str,
        *,
        remote_task_id: str | None,
        outcome: str,
        at: datetime,
    ) -> None:
        run = _make_run(delegation_id, f"task-{delegation_id}")
        run["subconversation_id"] = "sub-lineage"
        await db_context.delegation_runs.create_run(run)
        if remote_task_id is not None:
            await db_context.delegation_runs.mark_awaiting_remote(
                delegation_id,
                remote_task_id=remote_task_id,
                remote_context_id=None,
                started_at=at,
            )
        if outcome == "completed":
            await db_context.delegation_runs.mark_completed(
                delegation_id=delegation_id,
                result_text="done",
                result_attachment_ids=[],
                completed_at=at,
            )
        else:
            await db_context.delegation_runs.mark_failed(
                delegation_id=delegation_id, error="boom", completed_at=at
            )

    async def _latest(
        self, db_context: Database, *, include_failed: bool
    ) -> str | None:
        run = await db_context.delegation_runs.get_latest_chainable_run(
            conversation_id="conv-1",
            subconversation_id="sub-lineage",
            target_service_id="remote_profile",
            include_failed=include_failed,
        )
        return run["remote_task_id"] if run else None

    @pytest.mark.asyncio
    async def test_a_sandboxed_resume_chains_onto_a_failed_run(
        self, db_context: Database
    ) -> None:
        """The 21 Oct case: a run failed on our side after real remote work."""
        await self._lineage_run(
            db_context,
            "d-done",
            remote_task_id="inter-1",
            outcome="completed",
            at=datetime(2026, 10, 21, 0, 0, tzinfo=UTC),
        )
        await self._lineage_run(
            db_context,
            "d-blip",
            remote_task_id="inter-2",
            outcome="failed",
            at=datetime(2026, 10, 21, 0, 46, tzinfo=UTC),
        )

        assert await self._latest(db_context, include_failed=True) == "inter-2"
        assert await self._latest(db_context, include_failed=False) == "inter-1"

    @pytest.mark.asyncio
    async def test_a_run_that_never_reached_the_provider_is_skipped(
        self, db_context: Database
    ) -> None:
        await self._lineage_run(
            db_context,
            "d-done",
            remote_task_id="inter-1",
            outcome="completed",
            at=datetime(2026, 10, 21, 0, 0, tzinfo=UTC),
        )
        await self._lineage_run(
            db_context,
            "d-unsent",
            remote_task_id=None,
            outcome="failed",
            at=datetime(2026, 10, 21, 0, 46, tzinfo=UTC),
        )

        assert await self._latest(db_context, include_failed=True) == "inter-1"
