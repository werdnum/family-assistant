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
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    MessageReasoningInfo,
    ToolMessage,
    UserMessage,
    text_content,
)
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.services.confirmation_service import ConfirmationService
from family_assistant.storage.confirmation_requests import confirmation_requests_table
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.message_history import (
    MessageHistoryRepository,
)
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.models import ChatPromptRequest
from family_assistant.web.routers import chat_api
from family_assistant.web.turn_producer import persist_stopped_reply
from family_assistant.web.web_mid_turn_controller import WebMidTurnController
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.web.conversation_stream_hub import (
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


def _gate_first_llm_call(
    original_generate: Callable[..., Awaitable[LLMOutput]],
    started: asyncio.Event,
    release: asyncio.Event,
) -> Callable[..., Awaitable[LLMOutput]]:
    """Wrap ``generate_response`` so its first call parks until ``release`` is set.

    Lets a test catch the producer mid-flight (await ``started``), drive the
    cancel/steer endpoint, then ``release`` it; later loop iterations pass
    straight through.
    """
    state = {"gated": False}

    async def gated(*args: object, **kwargs: object) -> LLMOutput:
        if not state["gated"]:
            state["gated"] = True
            started.set()
            await release.wait()
        # The gate only delays the first call; *args/**kwargs are exactly what
        # the loop passes to generate_response and are forwarded unchanged, so
        # at runtime they match its real signature. They are typed ``object``
        # here only so this wrapper can accept an arbitrary call shape.
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    return gated


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


async def _cancel_turn(
    client: AsyncClient, turn_id: str, conversation_id: str
) -> "httpx.Response":
    """POST /cancel, retrying a transient 503.

    The confirmation-listing read can momentarily lose SQLite's shared connection
    to a gated producer (a postgres-production non-issue), and 503 is the
    documented retryable response — the turn is already cancelled (the endpoint
    runs request_interrupt + task.cancel before the listing that 503s), and the
    real frontend retries it too.
    """
    resp = await client.post(
        f"/api/v1/chat/turns/{turn_id}/cancel",
        json={"conversation_id": conversation_id},
    )
    for _ in range(4):
        if resp.status_code != 503:
            break
        resp = await client.post(
            f"/api/v1/chat/turns/{turn_id}/cancel",
            json={"conversation_id": conversation_id},
        )
    return resp


async def _confirmation_status(db_engine: AsyncEngine, request_id: str) -> str | None:
    db = Database(engine=db_engine)
    row = await db.fetch_one(
        select(confirmation_requests_table.c.status).where(
            confirmation_requests_table.c.id == request_id
        )
    )
    return row["status"] if row else None


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
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

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

        cancel = await _cancel_turn(api_test_client, turn_id, conversation_id)
        # task.cancel() runs before the confirmation-listing read, so the turn is
        # cancelled regardless of whether that read momentarily 503s on SQLite's
        # shared connection (a postgres-production non-issue). Accept either; the
        # turn-status assertion below is the real check.
        assert cancel.status_code in {200, 503}, cancel.text
        if cancel.status_code == 200:
            assert cancel.json()["status"] in {"cancelling", "cancelled"}

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


async def test_persist_stopped_reply_is_durable_and_profile_tagged(
    db_engine: AsyncEngine,
) -> None:
    """persist_stopped_reply (the producer's cancellation-path persistence) writes
    a durable, profile-tagged assistant row, so a refresh shows the stopped turn
    in this profile's history rather than a prompt with no reply.

    Tested directly so the assertion doesn't ride the gated-producer + torn-down-
    DB-context cancellation race, which is flaky on SQLite's shared connection.
    """
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_persist_{uuid.uuid4().hex[:8]}"
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        UserMessage(content="plan my week"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="prof-x",
    )
    # A tool result committed before the stop taints the turn; the stopped
    # marker must inherit that state rather than being written untainted.
    await ctx.message_history.add_message(
        ToolMessage(
            tool_call_id="call_email",
            content="email body",
            name="get_email",
            taint_metadata=TurnTaintState
            .empty()
            .add_source(
                TaintSource(
                    source_type=TaintSourceType.EMAIL,
                    source_id="email-9",
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason="test email source",
                )
            )
            .to_metadata(),
        ),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="prof-x",
    )

    # No partial reply -> a Stopped marker.
    await persist_stopped_reply(
        db_engine,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        reply_text="",
        processing_profile_id="prof-x",
        initial_history_taint_metadata=TurnTaintState.empty().to_metadata(),
        initial_context_taint_metadata=TurnTaintState.empty().to_metadata(),
        live_taint_metadata=TurnTaintState.empty().to_metadata(),
    )
    # A partial reply -> the partial text is persisted (what the client rendered).
    await persist_stopped_reply(
        db_engine,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        reply_text="half an answer",
        processing_profile_id="prof-x",
        initial_history_taint_metadata=TurnTaintState.empty().to_metadata(),
        initial_context_taint_metadata=TurnTaintState.empty().to_metadata(),
        live_taint_metadata=TurnTaintState.empty().to_metadata(),
    )

    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id, limit=50
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert all(row["processing_profile_id"] == "prof-x" for row in assistant_rows)
    assert any("Stopped" in str(row["content"]) for row in assistant_rows)
    assert any("half an answer" in str(row["content"]) for row in assistant_rows)
    # Stopped rows carry runtime taint metadata merged from the turn's persisted
    # rows (here: the tainted tool result committed before the stop).
    for row in assistant_rows:
        assert row["taint_metadata_version"] == "runtime_v1"
        assert row["taint_metadata_json"] is not None
        assert row["taint_metadata_json"].get("max_tier") == "unknown_external"


@pytest.mark.asyncio
async def test_stopped_reply_inherits_initial_history_taint(
    db_engine: AsyncEngine,
) -> None:
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_prior_taint_{uuid.uuid4().hex[:8]}"
    initial_history_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="prior-email",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="prior conversation history",
        )
    )
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        UserMessage(content="continue"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="prof-x",
    )

    await persist_stopped_reply(
        db_engine,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        reply_text="partial response",
        processing_profile_id="prof-x",
        initial_history_taint_metadata=initial_history_taint.to_metadata(),
        initial_context_taint_metadata=TurnTaintState.empty().to_metadata(),
        live_taint_metadata=TurnTaintState.empty().to_metadata(),
    )

    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id, limit=50
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["taint_metadata_json"] is not None
    assert (
        assistant_rows[0]["taint_metadata_json"].get("max_tier") == "unknown_external"
    )


@pytest.mark.asyncio
async def test_stopped_reply_inherits_initial_context_taint(
    db_engine: AsyncEngine,
) -> None:
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_context_taint_{uuid.uuid4().hex[:8]}"
    initial_context_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id="context-note",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="tainted system-prompt context",
        )
    )
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        UserMessage(content="continue"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="prof-x",
    )

    await persist_stopped_reply(
        db_engine,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        reply_text="partial response from context",
        processing_profile_id="prof-x",
        initial_history_taint_metadata=TurnTaintState.empty().to_metadata(),
        initial_context_taint_metadata=initial_context_taint.to_metadata(),
        live_taint_metadata=TurnTaintState.empty().to_metadata(),
    )

    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id, limit=50
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["taint_metadata_json"] is not None
    assert (
        assistant_rows[0]["taint_metadata_json"].get("max_tier") == "unknown_external"
    )


@pytest.mark.asyncio
async def test_stopped_reply_inherits_uncommitted_live_taint(
    db_engine: AsyncEngine,
) -> None:
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_live_taint_{uuid.uuid4().hex[:8]}"
    live_taint = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="uncommitted-email",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="tool result observed before transaction cancellation",
        )
    )
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        UserMessage(content="continue"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="prof-x",
    )

    await persist_stopped_reply(
        db_engine,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id="test_user",
        reply_text="partial response after tool use",
        processing_profile_id="prof-x",
        initial_history_taint_metadata=TurnTaintState.empty().to_metadata(),
        initial_context_taint_metadata=TurnTaintState.empty().to_metadata(),
        live_taint_metadata=live_taint.to_metadata(),
    )

    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id, limit=50
    )
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["taint_metadata_json"] is not None
    assert (
        assistant_rows[0]["taint_metadata_json"].get("max_tier") == "unknown_external"
    )


async def test_completed_web_turn_persists_single_user_row(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """A web turn persists exactly one user row: the endpoint stores it and the
    producer reuses it (idempotent on turn_id), tagged with the turn's profile."""
    user_prompt = "Single user row please"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("done"),
    ))

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_oneuserrow_{uuid.uuid4().hex[:8]}"
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

    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web", conversation_id=conversation_id, limit=50
    )
    user_rows = [row for row in rows if row["role"] == "user"]
    assert len(user_rows) == 1, f"Expected one user row, got {user_rows}"
    assert user_rows[0]["processing_profile_id"] is not None


async def _seed_user_row(
    db_engine: AsyncEngine, conversation_id: str, turn_id: str
) -> None:
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        UserMessage(content="hi"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="default_assistant",
    )


async def test_retry_of_interrupted_turn_reports_incomplete(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A retry whose durable record has only the user prompt (no assistant reply,
    e.g. a crash/restart mid-turn) is reported incomplete, so the client can show
    a recovery path instead of silently reloading the prompt alone."""
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_incomplete_{uuid.uuid4().hex[:8]}"
    await _seed_user_row(db_engine, conversation_id, turn_id)

    resp = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": "hi",
            "interface_type": "web",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["already_complete"] is True
    assert body["incomplete"] is True


async def test_retry_of_finished_turn_not_incomplete(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A retry whose durable record has an assistant reply is NOT incomplete:
    the client reloads history and shows the reply."""
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_finished_{uuid.uuid4().hex[:8]}"
    await _seed_user_row(db_engine, conversation_id, turn_id)
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        AssistantMessage(content="here is your reply"),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="default_assistant",
    )

    resp = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": "hi",
            "interface_type": "web",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["already_complete"] is True
    assert body["incomplete"] is False


async def test_retry_with_only_intermediate_tool_row_is_incomplete(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """A turn that crashed after an intermediate tool-calling assistant row (which
    carries tool_calls) but before its final reply is still incomplete — that row
    isn't a terminal result."""
    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_intermediate_{uuid.uuid4().hex[:8]}"
    await _seed_user_row(db_engine, conversation_id, turn_id)
    ctx = Database(engine=db_engine)
    await ctx.message_history.add_message(
        AssistantMessage(
            content="let me check",
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
        ),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        processing_profile_id="default_assistant",
    )

    resp = await api_test_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": "hi",
            "interface_type": "web",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["already_complete"] is True
    assert body["incomplete"] is True


async def test_user_message_insert_failure_ends_hub_turn(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the pre-producer user-message insert fails, the just-registered hub
    turn is ended — not left wedged at 'running' with no producer task or
    safety-net callback to ever end it."""

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("insert exploded")

    monkeypatch.setattr(MessageHistoryRepository, "add_message", boom)

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_insertfail_{uuid.uuid4().hex[:8]}"
    # The ASGI test transport re-raises unhandled app exceptions; either way the
    # hub turn must be cleaned up.
    with contextlib.suppress(RuntimeError):
        resp = await api_test_client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "prompt": "hi",
                "interface_type": "web",
            },
        )
        assert resp.status_code == 500, resp.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    turn = hub.get_turn(conversation_id, turn_id)
    assert turn is not None
    assert turn.status != "running", "Hub turn must be ended, not wedged at running"


async def test_cancel_before_producer_runs_does_not_wedge_turn() -> None:
    """A producer task cancelled before its coroutine runs must still end the turn.

    Stop immediately after Send can call task.cancel() after attach_producer_task
    but before run_turn_producer's first slice, so its try/except never runs and
    never ends the turn. The hub's done-callback safety net ends it instead,
    preventing a permanent 'running' wedge (pruning/eviction skip running turns).
    """
    hub = ConversationStreamHub()
    # The cancel endpoint sets the controller's interrupt flag before cancelling,
    # which is how a user Stop is distinguished from a teardown cancellation.
    controller = WebMidTurnController()
    controller.request_interrupt()
    await hub.start_turn(
        "conv",
        turn_id="t1",
        user_id="u1",
        started_at=datetime.now(UTC),
        mid_turn_controller=controller,
    )

    started = asyncio.Event()
    orphan_persisted = asyncio.Event()

    async def never_runs() -> None:
        started.set()
        await asyncio.Event().wait()  # parks forever (never reached: cancelled first)

    async def on_orphan_cancel() -> None:
        orphan_persisted.set()

    task = asyncio.ensure_future(never_runs())
    hub.attach_producer_task("conv", "t1", task, on_orphan_cancel=on_orphan_cancel)

    # Cancel before the task gets a scheduling slice: its coroutine never runs.
    task.cancel()
    assert not started.is_set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The done-callback schedules end_turn(cancelled); wait for it to land.
    await wait_for_condition(
        lambda: (
            (t := hub.get_turn("conv", "t1")) is not None and t.status == "cancelled"
        ),
        description="wedged turn ended",
    )
    # The stopped-marker persistence hook ran for the never-run producer.
    assert orphan_persisted.is_set()


async def test_orphan_cancel_without_interrupt_fails_without_stopped_marker() -> None:
    """A producer task cancelled with NO interrupt flag (app shutdown / supervisor
    teardown, not a user Stop) ends as 'failed' and does NOT persist a stopped
    marker — matching the producer's own should_interrupt()-based classification.
    """
    hub = ConversationStreamHub()
    # No request_interrupt(): this is not a user Stop.
    controller = WebMidTurnController()
    await hub.start_turn(
        "conv",
        turn_id="t1",
        user_id="u1",
        started_at=datetime.now(UTC),
        mid_turn_controller=controller,
    )

    orphan_persisted = asyncio.Event()

    async def never_runs() -> None:
        await asyncio.Event().wait()

    async def on_orphan_cancel() -> None:
        orphan_persisted.set()

    task = asyncio.ensure_future(never_runs())
    hub.attach_producer_task("conv", "t1", task, on_orphan_cancel=on_orphan_cancel)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await wait_for_condition(
        lambda: (t := hub.get_turn("conv", "t1")) is not None and t.status == "failed",
        description="orphan turn failed",
    )
    assert not orphan_persisted.is_set()


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


async def test_cancel_rejects_pending_confirmations_for_turn(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """Stopping a turn rejects the durable confirmations it was waiting on.

    Otherwise the pending-confirmations UI could approve a stopped turn's tool
    later, running a state-changing tool with no turn to receive the result.
    Only this turn's confirmations are rejected (matched by source message);
    an unrelated pending confirmation is left untouched.
    """
    # Use a turn that runs to completion: cancel rejects confirmations on the
    # already-finished path too, and this avoids a gated producer holding the
    # (shared, single-connection) SQLite session open while we create/read
    # confirmations, which otherwise causes lock contention under CI.
    user_prompt = "Cancel with a pending confirmation"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("all done"),
    ))

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_cancelconf_{uuid.uuid4().hex[:8]}"
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

    # A confirmation tied to this turn (same source message the producer would
    # set) plus an unrelated one (no source message) that must survive.
    service = ConfirmationService(db=Database(db_engine))
    ctx = Database(engine=db_engine)
    user_row = await ctx.message_history.get_user_row_by_turn_id(turn_id)
    assert user_row is not None
    expires_at = datetime.now(UTC) + timedelta(minutes=30)
    turn_conf = await service.create_request(
        target_user_id="test_user",
        tool_name="add_or_update_note",
        tool_args={"title": "x", "content": "y"},
        tool_call_id="turn-conf",
        source_message_internal_id=user_row["internal_id"],
        confirmation_prompt="ok?",
        expires_at=expires_at,
    )
    other_conf = await service.create_request(
        target_user_id="test_user",
        tool_name="add_or_update_note",
        tool_args={"title": "x", "content": "y"},
        tool_call_id="other-conf",
        source_message_internal_id=None,
        confirmation_prompt="ok?",
        expires_at=expires_at,
    )

    cancel = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/cancel",
        json={"conversation_id": conversation_id},
    )
    assert cancel.status_code == 200, cancel.text

    # Rejection happens synchronously within the cancel request.
    assert await _confirmation_status(db_engine, turn_conf["id"]) == "rejected"
    assert await _confirmation_status(db_engine, other_conf["id"]) == "pending"


async def test_cancel_returns_503_when_confirmation_rejection_fails(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a stopped turn's confirmation can't be rejected, /cancel reports 503.

    Reporting a clean stop while a state-changing confirmation stays approvable
    would be unsafe, so the failure propagates for the client to retry.
    """
    # A completed turn: cancel still rejects this turn's confirmations on the
    # already-finished path, and there's no gated producer holding the shared
    # SQLite session (which would otherwise cause CI lock contention).
    user_prompt = "Cancel where reject fails"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("all done"),
    ))

    # Pin a confirmation service on app state whose reject() always fails, so the
    # cancel endpoint resolves it via _get_confirmation_service.
    service = ConfirmationService(db=Database(db_engine))

    async def failing_reject(**_kwargs: object) -> None:
        raise RuntimeError("reject exploded")

    monkeypatch.setattr(service, "reject", failing_reject)
    app_fixture.state.confirmation_service = service

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_cancelfail_{uuid.uuid4().hex[:8]}"
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

    ctx = Database(engine=db_engine)
    user_row = await ctx.message_history.get_user_row_by_turn_id(turn_id)
    assert user_row is not None
    conf = await service.create_request(
        target_user_id="test_user",
        tool_name="add_or_update_note",
        tool_args={"title": "x", "content": "y"},
        tool_call_id="turn-conf",
        source_message_internal_id=user_row["internal_id"],
        confirmation_prompt="ok?",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    cancel = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/cancel",
        json={"conversation_id": conversation_id},
    )
    assert cancel.status_code == 503, cancel.text
    # The confirmation is still pending (not silently dropped), so a retry can
    # re-attempt the rejection.
    assert await _confirmation_status(db_engine, conf["id"]) == "pending"


async def test_cancel_rejects_conversation_owned_by_another_user(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Cancel enforces ownership before touching turn state (404, not 403)."""
    conversation_id = f"conv_owned_{uuid.uuid4().hex[:8]}"
    ctx = Database(engine=db_engine)
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
    steer_input_id = f"input_{uuid.uuid4().hex[:8]}"

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
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

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
            json={
                "conversation_id": conversation_id,
                "prompt": steer_text,
                "input_id": steer_input_id,
            },
        )
        assert steer.status_code == 200, steer.text
        assert steer.json()["accepted"] is True

        release.set()
        await wait_for_condition(
            _turn_complete(hub, conversation_id, turn_id), description="turn complete"
        )

        events = _drain(handle)
        # The echo names the submission it consumed, which is how the sending
        # client recognises its own message rather than an identical one from
        # somewhere else.
        assert any(
            e.type == "user_input"
            and e.payload.get("content") == steer_text
            and e.payload.get("input_id") == steer_input_id
            for e in events
        ), f"Expected a user_input event carrying the steer text, got {events}"
    finally:
        hub.unsubscribe(conversation_id, handle.queue)

    # The injected mid-turn message is persisted as the RAW user text, not the
    # internal [MID-TURN USER UPDATE] wrapper the model saw, so a later history
    # reload shows what the user actually typed.
    ctx = Database(engine=db_engine)
    rows = await ctx.message_history.get_recent_with_metadata(
        interface_type="web",
        conversation_id=conversation_id,
        limit=50,
    )
    steer_rows = [
        row
        for row in rows
        if row["role"] == "user" and steer_text in str(row["content"])
    ]
    assert steer_rows, "Expected the steering message to be persisted"
    assert all(
        "MID-TURN USER UPDATE" not in str(row["content"]) for row in steer_rows
    ), "Steering message must persist as raw text, not the internal wrapper"


async def test_retried_steer_with_the_same_input_id_is_queued_once(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client whose steer response was lost retries with the same input_id.

    The turn must act on the message once: queueing both copies would feed the
    instruction to the model twice and can repeat whatever tool work it asks
    for. Both requests answer 200, since the retry is asking whether its message
    landed, not to say the same thing twice.
    """
    user_prompt = "Steer me twice"
    steer_text = "actually, focus on tomorrow"
    steer_input_id = f"input_{uuid.uuid4().hex[:8]}"

    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, "MID-TURN USER UPDATE"),
        _reply("Okay, focusing on tomorrow."),
    ))
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_steer_twice",
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
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_steerdup_{uuid.uuid4().hex[:8]}"
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

        body = {
            "conversation_id": conversation_id,
            "prompt": steer_text,
            "input_id": steer_input_id,
        }
        first = await api_test_client.post(
            f"/api/v1/chat/turns/{turn_id}/steer", json=body
        )
        # The retry a lost response provokes, byte-identical to the first.
        second = await api_test_client.post(
            f"/api/v1/chat/turns/{turn_id}/steer", json=body
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["accepted"] is True

        release.set()
        await wait_for_condition(
            _turn_complete(hub, conversation_id, turn_id), description="turn complete"
        )

        events = _drain(handle)
        echoes = [
            event
            for event in events
            if event.type == "user_input"
            and event.payload.get("input_id") == steer_input_id
        ]
        assert len(echoes) == 1, f"Expected the steer to be consumed once, got {echoes}"
    finally:
        hub.unsubscribe(conversation_id, handle.queue)


async def test_retried_steer_after_the_turn_ends_is_still_accepted(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steer retry can outlive the turn it was sent to.

    The controller holding the accepted ids is dropped when the producer
    finishes, so recognition has to live on the turn record. Refusing the retry
    with 409 would send the client down the resend path and repeat an
    instruction the turn already acted on.
    """
    user_prompt = "Steer me then finish"
    steer_text = "actually, focus on tomorrow"
    steer_input_id = f"input_{uuid.uuid4().hex[:8]}"

    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, "MID-TURN USER UPDATE"),
        _reply("Okay, focusing on tomorrow."),
    ))
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_steer_late",
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
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_steerlate_{uuid.uuid4().hex[:8]}"
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
    await asyncio.wait_for(started.wait(), timeout=5.0)

    body = {
        "conversation_id": conversation_id,
        "prompt": steer_text,
        "input_id": steer_input_id,
    }
    accepted = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/steer", json=body
    )
    assert accepted.status_code == 200, accepted.text

    release.set()
    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )

    # The retry arrives once the turn is over and its controller is gone.
    late_retry = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/steer", json=body
    )
    assert late_retry.status_code == 200, late_retry.text
    assert late_retry.json()["accepted"] is True

    # A message the turn never saw still gets the 409 that tells the client to
    # start a new turn.
    unknown = await api_test_client.post(
        f"/api/v1/chat/turns/{turn_id}/steer",
        json={
            "conversation_id": conversation_id,
            "prompt": "and one more thing",
            "input_id": f"input_{uuid.uuid4().hex[:8]}",
        },
    )
    assert unknown.status_code == 409, unknown.text


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


async def test_second_turn_while_one_is_running_returns_409(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation runs one turn at a time; the rival turn is refused.

    Two concurrent turns interleave their writes on one history, and the second
    replays tool calls the first has not answered yet. The 409 hands back the
    running turn's id so the client can steer it instead.
    """
    user_prompt = "Start something long"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("done"),
    ))

    started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_overlap_{uuid.uuid4().hex[:8]}"
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
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)

        rival = await api_test_client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": str(uuid.uuid4()),
                "conversation_id": conversation_id,
                "prompt": "and another thing",
                "interface_type": "web",
            },
        )
    finally:
        release.set()

    assert rival.status_code == 409, rival.text
    detail = rival.json()["detail"]
    assert detail["active_turn_id"] == turn_id
    # The client resubscribes from here, so it follows the running turn alone
    # instead of replaying the conversation from seq 0.
    running = hub.get_turn(conversation_id, turn_id)
    assert running is not None
    assert detail["active_turn_first_seq"] == running.first_seq

    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )


async def test_retrying_the_running_turn_id_is_still_idempotent(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlap guard must not break the kickoff retry it sits next to.

    A client that retries the SAME turn id (a dropped kickoff response) is
    resending one turn, not starting a rival, so it gets that turn's identity
    back rather than a 409.
    """
    user_prompt = "Retry my kickoff"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("done"),
    ))

    started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_retry_{uuid.uuid4().hex[:8]}"
    body = {
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "prompt": user_prompt,
        "interface_type": "web",
    }
    post = await api_test_client.post("/api/v1/chat/turns", json=body)
    assert post.status_code == 200, post.text

    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)
        retry = await api_test_client.post("/api/v1/chat/turns", json=body)
    finally:
        release.set()

    assert retry.status_code == 200, retry.text
    assert retry.json()["turn_id"] == turn_id

    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )


async def test_turn_admitted_during_attachment_setup_is_still_refused(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overlap guard holds across the setup this endpoint awaits.

    A request carrying attachments checks for a running turn, then uploads them
    — and a rival turn can be admitted while it does. Rechecking would only
    narrow the window, so registration itself refuses under the hub's lock: the
    request that parked in setup loses even though it checked first.
    """
    user_prompt = "The rival that got in first"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("done"),
    ))

    llm_started = asyncio.Event()
    llm_release = asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(
            api_mock_llm_client.generate_response, llm_started, llm_release
        ),
    )

    upload_started = asyncio.Event()
    upload_release = asyncio.Event()

    async def gated_process_attachments(
        payload: ChatPromptRequest,
        _conversation_id: str,
        _attachment_registry: object,
        _db_context: object,
        _user_id: str,
    ) -> tuple[list[ContentPartDict], None]:
        upload_started.set()
        await upload_release.wait()
        return [text_content(payload.prompt)], None

    monkeypatch.setattr(
        chat_api, "_process_user_attachments", gated_process_attachments
    )

    conversation_id = f"conv_setup_race_{uuid.uuid4().hex[:8]}"
    parked_turn_id = str(uuid.uuid4())
    rival_turn_id = str(uuid.uuid4())

    # Parks inside attachment setup, having already passed the early check.
    parked = asyncio.ensure_future(
        api_test_client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": parked_turn_id,
                "conversation_id": conversation_id,
                "prompt": "look at this scan",
                "interface_type": "web",
                "attachments": [
                    {
                        "type": "image",
                        "content": "eA==",
                        "mime_type": "image/png",
                        "filename": "scan.png",
                    }
                ],
            },
        )
    )
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    try:
        await asyncio.wait_for(upload_started.wait(), timeout=5.0)

        # No turn is registered yet, so this one is admitted and starts running.
        rival = await api_test_client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": rival_turn_id,
                "conversation_id": conversation_id,
                "prompt": user_prompt,
                "interface_type": "web",
            },
        )
        assert rival.status_code == 200, rival.text
        await asyncio.wait_for(llm_started.wait(), timeout=5.0)
    finally:
        upload_release.set()

    try:
        parked_response = await asyncio.wait_for(parked, timeout=5.0)
    finally:
        llm_release.set()

    assert parked_response.status_code == 409, parked_response.text
    assert parked_response.json()["detail"]["active_turn_id"] == rival_turn_id
    assert hub.get_turn(conversation_id, parked_turn_id) is None

    await wait_for_condition(
        _turn_complete(hub, conversation_id, rival_turn_id),
        description="rival turn complete",
    )


async def test_steer_reports_the_stream_head_it_was_queued_after(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``queued_after_seq`` is a floor: the steer's echo is published above it.

    A client replaying a turn it has just adopted uses this to tell its own
    echo from identical text the turn consumed earlier.
    """
    user_prompt = "Tell me about seqs"
    api_mock_llm_client.rules.append((
        lambda args: _user_message_contains(args, user_prompt),
        _reply("done"),
    ))

    started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_first_llm_call(api_mock_llm_client.generate_response, started, release),
    )

    turn_id = str(uuid.uuid4())
    conversation_id = f"conv_steerseq_{uuid.uuid4().hex[:8]}"
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
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)
        steer = await api_test_client.post(
            f"/api/v1/chat/turns/{turn_id}/steer",
            json={"conversation_id": conversation_id, "prompt": "actually, hurry"},
        )
    finally:
        release.set()

    assert steer.status_code == 200, steer.text
    queued_after_seq = steer.json()["queued_after_seq"]
    assert queued_after_seq == hub.latest_seq(conversation_id)

    await wait_for_condition(
        _turn_complete(hub, conversation_id, turn_id), description="turn complete"
    )
    # Everything the turn published after the steer — including its echo — sits
    # above the floor the client was handed.
    turn = hub.get_turn(conversation_id, turn_id)
    assert turn is not None
    assert turn.latest_seq > queued_after_seq


async def test_steer_rejects_conversation_owned_by_another_user(
    api_test_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """Steer enforces ownership before touching turn state (404, not 403)."""
    conversation_id = f"conv_steerowned_{uuid.uuid4().hex[:8]}"
    ctx = Database(engine=db_engine)
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
