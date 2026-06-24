"""Tests for the web turn-control endpoints: cancel (Stop) and steer.

These cover the cooperative interrupt/steer mechanism wired into the web turn
producer:

* ``POST /v1/chat/turns/{turn_id}/cancel`` stops a running turn. It requests a
  graceful interrupt and then hard-cancels the producer task, ending the turn as
  ``cancelled`` (a distinct, non-error terminal status).
* ``POST /v1/chat/turns/{turn_id}/steer`` injects a mid-turn user message that
  the LLM loop drains after the next tool round, surfaces as a ``user_input``
  SSE event, and persists to message history.

The fixtures reuse the shared ``app_fixture`` / ``api_test_client`` /
``api_mock_llm_client`` stack from ``tests/functional/web/conftest.py`` and the
LLM-gating pattern from ``test_turn_producer_resilience.py``.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import MessageReasoningInfo, UserMessage
from family_assistant.storage.context import get_db_context
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.web.conversation_stream_hub import (
        ConversationStreamHub,
        StreamEvent,
        SubscriptionHandle,
    )


def _reply(content: str) -> LLMOutput:
    return LLMOutput(
        content=content,
        tool_calls=None,
        reasoning_info=MessageReasoningInfo(
            prompt_tokens=10, completion_tokens=10, total_tokens=20
        ),
    )


def _user_message_contains(args: dict, marker: str) -> bool:
    return any(
        msg.role == "user" and marker in str(msg.content or "")
        for msg in args.get("messages", [])
    )


def _turn_complete(
    hub: "ConversationStreamHub", conversation_id: str, turn_id: str
) -> Callable[[], bool]:
    """Condition for wait_for_condition: the turn has reached 'complete'."""

    def _check() -> bool:
        turn = hub.get_turn(conversation_id, turn_id)
        return turn is not None and turn.status == "complete"

    return _check


def _drain(handle: "SubscriptionHandle") -> "list[StreamEvent]":
    events = list(handle.replayed_events)
    while not handle.queue.empty():
        events.append(handle.queue.get_nowait())
    return events


async def test_cancel_running_turn_marks_cancelled(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a running turn ends it as ``cancelled`` (not ``failed``).

    The endpoint requests a graceful interrupt before hard-cancelling, so the
    producer resolves the CancelledError to a user-initiated stop.
    """
    user_prompt = "Cancel me mid-turn"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("never sent"),
    ))

    started = asyncio.Event()
    release = asyncio.Event()
    original_generate = api_mock_llm_client.generate_response

    async def gated_generate(*args: object, **kwargs: object) -> LLMOutput:
        started.set()
        await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api_mock_llm_client, "generate_response", gated_generate)

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_cancel_{uuid.uuid4().hex[:8]}"
    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post.status_code == 200, post.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    handle = await hub.subscribe(conversation_id, from_seq=0)
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)

        cancel = await api_test_client.post(
            f"/api/v1/chat/turns/{turn_id}/cancel",
            json={"conversation_id": conversation_id},
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "cancelling"

        producer_tasks = hub.get_active_producer_tasks(conversation_id)
        await asyncio.gather(*producer_tasks, return_exceptions=True)
        release.set()

        turn = hub.get_turn(conversation_id, turn_id)
        assert turn is not None
        assert turn.status == "cancelled"

        events = _drain(handle)
        assert any(
            e.type == "turn_ended" and e.payload.get("status") == "cancelled"
            for e in events
        ), f"Expected turn_ended(cancelled), got {events}"
    finally:
        hub.unsubscribe(conversation_id, handle.queue)


async def test_cancel_unknown_turn_returns_404(
    api_test_client: AsyncClient,
) -> None:
    """Cancelling a turn the hub has never seen is a 404."""
    conversation_id = f"conv_unknown_{uuid.uuid4().hex[:8]}"
    response = await api_test_client.post(
        f"/api/v1/chat/turns/{uuid.uuid4()}/cancel",
        json={"conversation_id": conversation_id},
    )
    assert response.status_code == 404, response.text


async def test_cancel_finished_turn_is_idempotent_noop(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Cancelling an already-finished turn returns 200 with its terminal status."""
    user_prompt = "Finish then cancel"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("all done"),
    ))

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_done_{uuid.uuid4().hex[:8]}"
    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post.status_code == 200, post.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )

    cancel = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/cancel",
        json={"conversation_id": conversation_id},
    )
    assert cancel.status_code == 200, cancel.text
    body = cancel.json()
    assert body["already_complete"] is True
    assert body["status"] == "complete"


async def test_cancel_rejects_conversation_owned_by_another_user(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Cancel enforces ownership before touching turn state (404, not 403)."""
    conversation_id = f"conv_owned_{uuid.uuid4().hex[:8]}"
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            UserMessage(content="victim's private message"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )

    response = await api_test_client.post(
        f"/api/v1/chat/turns/{uuid.uuid4()}/cancel",
        json={"conversation_id": conversation_id},
    )
    assert response.status_code == 404, response.text


async def test_steer_running_turn_injects_user_input(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steering message injected mid-turn surfaces as a user_input event and
    is persisted, and the model sees it on the next iteration.

    The turn makes one tool call (so the loop reaches the mid-turn drain after
    the tool round); the gate parks the first LLM call so the steer is queued
    before the drain runs.
    """
    user_prompt = "Steer me mid-turn"
    steer_text = "actually, focus on tomorrow"

    # Iteration 2: once the injected [MID-TURN USER UPDATE] is in the messages,
    # reply with final text. Listed first so it wins over the tool-call rule.
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, "MID-TURN USER UPDATE"),
        _reply("Okay, focusing on tomorrow."),
    ))
    # Iteration 1: the initial prompt triggers a (side-effect-free) tool call.
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_steer_1",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=10, completion_tokens=10, total_tokens=20
            ),
        ),
    ))

    started = asyncio.Event()
    release = asyncio.Event()
    original_generate = api_mock_llm_client.generate_response
    first_call = {"done": False}

    async def gated_generate(*args: object, **kwargs: object) -> LLMOutput:
        # Park only the first LLM call so the steer is queued before the
        # post-tool-round drain; later iterations run unimpeded.
        if not first_call["done"]:
            first_call["done"] = True
            started.set()
            await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api_mock_llm_client, "generate_response", gated_generate)

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_steer_{uuid.uuid4().hex[:8]}"
    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post.status_code == 200, post.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    handle = await hub.subscribe(conversation_id, from_seq=0)
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)

        steer = await api_test_client.post(
            f"/api/v1/chat/turns/{turn_id}/steer",
            json={"conversation_id": conversation_id, "prompt": steer_text},
        )
        assert steer.status_code == 200, steer.text
        assert steer.json()["accepted"] is True

        release.set()
        await wait_for_condition(
            _turn_complete(hub, conversation_id, turn_id), description="turn complete"
        )

        events = _drain(handle)
        assert any(
            e.type == "user_input" and e.payload.get("content") == steer_text
            for e in events
        ), f"Expected a user_input event carrying the steer text, got {events}"
    finally:
        hub.unsubscribe(conversation_id, handle.queue)

    # The injected mid-turn message is persisted (formatted as a steering update).
    async with get_db_context(engine=db_engine) as ctx:
        rows = await ctx.message_history.get_recent_with_metadata(
            interface_type="web",
            conversation_id=conversation_id,
            limit=50,
        )
    assert any(
        row["role"] == "user" and steer_text in str(row["content"]) for row in rows
    ), "Expected the steering message to be persisted to message history"


async def test_steer_finished_turn_returns_409(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Steering a turn that has already finished is a 409 (start a new turn)."""
    user_prompt = "Finish then steer"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("all done"),
    ))

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_steerdone_{uuid.uuid4().hex[:8]}"
    post = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post.status_code == 200, post.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )

    steer = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/steer",
        json={"conversation_id": conversation_id, "prompt": "too late"},
    )
    assert steer.status_code == 409, steer.text


async def test_steer_unknown_turn_returns_404(
    api_test_client: AsyncClient,
) -> None:
    """Steering a turn the hub has never seen is a 404."""
    conversation_id = f"conv_steerunknown_{uuid.uuid4().hex[:8]}"
    response = await api_test_client.post(
        f"/api/v1/chat/turns/{uuid.uuid4()}/steer",
        json={"conversation_id": conversation_id, "prompt": "hello?"},
    )
    assert response.status_code == 404, response.text


async def test_steer_rejects_conversation_owned_by_another_user(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Steer enforces ownership before touching turn state (404, not 403)."""
    conversation_id = f"conv_steerowned_{uuid.uuid4().hex[:8]}"
    async with get_db_context(engine=db_engine) as ctx:
        await ctx.message_history.add_message(
            UserMessage(content="victim's private message"),
            interface_type="web",
            conversation_id=conversation_id,
            timestamp=datetime.now(UTC),
            user_id="someone_else",
        )

    response = await api_test_client.post(
        f"/api/v1/chat/turns/{uuid.uuid4()}/steer",
        json={"conversation_id": conversation_id, "prompt": "let me steer"},
    )
    assert response.status_code == 404, response.text
