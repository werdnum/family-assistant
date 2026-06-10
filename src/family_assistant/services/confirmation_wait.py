"""Shared WAIT state machine for durable tool-confirmation requests.

Both the web turn producer and the Telegram UI manager send a confirmation
prompt, then wait until one of:

- the local decision future resolves (a button press / API decision arrived in
  this process),
- the durable execution future resolves (an approval enqueued a background
  execution that completed elsewhere),
- the durable status, polled on an interval, shows the request was resolved or
  expired out of band, or
- the overall deadline passes.

The loop structure and the user-facing error strings are identical between the
two callers; only HOW each resolution is surfaced differs (the web side
publishes hub events, Telegram edits its message), and Telegram additionally
supports a non-durable mode without an execution future. Those differences are
injected through :class:`ConfirmationWaitStrategy` so this module owns the
state machine and nothing else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from family_assistant.services.confirmation_service import (
    DURABLE_CONFIRMATION_STATUS_POLL_SECONDS,
)
from family_assistant.tools.types import ConfirmationOutcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

CONFIRMATION_UNRESOLVED_MESSAGE = "Confirmation request could not be resolved."

# Durable statuses that mean the request was resolved or torn down out of band
# while polling, and can no longer be approved or rejected cleanly.
_POLL_FAILED_STATUSES = frozenset({"expired", "missing", "unauthorized", "error"})
# The post-deadline check never treats "expired" as a failure: an expired
# request at the deadline is the timed-out path, which marks the request
# expired and returns ``timed_out``.
_FINAL_FAILED_STATUSES = frozenset({"missing", "unauthorized", "error"})


@dataclass(frozen=True)
class ConfirmationWaitStrategy:
    """Caller-specific side effects for the confirmation WAIT loop.

    ``get_durable_status`` returns the current durable status, or ``None`` when
    the request is non-durable (Telegram only). Every ``on_*`` callback runs the
    caller's delivery side effect (hub publish / message edit) for the matching
    resolution; the state machine then returns the corresponding outcome.

    ``durable`` is ``True`` whenever an execution future exists. ``execution``
    is the optional durable execution future; ``decision`` is always present.

    ``on_decision_approved`` and ``on_resolved_approved`` are kept distinct
    because a local approval (the user pressed approve in this process) and an
    out-of-band approval discovered while polling need different delivery side
    effects for some callers: the web side treats them identically, but
    Telegram has already edited its message on a local button press and must
    not overwrite that with an "resolved externally" notice, while it does add
    that notice when it discovers the approval via polling.
    """

    decision: asyncio.Future[ConfirmationOutcome]
    execution: asyncio.Future[ConfirmationOutcome] | None
    durable: bool
    get_durable_status: Callable[[], Awaitable[str | None]]
    wait_for_execution_result: Callable[[], Awaitable[ConfirmationOutcome]]
    on_decision: Callable[[ConfirmationOutcome], Awaitable[None]]
    on_execution_done: Callable[[ConfirmationOutcome], Awaitable[None]]
    on_decision_approved: Callable[[], Awaitable[None]]
    on_resolved_approved: Callable[[], Awaitable[None]]
    on_resolved_rejected: Callable[[], Awaitable[None]]
    on_resolved_failed: Callable[[], Awaitable[None]]
    on_timed_out: Callable[[], Awaitable[None]]


async def wait_for_confirmation_resolution(
    strategy: ConfirmationWaitStrategy,
    *,
    timeout_seconds: float,
) -> ConfirmationOutcome:
    """Drive the durable-confirmation WAIT loop to a terminal outcome.

    Polls ``strategy.get_durable_status`` on the standard interval while waiting
    on the decision and execution futures, returning the first terminal
    ``ConfirmationOutcome`` reached. Callers own all I/O via ``strategy``; this
    function performs none itself.
    """
    decision_future = strategy.decision
    execution_future = strategy.execution

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break

        wait_futures: set[asyncio.Future[ConfirmationOutcome]] = {decision_future}
        if execution_future is not None:
            wait_futures.add(execution_future)
        done, _pending = await asyncio.wait(
            wait_futures,
            timeout=min(DURABLE_CONFIRMATION_STATUS_POLL_SECONDS, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if decision_future in done:
            decision_outcome = decision_future.result()
            await strategy.on_decision(decision_outcome)
            if not strategy.durable:
                return decision_outcome
            if decision_outcome.kind != "approved":
                return decision_outcome
            await strategy.on_decision_approved()
            return await strategy.wait_for_execution_result()
        if execution_future is not None and execution_future in done:
            execution_outcome = execution_future.result()
            await strategy.on_execution_done(execution_outcome)
            return execution_outcome

        durable_status = await strategy.get_durable_status()
        if durable_status == "approved" and (
            not strategy.durable or execution_future is not None
        ):
            await strategy.on_resolved_approved()
            return await strategy.wait_for_execution_result()
        if durable_status == "rejected":
            await strategy.on_resolved_rejected()
            return ConfirmationOutcome(kind="rejected")
        if durable_status in _POLL_FAILED_STATUSES:
            await strategy.on_resolved_failed()
            return ConfirmationOutcome(
                kind="failed",
                result=CONFIRMATION_UNRESOLVED_MESSAGE,
            )

    final_status = await strategy.get_durable_status()
    if final_status == "approved" and (
        not strategy.durable or execution_future is not None
    ):
        await strategy.on_resolved_approved()
        return await strategy.wait_for_execution_result()
    if final_status == "rejected":
        await strategy.on_resolved_rejected()
        return ConfirmationOutcome(kind="rejected")
    if final_status in _FINAL_FAILED_STATUSES:
        await strategy.on_resolved_failed()
        return ConfirmationOutcome(
            kind="failed",
            result=CONFIRMATION_UNRESOLVED_MESSAGE,
        )
    await strategy.on_timed_out()
    return ConfirmationOutcome(kind="timed_out")
