"""Tests for the calendar title similarity strategies."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator, Sequence

import numpy as np
import pytest

from family_assistant.similarity import (
    FuzzySimilarityStrategy,
    create_similarity_strategy_from_config,
    load_embedding_strategy,
)

_VECTORS = {
    "birthday": [1.0, 0.0],
    "Mia's birthday": [0.9, 0.1],
    "Labour Day": [0.0, 1.0],
    "": [0.0, 0.0],
}


class _CountingModel:
    """Stands in for a SentenceTransformer, mapping known titles to vectors."""

    instances: list[_CountingModel] = []

    def __init__(self, model_name: str, device: str) -> None:
        self.encode_calls = 0
        _CountingModel.instances.append(self)

    def encode(self, sentences: Sequence[str]) -> np.ndarray:
        self.encode_calls += 1
        return np.array([_VECTORS[s] for s in sentences])


@pytest.fixture
def fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _CountingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    _CountingModel.instances.clear()
    load_embedding_strategy.cache_clear()
    yield
    load_embedding_strategy.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_sentence_transformers")
async def test_embedding_scores_a_batch_with_one_encode() -> None:
    strategy = load_embedding_strategy("fake-model", "cpu")
    titles = ["Mia's birthday", "Labour Day", ""] * 50

    scores = await strategy.compute_similarities("birthday", titles)

    assert _CountingModel.instances[0].encode_calls == 1
    assert scores[:3] == pytest.approx([0.9 / np.hypot(0.9, 0.1), 0.0, 0.0])
    assert len(scores) == len(titles)


@pytest.mark.usefixtures("fake_sentence_transformers")
def test_embedding_model_is_loaded_once_per_config() -> None:
    config = {
        "duplicate_detection": {
            "similarity_strategy": "embedding",
            "embedding": {"model": "fake-model", "device": "cpu"},
        }
    }

    first = create_similarity_strategy_from_config(config)  # type: ignore[arg-type]
    second = create_similarity_strategy_from_config(config)  # type: ignore[arg-type]

    assert first is second
    assert len(_CountingModel.instances) == 1


@pytest.mark.asyncio
async def test_fuzzy_scores_each_title_against_the_query() -> None:
    scores = await FuzzySimilarityStrategy().compute_similarities(
        "Dentist", ["dentist", "Swimming"]
    )

    assert scores[0] == 1.0
    assert scores[1] < 0.5
