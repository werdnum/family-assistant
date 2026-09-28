"""The MCP adapter's holder of detached turns."""

import asyncio

import pytest

from family_assistant.web.mcp_adapter.turns import RunningTurns, wait_for_turn
from family_assistant.web.models import ChatMessageResponse


async def _reply_after(gate: asyncio.Event) -> ChatMessageResponse:
    await gate.wait()
    return ChatMessageResponse(reply="done", conversation_id="c", turn_id="t")


@pytest.mark.asyncio
async def test_timed_out_wait_leaves_the_turn_running() -> None:
    turns = RunningTurns()
    gate = asyncio.Event()
    task = turns.start("c", "alice", _reply_after(gate))

    assert not await wait_for_turn(task, timeout=0.01)
    assert turns.get("c", "alice") is task

    gate.set()
    assert await wait_for_turn(task, timeout=5)
    assert task.result().reply == "done"
    await turns.aclose()


@pytest.mark.asyncio
async def test_turn_is_visible_only_to_its_user() -> None:
    turns = RunningTurns()
    gate = asyncio.Event()
    task = turns.start("c", "alice", _reply_after(gate))

    assert turns.get("c", "alice") is task
    assert turns.get("c", "bob") is None
    assert turns.is_running("c")
    gate.set()
    await turns.aclose()


@pytest.mark.asyncio
async def test_finished_turn_is_released() -> None:
    turns = RunningTurns()
    gate = asyncio.Event()
    gate.set()
    task = turns.start("c", "alice", _reply_after(gate))

    await task

    assert turns.get("c", "alice") is None


@pytest.mark.asyncio
async def test_aclose_cancels_running_turns() -> None:
    turns = RunningTurns()
    task = turns.start("c", "alice", _reply_after(asyncio.Event()))

    await turns.aclose()

    assert task.cancelled()
    assert turns.get("c", "alice") is None
