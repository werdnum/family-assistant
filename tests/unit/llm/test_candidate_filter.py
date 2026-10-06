"""Jev as a relevance filter: batching, the threshold cut, and failing open."""

from collections.abc import Mapping

import pytest

from family_assistant.config_models import JevFilterConfig
from family_assistant.llm.candidate_filter import CandidateQuestion, JevCandidateFilter
from family_assistant.llm.typesafe import ChoiceQuestion, JevAnswers, NoulQuestion
from tests.mocks.fake_jev import FakeJevClient

QUESTION = CandidateQuestion(
    instructions="Does `{candidate}` match `search`?",
    true_criteria="It matches.",
    false_criteria="It does not.",
)


def _scores_by_summary(
    scores: Mapping[str, float],
) -> FakeJevClient:
    """A client answering each candidate with the score its summary is given."""

    def respond(
        state: str | Mapping[str, object],
        questions: Mapping[str, NoulQuestion | ChoiceQuestion],
    ) -> JevAnswers:
        assert isinstance(state, Mapping)
        candidates = state["candidates"]
        assert isinstance(candidates, Mapping)
        nouls = {}
        for label in questions:
            candidate = candidates[label]
            assert isinstance(candidate, Mapping)
            nouls[label] = scores[str(candidate["summary"])]
        return JevAnswers("jev-test", nouls, {}, 10)

    return FakeJevClient(respond=respond)


def _filter(client: FakeJevClient, **config: object) -> JevCandidateFilter:
    return JevCandidateFilter(
        client,
        surface="calendar_search",
        config=JevFilterConfig.model_validate({"mode": "active", **config}),
    )


@pytest.mark.asyncio
async def test_keeps_candidates_over_threshold_most_probable_first() -> None:
    client = _scores_by_summary({"Dentist": 0.7, "Swim": 0.1, "Dr Lee": 0.95})
    outcome = await _filter(client).assess(
        query={"search": "teeth"},
        candidates={
            "a": {"summary": "Dentist"},
            "b": {"summary": "Swim"},
            "c": {"summary": "Dr Lee"},
        },
        question=QUESTION,
        baseline=["a"],
    )

    assert outcome.applies
    assert outcome.kept() == ["c", "a"]
    state, questions = client.calls[0]
    assert isinstance(state, Mapping)
    assert state["search"] == "teeth"
    question = questions["c00"]
    assert isinstance(question, NoulQuestion)
    assert question.instructions == "Does `candidates.c00` match `search`?"


@pytest.mark.asyncio
async def test_large_candidate_sets_go_in_concurrent_batches() -> None:
    scores = {f"event {index}": index / 100 for index in range(65)}
    client = _scores_by_summary(scores)
    outcome = await _filter(client, threshold=0.6).assess(
        query={"search": "x"},
        candidates={str(index): {"summary": f"event {index}"} for index in range(65)},
        question=QUESTION,
        baseline=[],
    )

    assert [len(questions) for _, questions in client.calls] == [30, 30, 5]
    assert len(outcome.probabilities) == 65
    assert outcome.kept() == [str(index) for index in range(64, 59, -1)]


@pytest.mark.asyncio
async def test_shadow_mode_records_but_does_not_apply() -> None:
    client = _scores_by_summary({"Dentist": 0.9})
    jev_filter = JevCandidateFilter(
        client, surface="calendar_search", config=JevFilterConfig(mode="shadow")
    )
    outcome = await jev_filter.assess(
        query={"search": "teeth"},
        candidates={"a": {"summary": "Dentist"}},
        question=QUESTION,
        baseline=[],
    )

    assert outcome.outcome == "decided"
    assert not outcome.applies
    assert outcome.kept() == ["a"]


@pytest.mark.asyncio
async def test_failure_falls_back_to_the_surface() -> None:
    outcome = await _filter(FakeJevClient(fail=True)).assess(
        query={"search": "teeth"},
        candidates={"a": {"summary": "Dentist"}},
        question=QUESTION,
        baseline=["a"],
    )

    assert outcome.outcome == "error"
    assert not outcome.applies


@pytest.mark.asyncio
async def test_timeout_falls_back_to_the_surface() -> None:
    outcome = await _filter(FakeJevClient(stall=True), timeout_seconds=0.01).assess(
        query={"search": "teeth"},
        candidates={"a": {"summary": "Dentist"}},
        question=QUESTION,
        baseline=["a"],
    )

    assert outcome.outcome == "timeout"
    assert not outcome.applies
