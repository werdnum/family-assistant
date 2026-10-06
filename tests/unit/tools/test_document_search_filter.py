"""search_documents' Jev filter: what it shows, and what it says it left out."""

from collections.abc import Mapping
from typing import Any

import pytest

from family_assistant.config_models import JevFilterConfig
from family_assistant.llm.candidate_filter import JevCandidateFilter
from family_assistant.llm.typesafe import ChoiceQuestion, JevAnswers, NoulQuestion
from family_assistant.tools.documents import (
    filter_documents_with_jev,
)
from tests.mocks.fake_jev import FakeJevClient


def _filter(scores: Mapping[str, float], mode: str = "active") -> JevCandidateFilter:
    """A filter whose answers are keyed by each candidate's title."""

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
            nouls[label] = scores[str(candidate["title"])]
        return JevAnswers("jev-test", nouls, {}, 10)

    return JevCandidateFilter(
        FakeJevClient(respond=respond),
        surface="document_search",
        config=JevFilterConfig.model_validate({"mode": mode}),
    )


# ast-grep-ignore: no-dict-any - mirrors query_vector_store's result rows
def _results(*titles: str) -> list[dict[str, Any]]:
    return [
        {"title": title, "source_type": "note", "embedding_source_content": title}
        for title in titles
    ]


@pytest.mark.asyncio
async def test_active_filter_shows_relevant_results_most_probable_first() -> None:
    jev_filter = _filter({"Rates": 0.2, "Insurance": 0.7, "Policy": 0.95})

    shown, note = await filter_documents_with_jev(
        jev_filter, "home insurance", _results("Rates", "Insurance", "Policy"), 5
    )

    assert [result["title"] for result in shown] == ["Policy", "Insurance"]
    assert "1 weaker match(es) judged not relevant were left out" in note


@pytest.mark.asyncio
async def test_active_filter_says_when_relevant_results_exceed_the_limit() -> None:
    jev_filter = _filter({"A": 0.9, "B": 0.8, "C": 0.7})

    shown, note = await filter_documents_with_jev(
        jev_filter, "q", _results("A", "B", "C"), 2
    )

    assert [result["title"] for result in shown] == ["A", "B"]
    assert "1 more relevant match(es) beyond the limit" in note


@pytest.mark.asyncio
async def test_shadow_filter_shows_the_top_results_by_rank() -> None:
    jev_filter = _filter({"A": 0.1, "B": 0.9, "C": 0.9}, mode="shadow")

    shown, note = await filter_documents_with_jev(
        jev_filter, "q", _results("A", "B", "C"), 2
    )

    assert [result["title"] for result in shown] == ["A", "B"]
    assert not note
