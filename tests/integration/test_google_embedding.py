"""Integration tests for GoogleEmbeddingGenerator using SDK record/replay.

Uses Google GenAI SDK's built-in DebugConfig for deterministic record/replay,
following the same pattern as the LLM streaming integration tests.

## Running

```bash
# Replay existing recordings (default, safe for CI)
LLM_RECORD_MODE=replay pytest tests/integration/test_google_embedding.py -xq -m llm_integration

# Record new interactions (requires GEMINI_API_KEY)
LLM_RECORD_MODE=record GEMINI_API_KEY=xxx pytest tests/integration/test_google_embedding.py -xq -m llm_integration

# Auto-record missing, replay existing
LLM_RECORD_MODE=auto GEMINI_API_KEY=xxx pytest tests/integration/test_google_embedding.py -xq -m llm_integration
```
"""

import os
from pathlib import Path

import pytest
from google.genai.client import DebugConfig

from family_assistant.embeddings import GoogleEmbeddingGenerator

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 256
CASSETTE_DIR = "tests/cassettes/gemini"


def _replay_file_exists(test_name: str) -> bool:
    replay_path = (
        Path(CASSETTE_DIR) / f"integration.test_google_embedding/{test_name}/mldev.json"
    )
    return replay_path.exists()


def _make_generator(llm_record_mode: str, test_name: str) -> GoogleEmbeddingGenerator:
    replay_exists = _replay_file_exists(test_name)
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if llm_record_mode == "replay" and not replay_exists:
        pytest.fail(
            f"Replay file missing for {test_name}. Record with LLM_RECORD_MODE=record."
        )

    if llm_record_mode == "record" and not api_key:
        pytest.skip("Recording requires GEMINI_API_KEY or GOOGLE_API_KEY.")

    if llm_record_mode == "auto" and not replay_exists and not api_key:
        pytest.skip(
            "Auto-recording missing replays requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    debug_config = DebugConfig(
        client_mode=llm_record_mode,
        replay_id=f"integration.test_google_embedding/{test_name}/mldev",
        replays_directory=CASSETTE_DIR,
    )
    return GoogleEmbeddingGenerator(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        api_key=api_key or "test-key",
        debug_config=debug_config,
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_single_text_embedding(llm_record_mode: str) -> None:
    """Test embedding a single text returns a vector of the correct dimension."""
    generator = _make_generator(llm_record_mode, "test_single_text_embedding")

    result = await generator.generate_embeddings(["Hello, world!"])

    assert len(result.embeddings) == 1
    assert len(result.embeddings[0]) == EMBEDDING_DIMENSIONS
    assert result.model_name == EMBEDDING_MODEL
    assert all(isinstance(v, float) for v in result.embeddings[0])


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_batch_embedding(llm_record_mode: str) -> None:
    """Test embedding multiple texts returns the correct number of vectors."""
    generator = _make_generator(llm_record_mode, "test_batch_embedding")

    texts = [
        "The quick brown fox jumps over the lazy dog.",
        "Machine learning is a subset of artificial intelligence.",
        "Family calendars help organize busy schedules.",
    ]
    result = await generator.generate_embeddings(texts)

    assert len(result.embeddings) == 3
    for embedding in result.embeddings:
        assert len(embedding) == EMBEDDING_DIMENSIONS
        assert all(isinstance(v, float) for v in embedding)


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_empty_input() -> None:
    """Test that empty input returns empty result without API call."""
    generator = GoogleEmbeddingGenerator(
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        api_key="dummy-key-not-used-for-empty-input",
    )

    result = await generator.generate_embeddings([])

    assert result.embeddings == []
    assert result.model_name == EMBEDDING_MODEL


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_similar_texts_have_closer_embeddings(llm_record_mode: str) -> None:
    """Test that semantically similar texts produce closer embeddings than dissimilar ones."""
    generator = _make_generator(
        llm_record_mode, "test_similar_texts_have_closer_embeddings"
    )

    texts = [
        "I love programming in Python",
        "Python is my favorite programming language",
        "The weather forecast says it will rain tomorrow",
    ]
    result = await generator.generate_embeddings(texts)

    def cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        return dot / (norm_a * norm_b)

    sim_similar = cosine_similarity(result.embeddings[0], result.embeddings[1])
    sim_different = cosine_similarity(result.embeddings[0], result.embeddings[2])

    assert sim_similar > sim_different, (
        f"Similar texts should have higher cosine similarity ({sim_similar:.4f}) "
        f"than dissimilar texts ({sim_different:.4f})"
    )
