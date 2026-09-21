"""Resilience tests for the turn producer and WebChatInterface.

These cover review findings that the happy-path streaming tests don't:

* The producer task being cancelled mid-turn must still end the turn, so the
  ``TurnRecord`` never wedges at status='running' (pruning/eviction skip running
  turns).
* ``WebChatInterface.send_message`` must let a post-commit hub ``publish``
  failure propagate, instead of swallowing it and returning ``None`` (which
  would make a durably-saved message look like a failed send).

The fixtures reuse the shared ``app_fixture`` / ``api_test_client`` /
``api_mock_llm_client`` stack from ``tests/functional/web/conftest.py``.
"""

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import MessageReasoningInfo
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.web.conversation_stream_hub import (
        ConversationStreamHub,
        StreamEvent,
    )


def _add_simple_llm_rule(
    mock_llm_client: RuleBasedMockLLMClient,
    *,
    prompt_marker: str,
    reply: str,
) -> None:
    """Reply ``reply`` whenever a user message contains ``prompt_marker``."""
    mock_llm_client.rules.append((
        lambda args: any(
            msg.role == "user" and prompt_marker in str(msg.content or "")
            for msg in args.get("messages", [])
        ),
        LLMOutput(
            content=reply,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=10, completion_tokens=10, total_tokens=20
            ),
        ),
    ))


async def test_producer_cancellation_ends_running_turn(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the producer task mid-turn must still end the turn.

    Without the CancelledError handler the TurnRecord stays status='running'
    forever (pruning/eviction skip running turns), so the conversation wedges.
    Gate the LLM so the producer is mid-flight, cancel its task, and assert the
    turn is no longer 'running' (a failed turn_ended was published).
    """
    user_prompt = "Cancel mid-turn"
    _add_simple_llm_rule(
        api_mock_llm_client, prompt_marker=user_prompt, reply="never sent"
    )

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

    post_response = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": user_prompt,
            "interface_type": "web",
        },
    )
    assert post_response.status_code == 200, post_response.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub

    # Subscribe so we can observe the failed turn_ended the cancellation path
    # publishes.
    handle = await hub.subscribe(conversation_id, from_seq=0)

    await asyncio.wait_for(started.wait(), timeout=5.0)

    turn = hub.get_turn(conversation_id, turn_id)
    assert turn is not None
    assert turn.status == "running"

    producer_tasks = hub.get_active_producer_tasks(conversation_id)
    assert producer_tasks, "Expected an active producer task to cancel"
    # Cancel while the producer is parked on the gated LLM await. The cancel
    # interrupts that await directly; we deliberately do NOT release the gate
    # first (releasing would let the LLM run to completion and race the
    # cancellation, leaving a producer using a torn-down DB context).
    for task in producer_tasks:
        task.cancel()
    await asyncio.gather(*producer_tasks, return_exceptions=True)
    release.set()

    # The turn must no longer be running, and a failed turn_ended must have
    # been published to subscribers.
    turn = hub.get_turn(conversation_id, turn_id)
    assert turn is not None
    assert turn.status != "running"

    ended_events: list[StreamEvent] = []
    while not handle.queue.empty():
        ended_events.append(handle.queue.get_nowait())
    assert any(
        e.type == "turn_ended" and e.payload.get("status") == "failed"
        for e in ended_events
    ), f"Expected a failed turn_ended after cancellation, got {ended_events}"

    hub.unsubscribe(conversation_id, handle.queue)


async def test_send_message_propagates_publish_failure(
    db_engine: AsyncEngine,
) -> None:
    """A hub publish failure in send_message must surface, not return None.

    The publish runs AFTER the DB commit, so swallowing its exception would make
    a durably-saved message look like a failed send — callers would then resend
    or retry an already-approved confirmation, causing duplicate side effects.
    """

    class _FailingPublishHub:
        async def publish(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("hub publish exploded")

    ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await ctx.init_vector_db()

    interface = WebChatInterface(
        db_engine,
        stream_hub=cast("Any", _FailingPublishHub()),
    )
    conversation_id = f"conv_pubfail_{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError, match="hub publish exploded"):
        await interface.send_message(
            conversation_id=conversation_id,
            text="committed but publish fails",
        )

    # The message was durably committed before the publish failure.
    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web",
        conversation_id=conversation_id,
        limit=20,
    )
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
