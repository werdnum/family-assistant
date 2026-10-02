"""Web streaming turns survive the process that ran them going away.

Covers the web side of docs/design/turn-resumption-across-restarts.md: a turn
started through ``POST /v1/chat/turns`` holds a lease while it runs, a graceful
shutdown suspends it without closing it as stopped, and a due lease relaunches
the turn from the rows its interrupted run persisted.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMOutput, ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageReasoningInfo,
    ToolMessage,
    UserMessage,
    is_turn_scaffolding,
)
from family_assistant.llm.model_selection import (
    ResolvedModelSelection,
    stamp_model_selection,
)
from family_assistant.services.turn_resumption import (
    TURN_RESUME_TASK_TYPE,
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.turn_resumption import (
    WEB_STREAM_RESUMER,
    WebTurnResumer,
    resumed_model_selection,
)
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import RuleBasedMockLLMClient

PROFILE = "chat_api_test_profile"
USER = "test_user"
PROMPT = "What's on my notes list?"
RESUMED_REPLY = "Here is what I found after the restart."


@pytest.fixture
def lease_registry(app_fixture: FastAPI) -> TurnLeaseRegistry:
    registry = TurnLeaseRegistry()
    registry.register_resumer(WEB_STREAM_RESUMER, WebTurnResumer(app_fixture.state))
    app_fixture.state.turn_lease_registry = registry
    return registry


def _usage() -> MessageReasoningInfo:
    return MessageReasoningInfo(prompt_tokens=10, completion_tokens=10, total_tokens=20)


def _gate_llm(
    original: Callable[..., Awaitable[LLMOutput]],
    started: asyncio.Event,
    release: asyncio.Event,
) -> Callable[..., Awaitable[LLMOutput]]:
    async def gated(*args: object, **kwargs: object) -> LLMOutput:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    return gated


def _ids() -> tuple[str, str]:
    return f"conv_resume_{uuid.uuid4().hex[:8]}", str(uuid.uuid4())


async def _start_turn(client: AsyncClient, conversation_id: str, turn_id: str) -> None:
    response = await client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "prompt": PROMPT,
            "interface_type": "web",
        },
    )
    assert response.status_code == 200, response.text


async def _pending_leases(db: Database, turn_id: str) -> list[TurnResumePayload]:
    rows = await db.tasks.get_all(task_type=TURN_RESUME_TASK_TYPE, status="pending")
    payloads = [TurnResumePayload.model_validate(row["payload"]) for row in rows]
    return [payload for payload in payloads if payload.turn_id == turn_id]


async def _seed_turn_interrupted_after_tool(
    db: Database, conversation_id: str, turn_id: str
) -> None:
    """Rows a turn leaves behind when its process dies after a tool round."""
    for message in (
        UserMessage.from_trusted_user(content=PROMPT),
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_1",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
        ),
        ToolMessage(tool_call_id="call_1", name="list_notes", content="No notes."),
    ):
        await db.message_history.add_message(
            message,
            interface_type="web",
            conversation_id=conversation_id,
            turn_id=turn_id,
            timestamp=datetime.now(UTC),
            user_id=USER,
            processing_profile_id=PROFILE,
        )


async def _add_turn_row(
    db: Database,
    message: UserMessage | AssistantMessage | ToolMessage,
    conversation_id: str,
    turn_id: str,
) -> None:
    await db.message_history.add_message(
        message,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id=USER,
        processing_profile_id=PROFILE,
    )


async def _add_tool_rounds(
    db: Database, conversation_id: str, turn_id: str, *, rounds: int
) -> None:
    for _ in range(rounds):
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        await _add_turn_row(
            db,
            AssistantMessage(
                content="",
                tool_calls=[
                    ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name="list_notes", arguments="{}"),
                    )
                ],
            ),
            conversation_id,
            turn_id,
        )
        await _add_turn_row(
            db,
            ToolMessage(tool_call_id=call_id, name="list_notes", content="No notes."),
            conversation_id,
            turn_id,
        )


def _exec_context(db: Database) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="unknown",
        conversation_id="unknown",
        user_name="task_worker",
        turn_id=None,
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _resume(
    registry: TurnLeaseRegistry, db: Database, conversation_id: str, turn_id: str
) -> None:
    payload = TurnResumePayload(
        resumer=WEB_STREAM_RESUMER,
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_id=USER,
        user_name="Test User",
        processing_profile_id=PROFILE,
    )
    await registry.handle_resume_task(
        _exec_context(db), payload.model_dump(mode="json")
    )


def _turn_status(
    hub: ConversationStreamHub, conversation_id: str, turn_id: str, status: str
) -> Callable[[], bool]:
    def check() -> bool:
        turn = hub.get_turn(conversation_id, turn_id)
        return turn is not None and turn.status == status

    return check


def _reply_after_tool_result(args: dict) -> bool:
    return any(message.role == "tool" for message in args.get("messages", []))


async def test_running_turn_holds_a_lease(
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_llm(api_mock_llm_client.generate_response, started, release),
    )
    conversation_id, turn_id = _ids()
    await _start_turn(api_test_client, conversation_id, turn_id)
    await asyncio.wait_for(started.wait(), timeout=5.0)

    leases = await _pending_leases(Database(db_engine), turn_id)

    release.set()
    assert [(lease.conversation_id, lease.attempt) for lease in leases] == [
        (conversation_id, 0)
    ]
    assert lease_registry.is_live(turn_id)


async def test_completed_turn_releases_its_lease(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _start_turn(api_test_client, conversation_id, turn_id)
    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="turn complete",
    )

    async def lease_released() -> bool:
        return not await _pending_leases(db, turn_id)

    await wait_for_condition(lease_released, description="lease released")


async def test_suspended_turn_is_not_closed_as_stopped(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown leaves the turn open for another process: no Stopped marker,
    no turn_ended, and its lease still pending."""
    started, never = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_llm(api_mock_llm_client.generate_response, started, never),
    )
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _start_turn(api_test_client, conversation_id, turn_id)
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await lease_registry.suspend_all(grace_seconds=0.1, teardown_seconds=5.0)

    rows = await db.message_history.get_by_turn_id(turn_id)
    assert [row.role for row in rows] == ["user"]
    turn = hub.get_turn(conversation_id, turn_id)
    assert turn is not None
    assert turn.status == "running"
    assert len(await _pending_leases(db, turn_id)) == 1


async def test_resumed_turn_finishes_from_its_persisted_rows(
    app_fixture: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    api_mock_llm_client.rules.append((
        _reply_after_tool_result,
        LLMOutput(content=RESUMED_REPLY, tool_calls=None, reasoning_info=_usage()),
    ))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)

    await _resume(lease_registry, db, conversation_id, turn_id)

    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="resumed turn complete",
    )
    rows = await db.message_history.get_by_turn_id(turn_id)
    assert [row.role for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert isinstance(rows[-1], AssistantMessage)
    assert rows[-1].content == RESUMED_REPLY


async def test_resumed_turn_puts_turn_context_back_after_the_prompt(
    app_fixture: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    """The model sees the turn in its original shape: prompt, context block,
    then the tool round the interrupted run already did."""
    shapes: list[list[str]] = []

    def record_shape(args: dict) -> bool:
        shapes.append([
            "context" if is_turn_scaffolding(message) else message.role
            for message in args["messages"]
            if message.role != "system"
        ])
        return True

    api_mock_llm_client.rules.append((
        record_shape,
        LLMOutput(content=RESUMED_REPLY, tool_calls=None, reasoning_info=_usage()),
    ))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)

    await _resume(lease_registry, db, conversation_id, turn_id)

    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="resumed turn complete",
    )
    assert shapes == [["user", "context", "assistant", "tool"]]


async def test_suspension_lets_the_running_round_record_its_result(
    app_fixture: FastAPI,
    api_test_client: AsyncClient,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn asked to suspend partway through a round stops at the next loop
    boundary, so the tool it called is recorded rather than abandoned."""
    api_mock_llm_client.rules.append((
        lambda args: True,
        LLMOutput(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_safe_point",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
            reasoning_info=_usage(),
        ),
    ))
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        api_mock_llm_client,
        "generate_response",
        _gate_llm(api_mock_llm_client.generate_response, started, release),
    )
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _start_turn(api_test_client, conversation_id, turn_id)
    await asyncio.wait_for(started.wait(), timeout=5.0)
    suspension = asyncio.create_task(lease_registry.suspend_all(grace_seconds=30.0))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub

    def suspension_requested() -> bool:
        turn = hub.get_turn(conversation_id, turn_id)
        return turn is not None and turn.suspended

    await wait_for_condition(suspension_requested, description="suspension requested")

    release.set()
    await asyncio.wait_for(suspension, timeout=20.0)

    rows = await db.message_history.get_by_turn_id(turn_id)
    assert [row.role for row in rows] == ["user", "assistant", "tool"]


async def test_resumed_turn_longer_than_the_history_window_keeps_its_prompt(
    app_fixture: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    """A turn whose own rows outgrow the history window is replayed whole, so
    the resumed model still sees the request it is answering."""
    prompts_seen: list[bool] = []

    def record_prompt(args: dict) -> bool:
        prompts_seen.append(
            any(
                message.role == "user" and message.content == PROMPT
                for message in args["messages"]
            )
        )
        return True

    api_mock_llm_client.rules.append((
        record_prompt,
        LLMOutput(content=RESUMED_REPLY, tool_calls=None, reasoning_info=_usage()),
    ))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    # Three tool rounds after the prompt: seven rows, against a window of five.
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)
    await _add_tool_rounds(db, conversation_id, turn_id, rounds=2)

    await _resume(lease_registry, db, conversation_id, turn_id)

    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="resumed turn complete",
    )
    assert prompts_seen == [True]


async def test_resumed_turn_stays_on_the_tier_its_rows_ran_on(
    db_engine: AsyncEngine,
) -> None:
    """Under Auto the lease holds the unrouted default; the continuation must
    use the routed tier stamped on the turn's rows instead."""
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    routed = ResolvedModelSelection(
        tier="deep",
        requested=None,
        source="default",
        routing_outcome="decided",
        classifier_model="classifier",
    )
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)
    await db.message_history.add_message(
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCallItem(
                    id="call_routed",
                    type="function",
                    function=ToolCallFunction(name="list_notes", arguments="{}"),
                )
            ],
        ),
        interface_type="web",
        conversation_id=conversation_id,
        turn_id=turn_id,
        timestamp=datetime.now(UTC),
        user_id=USER,
        processing_profile_id=PROFILE,
        reasoning_info=stamp_model_selection(_usage(), routed),
    )
    admitted_default = ResolvedModelSelection.unselected("standard")

    selection = await resumed_model_selection(
        db,
        TurnResumePayload(
            resumer=WEB_STREAM_RESUMER,
            interface_type="web",
            conversation_id=conversation_id,
            turn_id=turn_id,
            user_id=USER,
            user_name="Test User",
            processing_profile_id=PROFILE,
            model_selection=admitted_default.to_json(),
        ),
    )

    assert selection == routed.freeze()


async def test_resumed_turn_puts_turn_context_after_the_opening_prompt_not_a_steer(
    app_fixture: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    """A steering message accepted later in the turn is not where the turn
    began; the context block goes back after the opening prompt."""
    shapes: list[list[str]] = []

    def record_shape(args: dict) -> bool:
        shapes.append([
            "context" if is_turn_scaffolding(message) else message.role
            for message in args["messages"]
            if message.role != "system"
        ])
        return True

    api_mock_llm_client.rules.append((
        record_shape,
        LLMOutput(content=RESUMED_REPLY, tool_calls=None, reasoning_info=_usage()),
    ))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)
    await _add_turn_row(
        db,
        UserMessage.from_trusted_user(content="also check tomorrow"),
        conversation_id,
        turn_id,
    )

    await _resume(lease_registry, db, conversation_id, turn_id)

    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="resumed turn complete",
    )
    assert shapes == [["user", "context", "assistant", "tool", "user"]]


async def test_resumed_turn_continues_on_its_remaining_iteration_budget(
    app_fixture: FastAPI,
    api_mock_llm_client: RuleBasedMockLLMClient,
    lease_registry: TurnLeaseRegistry,
    db_engine: AsyncEngine,
) -> None:
    """A turn that had used its whole budget before the restart gets only the
    final, tool-less iteration -- not a fresh allowance."""
    offered_tools: list[object] = []

    def record_tools(args: dict) -> bool:
        offered_tools.append(args.get("tools"))
        return True

    api_mock_llm_client.rules.append((
        record_tools,
        LLMOutput(content=RESUMED_REPLY, tool_calls=None, reasoning_info=_usage()),
    ))
    hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub
    db = Database(db_engine)
    conversation_id, turn_id = _ids()
    # The test profile allows five iterations; all five ran tools.
    await _seed_turn_interrupted_after_tool(db, conversation_id, turn_id)
    await _add_tool_rounds(db, conversation_id, turn_id, rounds=4)

    await _resume(lease_registry, db, conversation_id, turn_id)

    await wait_for_condition(
        _turn_status(hub, conversation_id, turn_id, "complete"),
        description="resumed turn complete",
    )
    assert offered_tools == [None]
