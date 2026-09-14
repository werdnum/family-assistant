"""Tests for reconciling locally failed delegations against late remote state.

The premise of these tests is the one the production audit established: a
provider's account of a run changes after Family Assistant has stopped
listening. A run reported ``cancelled`` comes back ``completed``; a run we
timed out on keeps going and produces a result; a run that says ``completed``
carries nothing at all. Each test below drives the real worker handlers
against a fake whose readings change between reads, exactly as the provider's
did.

See ``docs/design/delegation-remote-reconciliation.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from family_assistant.processing import (
    PENDING,
    ChatInteractionResult,
    DelegationTaskNotFoundError,
    DelegationTransientError,
    ObservableDelegationService,
    RemoteDisposition,
    RemoteObservation,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.storage import message_history_table
from family_assistant.storage.database import Database
from family_assistant.task_worker import (
    DELEGATION_RECONCILE_MAX_ATTEMPTS,
    DELEGATION_RECONCILE_TASK_TYPE,
    DelegationReconcilePayload,
)
from family_assistant.utils.clock import SystemClock

# The delegation test fixtures are shared rather than duplicated: these tests
# exercise the same worker against the same source profile, and a second copy
# of the scaffolding would be free to drift away from the behaviour the other
# file pins.
from tests.functional.automations.test_async_delegation import (
    TEST_CONVERSATION_ID,
    TEST_INTERFACE_TYPE,
    TEST_USER_NAME,
    FakeDelegatableService,
    FakePollableService,
    _build_worker,
    _create_run,
    _source_processing_service,
    _tool_context,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.processing.protocol import PendingPoll
    from family_assistant.processing.service import ProcessingService
    from family_assistant.storage.delegation_runs import DelegationLocalFailureKind
    from family_assistant.task_worker import TaskWorker

REMOTE_TASK_ID = "srv-1"


def _observation(
    disposition: RemoteDisposition,
    *,
    status: str | None = None,
    output_text: str | None = None,
    observed_at: datetime | None = None,
    step_count: int | None = None,
    total_tokens: int | None = None,
) -> RemoteObservation:
    """One classified reading, as a provider adapter would build it."""
    clock = SystemClock()
    return RemoteObservation(
        remote_task_id=REMOTE_TASK_ID,
        status=status or disposition.value,
        disposition=disposition,
        observed_at=observed_at or clock.now(),
        result=(
            ChatInteractionResult.success(text_reply=output_text or "")
            if disposition is RemoteDisposition.COMPLETED
            else None
        ),
        output_chars=len(output_text or ""),
        step_count=step_count,
        total_tokens=total_tokens,
        resolved_model="antigravity-preview-05-2026",
    )


class FakeObservableService(FakePollableService):
    """A pollable target that can also be re-read after we gave up on it.

    Reads come from a scripted sequence, so a test can make the provider say
    ``cancelled`` and then ``completed`` -- the sequence that lost real work.
    The last reading repeats once the script runs out, which is what a settled
    provider does.
    """

    def __init__(self, observations: list[RemoteObservation | BaseException]) -> None:
        super().__init__()
        self._observations = list(observations)
        self.observe_calls: list[str] = []

    async def observe_async(self, remote_task_id: str) -> RemoteObservation:
        self.observe_calls.append(remote_task_id)
        item = (
            self._observations.pop(0)
            if len(self._observations) > 1
            else self._observations[0]
        )
        if isinstance(item, BaseException):
            raise item
        return item

    def result_for_observation(
        self, observation: RemoteObservation
    ) -> ChatInteractionResult | PendingPoll:
        if observation.disposition is RemoteDisposition.PENDING:
            return PENDING
        if observation.result is not None:
            return observation.result
        return ChatInteractionResult.error(
            text_reply=f"The run {observation.status}.",
            error_traceback=f"Remote run ended {observation.status!r}.",
        )


def _reconcile_payload(delegation_id: str) -> DelegationReconcilePayload:
    return DelegationReconcilePayload(
        delegation_id=delegation_id,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=TEST_CONVERSATION_ID,
        user_name=TEST_USER_NAME,
    )


async def _failed_run(
    db_engine: AsyncEngine,
    delegation_id: str,
    *,
    local_failure_kind: DelegationLocalFailureKind = "remote_status",
    taint_state_json: TaintMetadata | None = None,
) -> None:
    """A run this application has already failed, with a remote id to re-read."""
    db_context = Database(engine=db_engine)
    await _create_run(
        db_context,
        delegation_id=delegation_id,
        taint_state_json=taint_state_json,
    )
    await db_context.delegation_runs.update_remote_task(
        delegation_id, remote_task_id=REMOTE_TASK_ID, remote_context_id=None
    )
    await db_context.delegation_runs.mark_failed(
        delegation_id=delegation_id,
        error="The target_profile run cancelled.",
        completed_at=SystemClock().now(),
        local_failure_kind=local_failure_kind,
    )
    await db_context.delegation_runs.mark_notified(
        delegation_id=delegation_id,
        result_message_internal_id=None,
        notified_at=SystemClock().now(),
    )


def _worker_for(
    db_engine: AsyncEngine, target: FakeObservableService
) -> tuple[TaskWorker, ProcessingService, AsyncMock]:
    processing_service = _source_processing_service(
        cast("FakeDelegatableService", target)
    )
    chat_interface = AsyncMock()
    chat_interface.send_message.return_value = "external_message_id"
    worker = _build_worker(db_engine, processing_service, chat_interface)
    return worker, processing_service, chat_interface


@pytest.mark.asyncio
async def test_a_cancelled_run_that_later_completes_is_recovered(
    db_engine: AsyncEngine,
) -> None:
    """The exact sequence that lost real work: cancelled, then completed.

    The run was failed on a provider ``cancelled``; a later read returns
    ``completed`` with a substantial result. That result must reach the user
    rather than be discarded because we had already made up our mind.
    """
    target = FakeObservableService([
        _observation(
            RemoteDisposition.COMPLETED,
            output_text="Here is the work you asked for.",
            step_count=342,
        )
    ])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_late_success")

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_late_success"),
    )

    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_late_success"
    )
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] == "Here is the work you asked for."
    assert run["late_recovered_at"] is not None
    # The failure is annotated, not rewritten: what we told the user at the
    # time is still readable.
    assert run["error"] == "The target_profile run cancelled."
    assert run["local_failure_kind"] == "remote_status"
    # The bounded observation is persisted, and carries no output text.
    observation = run["remote_observation_json"]
    assert observation is not None
    assert observation["has_output"] is True
    assert observation["output_chars"] == len("Here is the work you asked for.")
    assert observation["step_count"] == 342
    assert "Here is the work" not in str(observation)
    chat_interface.send_message.assert_awaited_once()
    assert (
        "Here is the work you asked for."
        in chat_interface.send_message.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_a_late_result_says_that_it_is_late(
    db_engine: AsyncEngine,
) -> None:
    """A late result accounts for the failure it reverses, without overclaiming.

    A message that simply announces a result contradicts a failure notice the
    requester may be holding. It says the run failed earlier rather than that
    they were told so: the failure notice can itself have failed to deliver,
    and this has no way to know. The run's status summary carries the same
    correction, so an assistant asked about the delegation can explain the
    reversal rather than just change its answer.
    """
    target = FakeObservableService([
        _observation(RemoteDisposition.COMPLETED, output_text="the finished work")
    ])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_marked_late")

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_marked_late"),
    )

    text = chat_interface.send_message.await_args.kwargs["text"]
    assert "failed earlier" in text
    assert "the finished work" in text

    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_marked_late"
    )
    assert run is not None
    summary = db_context.delegation_runs.summarize_run(run)
    assert summary["status"] == "completed"
    assert summary.get("late_recovered_at") is not None
    assert summary.get("original_error") == "The target_profile run cancelled."


@pytest.mark.asyncio
async def test_racing_reconcilers_recover_a_late_result_once(
    db_engine: AsyncEngine,
) -> None:
    """Two reconcilers racing the same late completion recover it once.

    The guarantee is on the recovery transition rather than the scheduler, so
    it has to hold when the same run is reconciled twice with no coordination
    between the attempts. It is a guarantee about recovery, not about
    delivery: terminal delivery sends before recording ``notified_at``, so a
    crash in that window re-sends -- a property of the existing delivery
    protocol that a late result inherits like any other terminal result.
    """
    target = FakeObservableService([
        _observation(RemoteDisposition.COMPLETED, output_text="late result")
    ])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_once")

    for _ in range(2):
        db_context = Database(engine=db_engine)
        await worker.handle_delegation_reconcile(
            _tool_context(db_context, processing_service, chat_interface),
            _reconcile_payload("delegation_once"),
        )

    chat_interface.send_message.assert_awaited_once()
    db_context = Database(engine=db_engine)
    rows = await db_context.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.role == "assistant")
    )
    assert len(rows) == 1
    # The second pass found a settled run and did not even read the provider.
    assert len(target.observe_calls) == 1


@pytest.mark.asyncio
async def test_a_recovered_result_carries_the_runs_own_taint(
    db_engine: AsyncEngine,
) -> None:
    """A late result goes out through the ordinary delivery path, taint and all.

    Recovering by writing the result straight to the interface would bypass
    the labelling every other delegated result gets; the point of routing
    recovery through the normal terminal delivery is that it cannot.
    """
    tainted = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.SANDBOX_OUTPUT,
            source_id="coder_sandbox",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="The delegated run read the open web in its sandbox.",
        )
    )
    target = FakeObservableService([
        _observation(RemoteDisposition.COMPLETED, output_text="late tainted result")
    ])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(
        db_engine, "delegation_tainted", taint_state_json=tainted.to_metadata()
    )

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_tainted"),
    )

    chat_interface.send_message.assert_awaited_once()
    _, send_kwargs = chat_interface.send_message.await_args
    assert send_kwargs["taint_metadata"]["max_tier"] == "unknown_external"
    rows = await db_context.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CONVERSATION_ID)
        .where(message_history_table.c.role == "assistant")
    )
    assert len(rows) == 1
    assert rows[0]["taint_metadata_json"]["max_tier"] == "unknown_external"


@pytest.mark.asyncio
async def test_a_run_still_in_progress_is_read_again_and_not_resurrected(
    db_engine: AsyncEngine,
) -> None:
    """A failed run the provider says is still running stays failed, and is re-read.

    This is the reading that disproves the monotonic-status assumption: four
    of the audited runs went back to ``in_progress`` after being seen
    ``cancelled``. It is not proof of anything yet, so the run keeps its
    terminal disposition and earns another read.
    """
    target = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_in_progress")

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_in_progress"),
    )

    run = await db_context.delegation_runs.get_by_delegation_id(
        "delegation_in_progress"
    )
    assert run is not None
    assert run["status"] == "failed"
    assert run["reconciled_at"] is None
    assert run["reconcile_attempts"] == 1
    assert run["remote_status"] == "pending"
    chat_interface.send_message.assert_not_awaited()
    # Reconciliation is a re-read, never a mutation.
    assert target.cancelled == []
    pending = await db_context.tasks.get_all(
        task_type=DELEGATION_RECONCILE_TASK_TYPE, status="pending", limit=10
    )
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_reconciliation_stops_at_its_attempt_bound(
    db_engine: AsyncEngine,
) -> None:
    """A run that never settles is abandoned rather than re-read forever."""
    target = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_bounded")

    for _ in range(DELEGATION_RECONCILE_MAX_ATTEMPTS):
        db_context = Database(engine=db_engine)
        await worker.handle_delegation_reconcile(
            _tool_context(db_context, processing_service, chat_interface),
            _reconcile_payload("delegation_bounded"),
        )

    db_context = Database(engine=db_engine)
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_bounded")
    assert run is not None
    assert run["reconcile_attempts"] == DELEGATION_RECONCILE_MAX_ATTEMPTS
    assert run["reconciled_at"] is not None

    # A settled run is not read again, whatever the provider would now say.
    reads_before = len(target.observe_calls)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_bounded"),
    )
    assert len(target.observe_calls) == reads_before


@pytest.mark.asyncio
async def test_a_run_the_provider_has_forgotten_is_settled(
    db_engine: AsyncEngine,
) -> None:
    """Nothing left to learn: settle rather than spend the remaining reads."""
    target = FakeObservableService([DelegationTaskNotFoundError("no such run")])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_forgotten")

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_forgotten"),
    )

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_forgotten")
    assert run is not None
    assert run["reconciled_at"] is not None
    assert run["status"] == "failed"


@pytest.mark.asyncio
async def test_a_transient_read_failure_earns_another_read(
    db_engine: AsyncEngine,
) -> None:
    """A provider that is briefly unavailable does not end reconciliation."""
    target = FakeObservableService([
        DelegationTransientError("the provider is unavailable"),
        _observation(RemoteDisposition.COMPLETED, output_text="recovered anyway"),
    ])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_flaky")

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_flaky"),
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_flaky")
    assert run is not None
    assert run["reconciled_at"] is None

    db_context = Database(engine=db_engine)
    await worker.handle_delegation_reconcile(
        _tool_context(db_context, processing_service, chat_interface),
        _reconcile_payload("delegation_flaky"),
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_flaky")
    assert run is not None
    assert run["status"] == "completed"
    assert run["result_text"] == "recovered anyway"


@pytest.mark.asyncio
async def test_the_sweep_starts_reconciliation_without_multiplying_it(
    db_engine: AsyncEngine,
) -> None:
    """Repeated sweeps over the same eligible run enqueue one task, not one each.

    The sweep exists so a reconciliation lost to a crash is picked back up;
    that is only safe if running it again over a run that already has a live
    task is a no-op.
    """
    target = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    await _failed_run(db_engine, "delegation_swept")

    db_context = Database(engine=db_engine)
    context = _tool_context(db_context, processing_service, chat_interface)
    now = SystemClock().now()
    for _ in range(3):
        await worker._reconcile_lost_runs(context, now=now)

    pending = await db_context.tasks.get_all(
        task_type=DELEGATION_RECONCILE_TASK_TYPE, status="pending", limit=10
    )
    assert len(pending) == 1
    payload = pending[0]["payload"]
    assert payload is not None
    assert payload["delegation_id"] == "delegation_swept"


@pytest.mark.asyncio
async def test_the_sweep_ignores_runs_with_nothing_to_re_read(
    db_engine: AsyncEngine,
) -> None:
    """A run that never reached a provider is not reconciliation's business."""
    target = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_stranded")
    await db_context.delegation_runs.mark_failed(
        delegation_id="delegation_stranded",
        error="The delegated run was interrupted.",
        completed_at=SystemClock().now(),
        local_failure_kind="stranded",
    )

    context = _tool_context(db_context, processing_service, chat_interface)
    await worker._reconcile_lost_runs(context, now=SystemClock().now())

    pending = await db_context.tasks.get_all(
        task_type=DELEGATION_RECONCILE_TASK_TYPE, status="pending", limit=10
    )
    assert pending == []


@pytest.mark.asyncio
async def test_a_stale_observation_does_not_overwrite_a_newer_one(
    db_engine: AsyncEngine,
) -> None:
    """Out-of-order reads are ordinary; the older one must lose.

    Polls, reconciliation tasks and the sweep all read the same run, and a
    slow read returning after a fast one would otherwise roll the record back
    to a state the provider has already left.
    """
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_stale")
    now = SystemClock().now()
    newer = _observation(
        RemoteDisposition.COMPLETED, output_text="fresh", observed_at=now
    )
    older = _observation(
        RemoteDisposition.PENDING,
        status="in_progress",
        observed_at=now - timedelta(minutes=5),
    )

    written = await db_context.delegation_runs.record_remote_observation(
        "delegation_stale",
        observation=newer.to_metadata(),
        observed_at=newer.observed_at,
        remote_status=newer.status,
        cancel_confirmed=False,
    )
    assert written is not None
    rejected = await db_context.delegation_runs.record_remote_observation(
        "delegation_stale",
        observation=older.to_metadata(),
        observed_at=older.observed_at,
        remote_status=older.status,
        cancel_confirmed=False,
    )
    assert rejected is None

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_stale")
    assert run is not None
    assert run["remote_status"] == "completed"


@pytest.mark.asyncio
async def test_cancellation_is_only_claimed_once_the_provider_confirms_it(
    db_engine: AsyncEngine,
) -> None:
    """Asking to cancel is not the same fact as the provider having cancelled."""
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_cancel")
    now = SystemClock().now()

    await db_context.delegation_runs.mark_cancel_requested("delegation_cancel", now=now)
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_cancel")
    assert run is not None
    assert run["cancel_requested_at"] is not None
    assert run["cancel_confirmed_at"] is None

    # A reading that still shows the run going does not confirm anything.
    still_running = _observation(RemoteDisposition.PENDING, status="in_progress")
    await db_context.delegation_runs.record_remote_observation(
        "delegation_cancel",
        observation=still_running.to_metadata(),
        observed_at=still_running.observed_at,
        remote_status=still_running.status,
        cancel_confirmed=False,
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_cancel")
    assert run is not None
    assert run["cancel_confirmed_at"] is None

    confirmed = _observation(RemoteDisposition.CANCELLED)
    await db_context.delegation_runs.record_remote_observation(
        "delegation_cancel",
        observation=confirmed.to_metadata(),
        observed_at=confirmed.observed_at,
        remote_status=confirmed.status,
        cancel_confirmed=True,
    )
    run = await db_context.delegation_runs.get_by_delegation_id("delegation_cancel")
    assert run is not None
    assert run["cancel_confirmed_at"] is not None


@pytest.mark.asyncio
async def test_the_poll_path_records_what_it_saw(
    db_engine: AsyncEngine,
) -> None:
    """A poll of an observable target leaves the reading behind for diagnosis.

    Polling and reconciliation read through the same classifier, so a run that
    was polled already carries the record reconciliation would otherwise have
    to go and fetch.
    """
    target = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    worker, processing_service, chat_interface = _worker_for(db_engine, target)
    db_context = Database(engine=db_engine)
    await _create_run(db_context, delegation_id="delegation_polled")
    await db_context.delegation_runs.mark_awaiting_remote(
        "delegation_polled",
        remote_task_id=REMOTE_TASK_ID,
        remote_context_id=None,
        started_at=SystemClock().now(),
    )

    await worker.handle_delegation_poll(
        _tool_context(db_context, processing_service, chat_interface),
        {
            "delegation_id": "delegation_polled",
            "interface_type": TEST_INTERFACE_TYPE,
            "conversation_id": TEST_CONVERSATION_ID,
            "user_name": TEST_USER_NAME,
        },
    )

    run = await db_context.delegation_runs.get_by_delegation_id("delegation_polled")
    assert run is not None
    assert run["status"] == "awaiting_remote"
    assert run["remote_status"] == "pending"
    assert run["remote_observation_json"] is not None


@pytest.mark.asyncio
async def test_an_observable_target_satisfies_the_protocol() -> None:
    """Observability is structural, and separate from being pollable."""
    observable = FakeObservableService([_observation(RemoteDisposition.PENDING)])
    assert isinstance(observable, ObservableDelegationService)
    assert not isinstance(FakePollableService(), ObservableDelegationService)
