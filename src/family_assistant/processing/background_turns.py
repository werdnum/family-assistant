"""Host background LLM turns outside the task worker's handlers.

A task handler that has a turn to run launches it here and returns. The turn
then lives as a tracked asyncio task of its own: no handler timeout, health
watchdog or stale-claim cutoff applies to it. What keeps it honest instead is
its turn lease (see ``turn_resumption``): the process heartbeat renews it, a
graceful shutdown suspends the turn and hands the lease off, and a crash lets
it come due so the turn is resumed elsewhere.

The runner bounds how many turns run at once. The bound is checked when a
handler admits a turn, not when it launches one, so handlers admitting at the
same moment may overshoot it by a turn or two; that is the trade for not
holding a reservation across the database writes in between.

See docs/design/background-turns-off-the-task-worker.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from family_assistant.services.turn_resumption import (
        TurnLease,
        TurnLeaseRegistry,
    )
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

DEFAULT_MAX_BACKGROUND_TURNS = 4


class BackgroundTurnRunner:
    """Runs background turns as tracked tasks, up to a concurrency bound."""

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_BACKGROUND_TURNS,
        lease_registry: TurnLeaseRegistry | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        self._lease_registry = lease_registry
        self._turns: dict[asyncio.Task[None], TurnLease | None] = {}

    @property
    def lease_registry(self) -> TurnLeaseRegistry | None:
        return self._lease_registry

    @property
    def running(self) -> int:
        return len(self._turns)

    def has_capacity(self) -> bool:
        return len(self._turns) < self._max_concurrent

    def launch(
        self,
        name: str,
        body: Coroutine[object, object, None],
        *,
        lease: TurnLease | None,
        database: Database,
        request_suspend: Callable[[], None],
    ) -> asyncio.Task[None]:
        """Start ``body`` as a background turn, renewing ``lease`` while it runs.

        ``body`` owns the turn's whole outcome -- recording success, failure or
        a timeout. One that raises instead recorded none, so its lease is left
        to come due and its resumer settles the turn from durable state.
        """
        task = asyncio.create_task(body, name=name)
        self._turns[task] = lease
        # Added before the lease registry's own callback, so an abandoned
        # lease is untracked before the registry would release it.
        task.add_done_callback(self._on_done)
        if self._lease_registry is not None and lease is not None:
            self._lease_registry.track(
                lease, task, database=database, request_suspend=request_suspend
            )
        return task

    def is_running(self, name: str) -> bool:
        """Whether a background turn launched under ``name`` is still running."""
        return any(task.get_name() == name and not task.done() for task in self._turns)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        lease = self._turns.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.error("Background turn %s raised", task.get_name(), exc_info=error)
        if self._lease_registry is not None and lease is not None:
            self._lease_registry.abandon(lease)

    async def wait_idle(self) -> None:
        """Wait until no background turn is running, including ones they start."""
        while self._turns:
            await asyncio.wait(set(self._turns))

    async def cancel_all(self) -> None:
        """Cancel every turn still running; for turns no lease will resume."""
        turns = set(self._turns)
        for turn in turns:
            turn.cancel()
        if turns:
            await asyncio.wait(turns)
