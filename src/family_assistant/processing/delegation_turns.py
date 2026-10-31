"""The delegated turns this process is running, so one can be stopped mid-turn."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Generator

logger = logging.getLogger(__name__)


class RunningDelegationTurns:
    """Maps a delegation to the task running its current turn.

    Every local turn runs in this one process, so stopping one is cancelling
    its task, as the web Stop button does for a chat turn. One instance is
    shared by every profile: the worker registers a turn under the target
    profile, and ``cancel_delegation`` stops it from the caller's.
    """

    def __init__(self) -> None:
        self._turns: dict[str, asyncio.Task[object]] = {}

    @contextmanager
    def running(
        self, delegation_id: str, turn: asyncio.Task[object]
    ) -> Generator[None]:
        """Register ``turn`` as the delegation's running turn for the block."""
        self._turns[delegation_id] = turn
        try:
            yield
        finally:
            if self._turns.get(delegation_id) is turn:
                del self._turns[delegation_id]

    def stop(self, delegation_id: str) -> bool:
        """Cancel the delegation's running turn; return whether there was one."""
        turn = self._turns.get(delegation_id)
        if turn is None or turn.done():
            return False
        logger.info("Stopping the running turn of delegation %s.", delegation_id)
        turn.cancel()
        return True
