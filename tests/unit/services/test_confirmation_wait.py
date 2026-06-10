"""Unit tests for the shared confirmation WAIT state machine.

These exercise :func:`wait_for_confirmation_resolution` directly with simple
recording callbacks, so the loop logic is verified independently of the web hub
and the Telegram UI that drive it in production.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from family_assistant.services.confirmation_wait import (
    CONFIRMATION_UNRESOLVED_MESSAGE,
    ConfirmationWaitStrategy,
    wait_for_confirmation_resolution,
)
from family_assistant.tools.types import ConfirmationOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _RecordingStrategyBuilder:
    """Builds a strategy whose side effects append to an ordered call log."""

    def __init__(
        self,
        *,
        durable: bool,
        execution: asyncio.Future[ConfirmationOutcome] | None,
        statuses: list[str | None],
        execution_result: ConfirmationOutcome | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._statuses = statuses
        self._execution_result = execution_result or ConfirmationOutcome(
            kind="completed", result="executed"
        )
        self.decision: asyncio.Future[ConfirmationOutcome] = (
            asyncio.get_running_loop().create_future()
        )
        self.strategy = ConfirmationWaitStrategy(
            decision=self.decision,
            execution=execution,
            durable=durable,
            get_durable_status=self._get_durable_status,
            wait_for_execution_result=self._wait_for_execution_result,
            on_decision=self._make_cb("on_decision"),
            on_execution_done=self._make_cb("on_execution_done"),
            on_decision_approved=self._make_void_cb("on_decision_approved"),
            on_resolved_approved=self._make_void_cb("on_resolved_approved"),
            on_resolved_rejected=self._make_void_cb("on_resolved_rejected"),
            on_resolved_failed=self._make_void_cb("on_resolved_failed"),
            on_timed_out=self._make_void_cb("on_timed_out"),
        )

    async def _get_durable_status(self) -> str | None:
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0] if self._statuses else None

    async def _wait_for_execution_result(self) -> ConfirmationOutcome:
        self.calls.append("wait_for_execution_result")
        return self._execution_result

    def _make_cb(self, name: str) -> Callable[[ConfirmationOutcome], Awaitable[None]]:
        async def _cb(_outcome: ConfirmationOutcome) -> None:
            self.calls.append(name)

        return _cb

    def _make_void_cb(self, name: str) -> Callable[[], Awaitable[None]]:
        async def _cb() -> None:
            self.calls.append(name)

        return _cb


@pytest.mark.asyncio
async def test_local_decision_approved_waits_for_execution() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True,
        execution=execution,
        statuses=["pending"],
        execution_result=ConfirmationOutcome(kind="completed", result="done"),
    )
    builder.decision.set_result(ConfirmationOutcome(kind="approved"))

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="completed", result="done")
    assert builder.calls == [
        "on_decision",
        "on_decision_approved",
        "wait_for_execution_result",
    ]


@pytest.mark.asyncio
async def test_local_decision_rejected_returns_outcome() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=["pending"]
    )
    builder.decision.set_result(ConfirmationOutcome(kind="rejected"))

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="rejected")
    assert builder.calls == ["on_decision"]


@pytest.mark.asyncio
async def test_non_durable_decision_returns_outcome_directly() -> None:
    builder = _RecordingStrategyBuilder(durable=False, execution=None, statuses=[None])
    builder.decision.set_result(ConfirmationOutcome(kind="approved"))

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="approved")
    assert builder.calls == ["on_decision"]


@pytest.mark.asyncio
async def test_execution_future_completes_first() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=["approved"]
    )
    execution.set_result(ConfirmationOutcome(kind="completed", result="bg done"))

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="completed", result="bg done")
    assert builder.calls == ["on_execution_done"]


@pytest.mark.asyncio
async def test_poll_approved_resolves_externally() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True,
        execution=execution,
        statuses=["approved"],
        execution_result=ConfirmationOutcome(kind="completed", result="poll done"),
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="completed", result="poll done")
    assert builder.calls == ["on_resolved_approved", "wait_for_execution_result"]


@pytest.mark.asyncio
async def test_poll_rejected_resolves_externally() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=["rejected"]
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(kind="rejected")
    assert builder.calls == ["on_resolved_rejected"]


@pytest.mark.parametrize("status", ["expired", "missing", "unauthorized", "error"])
@pytest.mark.asyncio
async def test_poll_failed_statuses_return_failed(status: str) -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=[status]
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=5.0
    )

    assert outcome == ConfirmationOutcome(
        kind="failed", result=CONFIRMATION_UNRESOLVED_MESSAGE
    )
    assert builder.calls == ["on_resolved_failed"]


@pytest.mark.asyncio
async def test_deadline_with_pending_status_times_out() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=["pending"]
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=-1.0
    )

    assert outcome == ConfirmationOutcome(kind="timed_out")
    assert builder.calls == ["on_timed_out"]


@pytest.mark.asyncio
async def test_deadline_with_expired_status_times_out() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=["expired"]
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=-1.0
    )

    assert outcome == ConfirmationOutcome(kind="timed_out")
    assert builder.calls == ["on_timed_out"]


@pytest.mark.parametrize("status", ["missing", "unauthorized", "error"])
@pytest.mark.asyncio
async def test_deadline_with_failed_status_returns_failed(status: str) -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True, execution=execution, statuses=[status]
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=-1.0
    )

    assert outcome == ConfirmationOutcome(
        kind="failed", result=CONFIRMATION_UNRESOLVED_MESSAGE
    )
    assert builder.calls == ["on_resolved_failed"]


@pytest.mark.asyncio
async def test_deadline_with_approved_status_resolves() -> None:
    execution: asyncio.Future[ConfirmationOutcome] = (
        asyncio.get_running_loop().create_future()
    )
    builder = _RecordingStrategyBuilder(
        durable=True,
        execution=execution,
        statuses=["approved"],
        execution_result=ConfirmationOutcome(kind="completed", result="late"),
    )

    outcome = await wait_for_confirmation_resolution(
        builder.strategy, timeout_seconds=-1.0
    )

    assert outcome == ConfirmationOutcome(kind="completed", result="late")
    assert builder.calls == ["on_resolved_approved", "wait_for_execution_result"]
