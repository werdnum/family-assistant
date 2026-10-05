"""The prompt's history window is built from whole turns against a size budget.

See docs/design/history-compaction.md.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm.messages import (
    AssistantMessage,
    ErrorMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.storage.database import Database
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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.utils.clock import MockClock


@pytest.mark.asyncio
async def test_a_tool_heavy_turn_does_not_push_out_the_request_before_it(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Get me two plumber quotes", "Here are two.")
    await seed_turn(db, mock_clock, "Check the calendar", "You are free.", tool_calls=4)
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=40_000),
        db,
        "And the other one?",
    )

    text = texts(recorder.requests[0])
    assert "Get me two plumber quotes" in text
    assert "Here are two." in text
    assert "Check the calendar" in text


@pytest.mark.asyncio
async def test_the_window_never_splits_a_turn(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(
        db, mock_clock, "Old request", "Old answer", tool_calls=3, tool_result="x" * 400
    )
    await seed_turn(db, mock_clock, "Newer request", "Newer answer")
    recorder = Recorder()
    # Room for the newer turn and some, but not all, of the older one's rows.
    await run_turn(make_service(mock_clock, recorder, budget_chars=700), db, "Now this")

    request = recorder.requests[0]
    assert "Old" not in texts(request)
    assert not any(isinstance(message, ToolMessage) for message in request)
    history = request[1:]
    assert isinstance(history[0], UserMessage)
    assert "Newer request" in texts(history)


@pytest.mark.asyncio
async def test_min_turns_outrank_the_budget(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Big request", "Big answer " + "y" * 5_000)
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=100, min_turns=1), db, "Next"
    )

    assert "Big request" in texts(recorder.requests[0])


@pytest.mark.asyncio
async def test_a_turn_older_than_the_age_cap_is_left_out(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Yesterday's request", "Yesterday's answer")
    mock_clock.advance(timedelta(hours=3))
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=40_000, max_age_hours=2),
        db,
        "Today",
    )

    assert "Yesterday" not in texts(recorder.requests[0])


@pytest.mark.asyncio
async def test_consecutive_turns_render_as_prefix_plus_append(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Earlier", "Earlier answer", tool_calls=2)
    recorder = Recorder()
    service = make_service(mock_clock, recorder, budget_chars=40_000)
    await run_turn(service, db, "First question")
    mock_clock.advance(timedelta(minutes=1))
    await run_turn(service, db, "Second question")

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
        user_message("Do the thing"),
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
    recorder = Recorder()
    await run_turn(make_service(mock_clock, recorder, budget_chars=40_000), db, "Again")

    text = texts(recorder.requests[0])
    assert "I encountered an error: Tool failed" in text
    assert "with detail" not in text
    assert "Traceback" not in text


@pytest.mark.asyncio
async def test_a_thread_reply_loads_the_thread_on_this_profile_only(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    root = await seed_turn(
        db,
        mock_clock,
        "Thread opener",
        "Opener answer",
        interface_message_id="tg-1",
    )
    await seed_turn(
        db,
        mock_clock,
        "Other profile in thread",
        "Other profile answer",
        profile_id="someone-else",
        thread_root_id=root,
    )
    # Long enough ago that the age cap alone would leave the thread out.
    mock_clock.advance(timedelta(hours=5))
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=40_000, max_age_hours=2),
        db,
        "Following up",
        replied_to_interface_id="tg-1",
    )

    text = texts(recorder.requests[0])
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
        user_message("Tell my partner dinner is at seven"),
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
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=40_000),
        db,
        "Thanks",
        replied_to_interface_id="tg-notify",
    )

    text = texts(recorder.requests[0])
    assert notification
    assert "Dinner is at seven." in text
    assert "Tell my partner" not in text


@pytest.mark.asyncio
async def test_thread_turns_count_toward_the_minimum(
    db_engine: AsyncEngine, mock_clock: MockClock
) -> None:
    db = Database(db_engine)
    await seed_turn(db, mock_clock, "Unrelated big request", "z" * 5_000)
    root = await seed_turn(
        db, mock_clock, "Thread opener", "Opener answer", interface_message_id="tg-2"
    )
    await seed_turn(
        db, mock_clock, "Thread follow-up", "Follow-up answer", thread_root_id=root
    )
    recorder = Recorder()
    await run_turn(
        make_service(mock_clock, recorder, budget_chars=500, min_turns=2),
        db,
        "Replying",
        replied_to_interface_id="tg-2",
    )

    text = texts(recorder.requests[0])
    assert "Thread follow-up" in text
    assert "Unrelated big request" not in text
