"""Turns the MCP adapter runs detached from the tool call that started them.

A turn can take minutes, and MCP clients do not wait that long for one tool
call: ChatGPT abandons a call after about a minute and sends no progress token,
so neither progress notifications nor a longer proxy timeout keeps it waiting.
The adapter therefore runs each turn as its own task, waits for it only as long
as a client will, and lets the client collect the reply with a follow-up call.

The registry is in memory. The deployment runs one replica, and a restart ends
every running turn anyway, so a turn id the registry has never seen or has
already forgotten gets an error telling the caller to ask again.
"""

import asyncio
import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from family_assistant.storage.database import spawn_detached
from family_assistant.web.models import ChatMessageResponse

logger = logging.getLogger(__name__)

# How long a finished turn stays collectable. Long enough for a client that
# polls late; short enough that the registry stays small.
FINISHED_TURN_RETENTION_SECONDS = 3600.0


@dataclass
class PendingTurn:
    """A turn started over MCP, running or recently finished."""

    turn_id: str
    conversation_id: str
    user_id: str
    task: asyncio.Task[ChatMessageResponse]
    finished_at: float | None = field(default=None)


class PendingTurns:
    """The turns the adapter is running, keyed by turn id."""

    def __init__(self) -> None:
        self._turns: dict[str, PendingTurn] = {}

    def start(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        user_id: str,
        turn: Coroutine[Any, Any, ChatMessageResponse],
    ) -> PendingTurn:
        """Run ``turn`` in its own task, so it outlives the call that started it."""
        self._prune()
        pending = PendingTurn(
            turn_id=turn_id,
            conversation_id=conversation_id,
            user_id=user_id,
            task=spawn_detached(turn, name=f"mcp-turn-{turn_id}"),
        )

        def _finished(task: asyncio.Task[ChatMessageResponse]) -> None:
            pending.finished_at = time.monotonic()
            if not task.cancelled() and task.exception() is not None:
                logger.info(
                    "MCP turn %s in conversation %s failed: %r",
                    turn_id,
                    conversation_id,
                    task.exception(),
                )

        pending.task.add_done_callback(_finished)
        self._turns[turn_id] = pending
        return pending

    def get(self, turn_id: str, user_id: str) -> PendingTurn | None:
        """The turn with this id, if it exists and belongs to ``user_id``."""
        self._prune()
        pending = self._turns.get(turn_id)
        if pending is None or pending.user_id != user_id:
            return None
        return pending

    async def aclose(self) -> None:
        """Cancel every running turn and wait for them to end."""
        tasks = [p.task for p in self._turns.values() if not p.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._turns.clear()

    def _prune(self) -> None:
        cutoff = time.monotonic() - FINISHED_TURN_RETENTION_SECONDS
        expired = [
            turn_id
            for turn_id, pending in self._turns.items()
            if pending.finished_at is not None and pending.finished_at < cutoff
        ]
        for turn_id in expired:
            del self._turns[turn_id]


async def wait_for_turn(pending: PendingTurn, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for the turn; whether it has finished.

    Never cancels the turn: a caller that stops waiting, or whose request is
    dropped, leaves it running for the next call to collect.
    """
    await asyncio.wait({pending.task}, timeout=timeout)
    return pending.task.done()
