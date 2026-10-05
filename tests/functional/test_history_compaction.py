"""History compaction events: the window changes only at events, and only so.

See docs/design/history-compaction.md.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.base import ContextLengthError
from family_assistant.llm.messages import AssistantMessage, ToolMessage
from family_assistant.storage.database import Database
from family_assistant.storage.history_compaction import HistoryScope
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from tests.functional.history_window_helpers import (
    CONVERSATION_ID,
    PROFILE_ID,
    Recorder,
    make_service,
    run_turn,
    seed_turn,
    texts,
    user_message,
)
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition
    from family_assistant.utils.clock import MockClock

HISTORY_TOOLS = [
    registration
    for registration in LOCAL_TOOL_REGISTRATIONS
    if registration.name == "get_message_history"
]
SCOPE = HistoryScope(
    interface_type="telegram",
    conversation_id=CONVERSATION_ID,
    processing_profile_id=PROFILE_ID,
    subconversation_id=None,
)


async def _seed_tool_heavy_turns(db: Database, mock_clock: MockClock) -> None:
    for index in range(3):
        await seed_turn(
            db,
            mock_clock,
            f"Request {index}",
            f"Answer {index}",
            tool_calls=3,
            tool_result="r" * 2_000,
        )


@pytest.mark.asyncio
async def test_a_budget_event_reduces_older_tool_calls_to_stubs(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_tool_heavy_turns(db, mock_clock)
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=8_000, tools=HISTORY_TOOLS),
        db,
        "Next",
    )

    request = recorder.requests[0]
    text = texts(request)
    assert "Request 0" in text
    assert "Answer 0" in text
    assert "[Called lookup 3 times" in text
    assert "get_message_history retrieves" in text
    assert "r" * 2_000 not in text
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "budget"


@pytest.mark.asyncio
async def test_without_history_retrieval_tool_turns_are_dropped_whole(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_tool_heavy_turns(db, mock_clock)
    recorder = Recorder()
    await run_turn(make_service(mock_clock, recorder, budget_chars=8_000), db, "Next")

    text = texts(recorder.requests[0])
    assert "Request 0" not in text
    assert "[Called" not in text


@pytest.mark.asyncio
async def test_between_events_the_window_only_appends(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_tool_heavy_turns(db, mock_clock)
    recorder = Recorder()
    service = make_service(
        mock_clock, recorder, budget_chars=8_000, tools=HISTORY_TOOLS
    )
    await run_turn(service, db, "First after compaction")
    mock_clock.advance(timedelta(minutes=1))
    await run_turn(service, db, "Second after compaction")

    first, second = recorder.requests[0], recorder.requests[1]
    assert second[: len(first)] == first


@pytest.mark.asyncio
async def test_a_turn_that_ages_out_leaves_at_the_next_event(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Old request", "Old answer")
    recorder = Recorder()
    service = make_service(
        mock_clock, recorder, budget_chars=40_000, max_age_hours=2, min_turns=0
    )
    await run_turn(service, db, "Soon after")
    mock_clock.advance(timedelta(hours=3))
    await run_turn(service, db, "Much later")

    assert "Old request" in texts(recorder.requests[0])
    assert "Old request" not in texts(recorder.requests[1])
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "age"


@pytest.mark.asyncio
async def test_an_idle_gap_is_an_event(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Earlier request", "Earlier answer")
    mock_clock.advance(timedelta(hours=1))
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=40_000, idle_gap_minutes=30),
        db,
        "Back again",
    )

    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "idle"
    assert not event.changed


@pytest.mark.asyncio
async def test_thinking_after_a_changed_turn_is_stripped(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(
        db,
        mock_clock,
        "Big request",
        "Big answer",
        tool_calls=3,
        tool_result="r" * 3_000,
    )
    turn_id = str(uuid.uuid4())
    for message in (
        user_message("Small request"),
        AssistantMessage(
            content="Small answer",
            provider_metadata={
                "provider": "anthropic",
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "hm", "signature": "s"}
                ],
            },
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
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=4_000, tools=HISTORY_TOOLS),
        db,
        "Next",
    )

    answer = next(
        message
        for message in recorder.requests[0]
        if isinstance(message, AssistantMessage) and message.content == "Small answer"
    )
    assert answer.provider_metadata is None


class _RejectsOnceIfLong(RuleBasedMockLLMClient):
    """Rejects the first request whose history is over a size, as a provider would."""

    def __init__(self, recorder: Recorder, limit_chars: int) -> None:
        super().__init__(
            rules=[(recorder, LLMOutput(content="Noted.", tool_calls=None))]
        )
        self._limit_chars = limit_chars
        self.rejected = 0

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        if not self.rejected and len(texts(messages)) > self._limit_chars:
            self.rejected += 1
            raise ContextLengthError("too long", provider="mock", model="mock")
        return await super().generate_response(messages, tools, tool_choice)


@pytest.mark.asyncio
async def test_a_context_length_rejection_compacts_and_retries(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await _seed_tool_heavy_turns(db, mock_clock)
    recorder = Recorder()
    client = _RejectsOnceIfLong(recorder, limit_chars=10_000)
    await run_turn(
        make_service(
            mock_clock,
            recorder,
            budget_chars=100_000,
            tools=HISTORY_TOOLS,
            llm_client=client,
        ),
        db,
        "Next",
    )

    assert client.rejected == 1
    assert len(texts(recorder.requests[0])) <= 10_000
    assert not any(
        isinstance(message, ToolMessage) and message.content == "r" * 2_000
        for message in recorder.requests[0][:2]
    )
    event = await db.history_compaction.latest(SCOPE)
    assert event is not None
    assert event.reason == "context_length"
