"""Turn relevance mapping, thresholds and failure fallback."""

import pytest

from family_assistant.llm.typesafe import JevAnswers
from family_assistant.processing.history_relevance import (
    JevTurnRelevance,
    RelevanceMode,
    RelevanceOutcome,
)
from tests.mocks.fake_jev import FakeJevClient


async def test_probabilities_are_mapped_to_original_turn_keys() -> None:
    client = FakeJevClient(
        JevAnswers("jev-served", {"turn_0": 0.8, "turn_1": 0.2}, {}, 10)
    )
    relevance = JevTurnRelevance(
        client, mode="active", threshold=0.5, timeout_seconds=1
    )
    try:
        outcome = await relevance.assess(
            request="Continue",
            turns=[("older-key", "First", "Answer"), ("newer-key", "Second", "Answer")],
        )
    finally:
        await client.close()

    assert outcome.outcome == "decided"
    assert outcome.probabilities == {"older-key": 0.8, "newer-key": 0.2}
    assert outcome.model == "jev-served"


def test_relevant_keys_include_threshold_and_exclude_lower_probabilities() -> None:
    outcome = RelevanceOutcome(
        "decided",
        True,
        "jev",
        0,
        {"low": 0.49, "boundary": 0.5, "high": 0.9},
        threshold=0.5,
    )

    assert outcome.relevant_keys == frozenset({"boundary", "high"})


@pytest.mark.parametrize(("mode", "active"), [("active", True), ("shadow", False)])
async def test_active_follows_mode(mode: RelevanceMode, active: bool) -> None:
    client = FakeJevClient(JevAnswers("jev", {"turn_0": 0.9}, {}, 1))
    relevance = JevTurnRelevance(client, mode=mode, threshold=0.5, timeout_seconds=1)
    try:
        outcome = await relevance.assess(
            request="Continue", turns=[("key", "First", "Answer")]
        )
    finally:
        await client.close()

    assert relevance.active is active
    assert outcome.active is active


async def test_timeout_returns_no_active_preference() -> None:
    client = FakeJevClient(stall=True)
    relevance = JevTurnRelevance(
        client, mode="active", threshold=0.5, timeout_seconds=0.01
    )
    try:
        outcome = await relevance.assess(
            request="Continue", turns=[("key", "First", "Answer")]
        )
    finally:
        await client.close()

    assert outcome.outcome == "timeout"
    assert outcome.probabilities == {}
    assert not outcome.active


async def test_typesafe_error_returns_no_active_preference() -> None:
    client = FakeJevClient(fail=True)
    relevance = JevTurnRelevance(
        client, mode="active", threshold=0.5, timeout_seconds=1
    )
    try:
        outcome = await relevance.assess(
            request="Continue", turns=[("key", "First", "Answer")]
        )
    finally:
        await client.close()

    assert outcome.outcome == "error"
    assert outcome.probabilities == {}
    assert not outcome.active
