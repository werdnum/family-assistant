"""The sweep that writes conversation-list summaries.

See docs/design/conversation-list-summaries.md: a conversation is summarized
once it has gone quiet, again after it moves on, and the list carries the
summary beside the latest-message preview.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from family_assistant.config_models import ConversationSummaryConfig
from family_assistant.conversation_summaries import (
    MAX_SUMMARY_CHARS,
    ConversationSummarizer,
    clean_summary,
    run_conversation_summary_sweep,
)
from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import AssistantMessage, UserMessage
from family_assistant.services.turn_resumption import (
    TurnLeaseRegistry,
    TurnResumePayload,
)
from family_assistant.storage.database import Database
from tests.mocks.mock_llm import RuleBasedMockLLMClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from tests.mocks.mock_llm import MatcherArgs

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
OWNER = "alice"
CONFIG = ConversationSummaryConfig(
    enabled=True, idle_seconds=60, lookback_days=30, batch_size=10
)


async def _add_exchange(
    db: Database,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    *,
    at: datetime,
) -> None:
    turn_id = f"{conversation_id}-{at.isoformat()}"
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content=user_text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=at,
        turn_id=turn_id,
        processing_profile_id="default_assistant",
        user_id=OWNER,
    )
    await db.message_history.add_message(
        AssistantMessage(content=assistant_text),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=at,
        turn_id=turn_id,
        processing_profile_id="default_assistant",
        user_id=None,
    )


def _transcript_contains(text: str) -> Callable[[MatcherArgs], bool]:
    def matcher(args: MatcherArgs) -> bool:
        return any(text in str(message.content) for message in args["messages"])

    return matcher


def _summarizer(llm: RuleBasedMockLLMClient) -> ConversationSummarizer:
    return ConversationSummarizer(llm, prompt="Label it.", timeout_seconds=5)


async def _listed_summaries(db: Database) -> dict[str, str | None]:
    rows, _ = await db.message_history.get_conversation_summaries(
        include_subconversations=False
    )
    return {row["conversation_id"]: row["summary"] for row in rows}


@pytest.mark.asyncio
async def test_a_quiet_conversation_is_listed_with_its_summary(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db,
        "conv-party",
        "Help me plan Sam's birthday party",
        "Happy to. Where would you like to hold it?",
        at=NOW - timedelta(minutes=5),
    )
    llm = RuleBasedMockLLMClient(
        rules=[
            (
                _transcript_contains("Sam's birthday party"),
                LLMOutput(content="Sam's birthday party planning"),
            )
        ]
    )

    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    assert await _listed_summaries(db) == {
        "conv-party": "Sam's birthday party planning"
    }


@pytest.mark.asyncio
async def test_a_conversation_still_in_progress_is_not_summarized(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db, "conv-live", "What's for dinner?", "Pasta?", at=NOW - timedelta(seconds=10)
    )
    llm = RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content="x"))

    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    assert llm.get_calls() == []


@pytest.mark.asyncio
async def test_a_conversation_whose_turn_is_still_running_is_not_summarized(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await db.message_history.add_message(
        UserMessage.from_trusted_user(content="Research flights to Tokyo in May"),
        interface_type="web",
        conversation_id="conv-running",
        timestamp=NOW - timedelta(minutes=5),
        turn_id="turn-running",
        processing_profile_id="default_assistant",
        user_id=OWNER,
    )
    await TurnLeaseRegistry().arm(
        db.tasks,
        TurnResumePayload(
            resumer="web_stream",
            interface_type="web",
            conversation_id="conv-running",
            turn_id="turn-running",
            user_id=OWNER,
            user_name="Alice",
            processing_profile_id="default_assistant",
        ),
    )
    llm = RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content="x"))

    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    assert llm.get_calls() == []


@pytest.mark.asyncio
async def test_a_summarized_conversation_is_not_summarized_again_until_it_moves_on(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db, "conv-a", "Book the dentist", "Booked.", at=NOW - timedelta(minutes=5)
    )
    llm = RuleBasedMockLLMClient(
        rules=[], default_response=LLMOutput(content="Dentist booking")
    )
    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    await run_conversation_summary_sweep(
        db,
        summarizer=_summarizer(llm),
        config=CONFIG,
        now_fn=lambda: NOW + timedelta(minutes=2),
    )

    assert len(llm.get_calls()) == 1


@pytest.mark.asyncio
async def test_new_messages_replace_the_summary(db_engine: AsyncEngine) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db, "conv-a", "Book the dentist", "Booked.", at=NOW - timedelta(minutes=10)
    )
    await run_conversation_summary_sweep(
        db,
        summarizer=_summarizer(
            RuleBasedMockLLMClient(
                rules=[], default_response=LLMOutput(content="Dentist booking")
            )
        ),
        config=CONFIG,
        now_fn=lambda: NOW - timedelta(minutes=8),
    )
    await _add_exchange(
        db,
        "conv-a",
        "Actually move it to Thursday",
        "Moved to Thursday 3pm.",
        at=NOW - timedelta(minutes=5),
    )
    llm = RuleBasedMockLLMClient(
        rules=[
            (
                _transcript_contains("move it to Thursday"),
                LLMOutput(content="Dentist moved to Thursday 3pm"),
            )
        ]
    )

    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    assert await _listed_summaries(db) == {"conv-a": "Dentist moved to Thursday 3pm"}


@pytest.mark.asyncio
async def test_a_failed_attempt_keeps_the_previous_summary_and_is_not_retried(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db, "conv-a", "Book the dentist", "Booked.", at=NOW - timedelta(minutes=10)
    )
    await run_conversation_summary_sweep(
        db,
        summarizer=_summarizer(
            RuleBasedMockLLMClient(
                rules=[], default_response=LLMOutput(content="Dentist booking")
            )
        ),
        config=CONFIG,
        now_fn=lambda: NOW - timedelta(minutes=8),
    )
    await _add_exchange(
        db, "conv-a", "Thanks", "You're welcome.", at=NOW - timedelta(minutes=5)
    )
    failing = RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content=""))
    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(failing), config=CONFIG, now_fn=lambda: NOW
    )

    await run_conversation_summary_sweep(
        db,
        summarizer=_summarizer(failing),
        config=CONFIG,
        now_fn=lambda: NOW + timedelta(minutes=2),
    )

    assert len(failing.get_calls()) == 1
    assert await _listed_summaries(db) == {"conv-a": "Dentist booking"}


@pytest.mark.asyncio
async def test_conversations_outside_the_lookback_are_left_unsummarized(
    db_engine: AsyncEngine,
) -> None:
    db = Database(engine=db_engine)
    await _add_exchange(
        db, "conv-old", "Old question", "Old answer", at=NOW - timedelta(days=45)
    )
    llm = RuleBasedMockLLMClient(rules=[], default_response=LLMOutput(content="x"))

    await run_conversation_summary_sweep(
        db, summarizer=_summarizer(llm), config=CONFIG, now_fn=lambda: NOW
    )

    assert await _listed_summaries(db) == {"conv-old": None}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Dentist moved to Thursday"', "Dentist moved to Thursday"),
        ("Summary: Paella for 8\n\nExtra commentary", "Paella for 8"),
        ("   ", None),
        (None, None),
    ],
)
def test_clean_summary_reduces_the_answer_to_one_display_line(
    raw: str | None, expected: str | None
) -> None:
    assert clean_summary(raw) == expected


def test_clean_summary_truncates_an_overlong_answer_on_a_word() -> None:
    cleaned = clean_summary("word " * 60)

    assert cleaned is not None
    assert len(cleaned) <= MAX_SUMMARY_CHARS
    assert cleaned.endswith("word…")
