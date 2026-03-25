"""Unit tests for GoogleEmbeddingGenerator (protocol compliance, no API calls)."""

import pytest

from family_assistant.embeddings import EmbeddingGenerator, GoogleEmbeddingGenerator


def test_protocol_compliance() -> None:
    """GoogleEmbeddingGenerator satisfies the EmbeddingGenerator protocol."""
    generator = GoogleEmbeddingGenerator(model="gemini-embedding-001")
    assert isinstance(generator, EmbeddingGenerator)


def test_model_name() -> None:
    generator = GoogleEmbeddingGenerator(model="gemini-embedding-001")
    assert generator.model_name == "gemini-embedding-001"


def test_empty_model_raises() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        GoogleEmbeddingGenerator(model="")


@pytest.mark.no_db
async def test_empty_input_returns_empty() -> None:
    """Empty input returns empty result without making an API call."""
    generator = GoogleEmbeddingGenerator(model="gemini-embedding-001")
    result = await generator.generate_embeddings([])
    assert result.embeddings == []
    assert result.model_name == "gemini-embedding-001"
