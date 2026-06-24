"""Cooperative interrupt/steer handle for a single web chat turn.

The web counterpart to ``TelegramMidTurnController``. While a turn's producer
task runs the LLM loop, the cancel/steer HTTP endpoints reach this controller
(stored on the hub's ``TurnRecord``) to:

- request a graceful interrupt (``request_interrupt``), which the loop honors at
  its next iteration boundary by raising ``asyncio.CancelledError``; and
- inject a mid-turn user message (``add_input``), which the loop drains after the
  next tool round and re-feeds to the model as steering context.

It implements the ``MidTurnInputProvider`` protocol the processing layer expects
(``processing/types.py``). Unlike Telegram there is no follow-up batch
processing — the steering text is the whole payload.
"""

import asyncio

from family_assistant.processing.types import MidTurnUserInput


class WebMidTurnController:
    """Tracks live interrupt/steer requests for one active web turn."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pending: list[MidTurnUserInput] = []
        self._interrupted = False

    def request_interrupt(self) -> None:
        """Mark the turn for a graceful stop at the next loop boundary."""
        self._interrupted = True

    def should_interrupt(self) -> bool:
        """Return whether a stop has been requested for this turn."""
        return self._interrupted

    async def add_input(self, user_input: MidTurnUserInput) -> None:
        """Queue a steering message to inject into the next LLM iteration."""
        async with self._lock:
            self._pending.append(user_input)

    async def drain_pending_mid_turn_inputs(self) -> list[MidTurnUserInput]:
        """Return and consume any queued mid-turn user inputs."""
        async with self._lock:
            pending = self._pending
            self._pending = []
            return pending
