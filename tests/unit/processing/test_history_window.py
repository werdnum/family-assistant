"""Which turns a budgeted history window keeps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from family_assistant.llm.messages import MessageWithMetadata, UserMessage
from family_assistant.processing.history_window import (
    HistoryLimits,
    HistoryTurn,
    select_turns,
)


def _turn(key: str) -> HistoryTurn:
    return HistoryTurn(
        key=key,
        rows=(
            MessageWithMetadata(
                message=UserMessage(content=key),
                internal_id="1",
                interface_message_id=None,
                timestamp=datetime(2026, 10, 5, tzinfo=UTC),
                conversation_id="c",
                interface_type="telegram",
            ),
        ),
    )


def _limits(budget: int, min_turns: int) -> HistoryLimits:
    return HistoryLimits(
        budget_chars=budget, min_turns=min_turns, max_age=timedelta(hours=1)
    )


def test_a_minimum_larger_than_the_history_keeps_every_turn() -> None:
    turns = [_turn("a"), _turn("b"), _turn("c")]

    kept = select_turns(
        turns, dict.fromkeys("abc", 100), limits=_limits(0, 4), mandatory=()
    )

    assert [turn.key for turn in kept] == ["a", "b", "c"]


def test_older_turns_fill_the_budget_contiguously() -> None:
    turns = [_turn("a"), _turn("b"), _turn("c"), _turn("d")]

    kept = select_turns(
        turns,
        {"a": 10, "b": 1_000, "c": 10, "d": 10},
        limits=_limits(100, 1),
        mandatory=(),
    )

    assert [turn.key for turn in kept] == ["c", "d"]


def test_mandatory_turns_stay_outside_the_budget() -> None:
    turns = [_turn("a"), _turn("b"), _turn("c")]

    kept = select_turns(
        turns,
        {"a": 1_000, "b": 1_000, "c": 10},
        limits=_limits(100, 1),
        mandatory={"a"},
    )

    assert [turn.key for turn in kept] == ["a", "c"]
