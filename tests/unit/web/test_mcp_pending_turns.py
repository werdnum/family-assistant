"""The MCP adapter's registry of detached turns."""

import asyncio

import pytest

from family_assistant.web.mcp_adapter.turns import PendingTurns, wait_for_turn
from family_assistant.web.models import ChatMessageResponse


async def _reply_after(gate: asyncio.Event) -> ChatMessageResponse:
    await gate.wait()
    return ChatMessageResponse(reply="done", conversation_id="c", turn_id="t")


@pytest.mark.asyncio
async def test_turn_is_visible_only_to_its_user() -> None:
    turns = PendingTurns()
    gate = asyncio.Event()
    turns.start(
        turn_id="t", conversation_id="c", user_id="alice", turn=_reply_after(gate)
    )

    assert turns.get("t", "alice") is not None
    assert turns.get("t", "bob") is None
    gate.set()
    await turns.aclose()


@pytest.mark.asyncio
async def test_timed_out_wait_leaves_the_turn_running() -> None:
    turns = PendingTurns()
    gate = asyncio.Event()
    pending = turns.start(
        turn_id="t", conversation_id="c", user_id="alice", turn=_reply_after(gate)
    )

    assert not await wait_for_turn(pending, timeout=0.01)
    assert not pending.task.done()

    gate.set()
    assert await wait_for_turn(pending, timeout=5)
    assert pending.task.result().reply == "done"
    await turns.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_running_turns() -> None:
    turns = PendingTurns()
    pending = turns.start(
        turn_id="t",
        conversation_id="c",
        user_id="alice",
        turn=_reply_after(asyncio.Event()),
    )

    await turns.aclose()

    assert pending.task.cancelled()
    assert turns.get("t", "alice") is None
