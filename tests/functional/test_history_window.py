"""The prompt's history window is built from whole turns against a size budget.

See docs/design/history-compaction.md.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import LocalToolsProvider
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.utils.clock import MockClock
    from tests.mocks.mock_llm import MatcherArgs

PROFILE_ID = "history-window"
CONVERSATION_ID = "history-window-conversation"


def _user(content: str) -> UserMessage:
    empty = TurnTaintState.empty().to_metadata()
    return UserMessage(
        content=content, taint_metadata=empty, authorship_taint_metadata=empty
    )


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[list[LLMMessage]] = []

    def __call__(self, args: MatcherArgs) -> bool:
        self.requests.append(list(args["messages"]))
        return True


def _service(
    mock_clock: MockClock,
    recorder: _Recorder,
    *,
    budget_chars: int,
    min_turns: int = 1,
    max_age_hours: float = 24,
    profile_id: str = PROFILE_ID,
) -> ProcessingService:
    return ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[(recorder, LLMOutput(content="Noted.", tool_calls=None))]
        ),
        tools_provider=LocalToolsProvider(registrations=[]),
        service_config=ProcessingServiceConfig(
            prompts={"system_prompt": "You are a test assistant."},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=budget_chars,
            history_min_turns=min_turns,
            history_max_age_hours=max_age_hours,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.CONFIRM,
            id=profile_id,
        ),
        context_providers=[],
        server_url=None,
        app_config=AppConfig(),
        clock=mock_clock,
    )


async def _seed_turn(
    db: Database,
    mock_clock: MockClock,
    request: str,
    answer: str,
    *,
    tool_calls: int = 0,
    tool_result: str = "result",
    interface_type: str = "telegram",
    profile_id: str = PROFILE_ID,
    thread_root_id: int | None = None,
    interface_message_id: str | None = None,
) -> int:
    """Store one completed turn; returns its user row's internal id."""
    turn_id = str(uuid.uuid4())
    common = {
        "interface_type": interface_type,
        "conversation_id": CONVERSATION_ID,
        "turn_id": turn_id,
        "processing_profile_id": profile_id,
        "thread_root_id": thread_root_id,
    }
    user_row = await db.message_history.add_message(
        _user(request),
        timestamp=mock_clock.now(),
        interface_message_id=interface_message_id,
        **common,
    )
    for index in range(tool_calls):
        mock_clock.advance(timedelta(seconds=1))
        call_id = f"{turn_id}-{index}"
        await db.message_history.add_message(
            AssistantMessage(
                content=None,
                tool_calls=[
                    ToolCallItem(
                        id=call_id,
                        type="function",
                        function=ToolCallFunction(name="lookup", arguments="{}"),
                    )
                ],
            ),
            timestamp=mock_clock.now(),
            **common,
        )
        await db.message_history.add_message(
            ToolMessage(tool_call_id=call_id, name="lookup", content=tool_result),
            timestamp=mock_clock.now(),
            **common,
        )
    mock_clock.advance(timedelta(seconds=1))
    await db.message_history.add_message(
        AssistantMessage(content=answer), timestamp=mock_clock.now(), **common
    )
    mock_clock.advance(timedelta(minutes=1))
    return user_row


async def _run(
    service: ProcessingService,
    db: Database,
    text: str,
    *,
    interface_type: str = "telegram",
    replied_to_interface_id: str | None = None,
) -> None:
    result = await service.handle_chat_interaction(
        db_context=db,
        interface_type=interface_type,
        conversation_id=CONVERSATION_ID,
        trigger_content_parts=[{"type": "text", "text": text}],
        trigger_interface_message_id=None,
        user_name="Alice",
        replied_to_interface_id=replied_to_interface_id,
    )
    assert result.status.value == "success"


def _texts(messages: list[LLMMessage]) -> str:
    return "\n".join(
        message.content
        for message in messages
        if isinstance(message, UserMessage | AssistantMessage | ToolMessage)
        and isinstance(message.content, str)
    )


@pytest.mark.asyncio
async def test_a_tool_heavy_turn_does_not_push_out_the_request_before_it(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(db, mock_clock, "Get me two plumber quotes", "Here are two.")
    await _seed_turn(
        db, mock_clock, "Check the calendar", "You are free.", tool_calls=4
    )
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=40_000), db, "And the other one?"
    )

    text = _texts(recorder.requests[0])
    assert "Get me two plumber quotes" in text
    assert "Here are two." in text
    assert "Check the calendar" in text


@pytest.mark.asyncio
async def test_the_window_never_splits_a_turn(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(
        db, mock_clock, "Old request", "Old answer", tool_calls=3, tool_result="x" * 400
    )
    await _seed_turn(db, mock_clock, "Newer request", "Newer answer")
    recorder = _Recorder()
    # Room for the newer turn and some, but not all, of the older one's rows.
    await _run(_service(mock_clock, recorder, budget_chars=700), db, "Now this")

    request = recorder.requests[0]
    assert "Old" not in _texts(request)
    assert not any(isinstance(message, ToolMessage) for message in request)
    history = request[1:]
    assert isinstance(history[0], UserMessage)
    assert "Newer request" in _texts(history)


@pytest.mark.asyncio
async def test_min_turns_outrank_the_budget(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(db, mock_clock, "Big request", "Big answer " + "y" * 5_000)
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=100, min_turns=1), db, "Next"
    )

    assert "Big request" in _texts(recorder.requests[0])


@pytest.mark.asyncio
async def test_a_turn_older_than_the_age_cap_is_left_out(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(db, mock_clock, "Yesterday's request", "Yesterday's answer")
    mock_clock.advance(timedelta(hours=3))
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=40_000, max_age_hours=2),
        db,
        "Today",
    )

    assert "Yesterday" not in _texts(recorder.requests[0])


@pytest.mark.asyncio
async def test_consecutive_turns_render_as_prefix_plus_append(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(db, mock_clock, "Earlier", "Earlier answer", tool_calls=2)
    recorder = _Recorder()
    service = _service(mock_clock, recorder, budget_chars=40_000)
    await _run(service, db, "First question")
    mock_clock.advance(timedelta(minutes=1))
    await _run(service, db, "Second question")

    first, second = recorder.requests[0], recorder.requests[1]
    assert second[: len(first)] == first
    assert len(second) > len(first)


@pytest.mark.asyncio
async def test_a_past_error_is_replayed_as_one_line(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    turn_id = str(uuid.uuid4())
    for message in (
        _user("Do the thing"),
        ErrorMessage(
            content="Tool failed\nwith detail", error_traceback="Traceback: boom"
        ),
    ):
        await db.message_history.add_message(
            message,
            interface_type="telegram",
            conversation_id=CONVERSATION_ID,
            timestamp=mock_clock.now(),
            turn_id=turn_id,
            processing_profile_id=PROFILE_ID,
        )
        mock_clock.advance(timedelta(seconds=1))
    recorder = _Recorder()
    await _run(_service(mock_clock, recorder, budget_chars=40_000), db, "Again")

    text = _texts(recorder.requests[0])
    assert "I encountered an error: Tool failed" in text
    assert "with detail" not in text
    assert "Traceback" not in text


@pytest.mark.asyncio
async def test_a_thread_reply_loads_the_thread_on_this_profile_only(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    root = await _seed_turn(
        db,
        mock_clock,
        "Thread opener",
        "Opener answer",
        interface_message_id="tg-1",
    )
    await _seed_turn(
        db,
        mock_clock,
        "Other profile in thread",
        "Other profile answer",
        profile_id="someone-else",
        thread_root_id=root,
    )
    # Long enough ago that the age cap alone would leave the thread out.
    mock_clock.advance(timedelta(hours=5))
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=40_000, max_age_hours=2),
        db,
        "Following up",
        replied_to_interface_id="tg-1",
    )

    text = _texts(recorder.requests[0])
    assert "Thread opener" in text
    assert "Opener answer" in text
    assert "Other profile" not in text


@pytest.mark.asyncio
async def test_a_reply_never_loads_another_conversations_rows(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    # A turn elsewhere that messaged this conversation, under its own turn id.
    sender_turn = str(uuid.uuid4())
    await db.message_history.add_message(
        _user("Tell my partner dinner is at seven"),
        interface_type="telegram",
        conversation_id="sender-conversation",
        timestamp=mock_clock.now(),
        turn_id=sender_turn,
        processing_profile_id=PROFILE_ID,
    )
    notification = await db.message_history.add_message(
        AssistantMessage(content="Dinner is at seven."),
        interface_type="telegram",
        conversation_id=CONVERSATION_ID,
        timestamp=mock_clock.now(),
        turn_id=sender_turn,
        processing_profile_id=PROFILE_ID,
        interface_message_id="tg-notify",
    )
    mock_clock.advance(timedelta(minutes=1))
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=40_000),
        db,
        "Thanks",
        replied_to_interface_id="tg-notify",
    )

    text = _texts(recorder.requests[0])
    assert notification
    assert "Dinner is at seven." in text
    assert "Tell my partner" not in text


@pytest.mark.asyncio
async def test_thread_turns_count_toward_the_minimum(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_turn(db, mock_clock, "Unrelated big request", "z" * 5_000)
    root = await _seed_turn(
        db, mock_clock, "Thread opener", "Opener answer", interface_message_id="tg-2"
    )
    await _seed_turn(
        db, mock_clock, "Thread follow-up", "Follow-up answer", thread_root_id=root
    )
    recorder = _Recorder()
    await _run(
        _service(mock_clock, recorder, budget_chars=500, min_turns=2),
        db,
        "Replying",
        replied_to_interface_id="tg-2",
    )

    text = _texts(recorder.requests[0])
    assert "Thread follow-up" in text
    assert "Unrelated big request" not in text
