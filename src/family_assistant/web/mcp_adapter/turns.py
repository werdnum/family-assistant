"""Turns the MCP adapter runs detached from the tool call that started them.

A turn can take minutes, and MCP clients do not wait that long for one tool
call: ChatGPT abandons a call after a minute or two and sends no progress
token, so neither progress notifications nor a longer proxy timeout keeps it
waiting. The adapter therefore runs each turn as its own task, waits for it
only as long as a client will, and lets the client read the reply from the
conversation with a follow-up call.

The reply itself is read from message history, so nothing here has to outlive
the turn; this only holds each running turn's task, keyed by conversation, so
the task is not garbage collected and a follow-up call can wait on it.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from family_assistant.storage.database import spawn_detached
from family_assistant.web.models import ChatMessageResponse


class RunningTurns:
    """The turns the adapter is running, at most one per conversation."""

    def __init__(self) -> None:
        self._tasks: dict[str, tuple[str, asyncio.Task[ChatMessageResponse]]] = {}

    def is_running(self, conversation_id: str) -> bool:
        """Whether any turn is running in ``conversation_id``."""
        return conversation_id in self._tasks

    def get(
        self, conversation_id: str, user_id: str
    ) -> asyncio.Task[ChatMessageResponse] | None:
        """The task running ``user_id``'s turn in ``conversation_id``, if there is one."""
        entry = self._tasks.get(conversation_id)
        if entry is None or entry[0] != user_id:
            return None
        return entry[1]

    def start(
        self,
        conversation_id: str,
        user_id: str,
        turn: Coroutine[Any, Any, ChatMessageResponse],
    ) -> asyncio.Task[ChatMessageResponse]:
        """Run ``turn`` in its own task, so it outlives the call that started it.

        The caller checks ``is_running`` first: the turn's own reservation
        refuses a second turn in one conversation, but it would do so from
        inside a task this registry had already replaced.
        """
        task = spawn_detached(turn, name=f"mcp-turn-{conversation_id}")
        self._tasks[conversation_id] = (user_id, task)

        def _finished(done: asyncio.Task[ChatMessageResponse]) -> None:
            entry = self._tasks.get(conversation_id)
            if entry is not None and entry[1] is done:
                del self._tasks[conversation_id]

        task.add_done_callback(_finished)
        return task

    async def aclose(self) -> None:
        """Cancel every running turn and wait for them to end."""
        tasks = [task for _user_id, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


async def wait_for_turn(
    task: asyncio.Task[ChatMessageResponse], timeout: float
) -> bool:
    """Wait up to ``timeout`` seconds for the turn; whether it has finished.

    Never cancels the turn: a caller that stops waiting, or whose request is
    dropped, leaves it running for the next call to collect.
    """
    await asyncio.wait({task}, timeout=timeout)
    return task.done()
