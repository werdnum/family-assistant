"""A completed run's output files reach the user with its reply.

Drives the real worker handlers -- the terminal poll and late-completion
recovery -- against a fake target that also collects outputs, so what is
pinned is the worker's wiring: both paths ask for the files, deliver them with
the text, and say so when some could not be returned. What a real provider
leaves in a sandbox, and how it is read back, is covered by the replayed
integration test of the shipped ``coder`` profile.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, TypedDict

import pytest

from family_assistant.processing import (
    CollectedOutputs,
    DelegationPermanentError,
    DelegationTransientError,
    RemoteDisposition,
)
from family_assistant.storage.database import Database
from family_assistant.utils.clock import SystemClock
from tests.functional.automations.test_async_delegation import (
    TEST_CONVERSATION_ID,
    TEST_INTERFACE_TYPE,
    TEST_USER_NAME,
    _create_run,
    _tool_context,
)
from tests.functional.automations.test_delegation_reconciliation import (
    REMOTE_TASK_ID,
    FakeObservableService,
    _failed_run,
    _observation,
    _reconcile_payload,
    _worker_for,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing.protocol import RemoteObservation
    from family_assistant.security.taint import TurnTaintState


class _CollectCall(TypedDict):
    environment_id: str | None
    owner_user_id: str | None
    conversation_id: str
    taint_state: TurnTaintState


class FakeCollectingService(FakeObservableService):
    """An observable target whose finished runs leave files behind."""

    def __init__(
        self,
        observations: list[RemoteObservation | BaseException],
        outputs: CollectedOutputs | BaseException,
    ) -> None:
        super().__init__(observations)
        self._outputs = outputs
        self.collect_calls: list[_CollectCall] = []

    async def collect_outputs(
        self,
        observation: RemoteObservation,
        *,
        owner_user_id: str | None,
        conversation_id: str,
        taint_state: TurnTaintState,
        db_context: Database,
    ) -> CollectedOutputs:
        _ = db_context
        self.collect_calls.append({
            "environment_id": observation.environment_id,
            "owner_user_id": owner_user_id,
            "conversation_id": conversation_id,
            "taint_state": taint_state,
        })
        if isinstance(self._outputs, BaseException):
            raise self._outputs
        return self._outputs


def _completed() -> RemoteObservation:
    return replace(
        _observation(
            RemoteDisposition.COMPLETED, output_text="Both bookmarks are done."
        ),
        environment_id="env-bookmarks",
    )


async def _awaiting_run(db_context: Database, delegation_id: str) -> None:
    await _create_run(db_context, delegation_id=delegation_id)
    await db_context.delegation_runs.mark_awaiting_remote(
        delegation_id,
        remote_task_id=REMOTE_TASK_ID,
        remote_context_id=None,
        started_at=SystemClock().now(),
    )


async def _poll(
    db_engine: AsyncEngine, target: FakeCollectingService, delegation_id: str
) -> Database:
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    db_context = Database(engine=db_engine)
    await _awaiting_run(db_context, delegation_id)
    await worker.handle_delegation_poll(
        _tool_context(db_context, processing_service, chat_interface),
        {
            "delegation_id": delegation_id,
            "interface_type": TEST_INTERFACE_TYPE,
            "conversation_id": TEST_CONVERSATION_ID,
            "user_name": TEST_USER_NAME,
        },
    )
    return db_context


@pytest.mark.asyncio
async def test_a_completed_poll_delivers_the_runs_output_files(
    db_engine: AsyncEngine,
) -> None:
    """The files are collected for the run's own user and conversation."""
    target = FakeCollectingService(
        [_completed()],
        CollectedOutputs(attachment_ids=["att-stl", "att-png"], omitted=[]),
    )

    db_context = await _poll(db_engine, target, "delegation_outputs")

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_outputs")
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] == "Both bookmarks are done."
    assert run["result_attachment_ids_json"] == ["att-stl", "att-png"]
    assert len(target.collect_calls) == 1
    call = target.collect_calls[0]
    assert call["environment_id"] == "env-bookmarks"
    assert call["owner_user_id"] == "async-delegation-user"
    assert call["conversation_id"] == TEST_CONVERSATION_ID


@pytest.mark.asyncio
async def test_a_late_completion_delivers_the_runs_output_files(
    db_engine: AsyncEngine,
) -> None:
    """A run that finished after we gave up on it loses its files no more
    than its text."""
    target = FakeCollectingService(
        [_completed()], CollectedOutputs(attachment_ids=["att-late"], omitted=[])
    )
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_late_outputs")
    db_context = Database(engine=db_engine)

    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_late_outputs"),
    )

    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_late_outputs"
    )
    assert run is not None
    assert run["late_recovered_at"] is not None
    assert run["result_attachment_ids_json"] == ["att-late"]


@pytest.mark.asyncio
async def test_files_that_were_not_returned_are_named_in_the_reply(
    db_engine: AsyncEngine,
) -> None:
    target = FakeCollectingService(
        [_completed()],
        CollectedOutputs(
            attachment_ids=["att-small"],
            omitted=["huge.glb (80,000,000 bytes, over the 50 MB a run returns)"],
        ),
    )

    db_context = await _poll(db_engine, target, "delegation_omitted")

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_omitted")
    assert run is not None
    assert run["result_attachment_ids_json"] == ["att-small"]
    assert run["result_text"] is not None
    assert "Both bookmarks are done." in run["result_text"]
    assert "huge.glb" in run["result_text"]


@pytest.mark.asyncio
async def test_a_transient_failure_fetching_files_reads_the_run_again(
    db_engine: AsyncEngine,
) -> None:
    """Delivering the text without the files would lose them for good."""
    target = FakeCollectingService(
        [_completed()], DelegationTransientError("connection reset")
    )

    db_context = await _poll(db_engine, target, "delegation_fetch_blip")

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_fetch_blip")
    assert run is not None
    assert run["status"] == "awaiting_remote"


@pytest.mark.asyncio
async def test_a_permanent_failure_fetching_files_still_delivers_the_text(
    db_engine: AsyncEngine,
) -> None:
    target = FakeCollectingService(
        [_completed()], DelegationPermanentError("403 permission denied")
    )

    db_context = await _poll(db_engine, target, "delegation_fetch_denied")

    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_fetch_denied"
    )
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] is not None
    assert "Both bookmarks are done." in run["result_text"]
    assert "output files could not be fetched" in run["result_text"]
    assert not run["result_attachment_ids_json"]
