"""Unit tests for GoogleEmbeddingGenerator (protocol compliance, no API calls)."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from family_assistant.embeddings import EmbeddingGenerator, GoogleEmbeddingGenerator


def test_protocol_compliance() -> None:
    """GoogleEmbeddingGenerator satisfies the EmbeddingGenerator protocol."""
    generator = GoogleEmbeddingGenerator(model="gemini-embedding-001")
    assert isinstance(generator, EmbeddingGenerator)


def test_model_name_bare() -> None:
    generator = GoogleEmbeddingGenerator(model="gemini-embedding-001")
    assert generator.model_name == "gemini-embedding-001"


def test_model_name_with_prefix() -> None:
    """model_name preserves the gemini/ prefix for storage compatibility."""
    generator = GoogleEmbeddingGenerator(model="gemini/gemini-embedding-001")
    assert generator.model_name == "gemini/gemini-embedding-001"


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


@dataclass
class FakeEmbedding:
    values: list[float] | None


@dataclass
class FakeEmbedResponse:
    embeddings: list[FakeEmbedding] | None


@pytest.mark.no_db
async def test_missing_embeddings_raises() -> None:
    """Raises ValueError when API returns no embeddings for non-empty input."""
    generator = GoogleEmbeddingGenerator(
        model="gemini-embedding-001", api_key="fake-key"
    )
    mock_embed = AsyncMock(return_value=FakeEmbedResponse(embeddings=None))
    with (
        patch.object(generator._client.aio.models, "embed_content", mock_embed),
        pytest.raises(ValueError, match="returned no embeddings"),
    ):
        await generator.generate_embeddings(["hello"])


@pytest.mark.no_db
async def test_none_values_raises() -> None:
    """Raises ValueError when an embedding has None values."""
    generator = GoogleEmbeddingGenerator(
        model="gemini-embedding-001", api_key="fake-key"
    )
    response = FakeEmbedResponse(
        embeddings=[FakeEmbedding(values=[1.0, 2.0]), FakeEmbedding(values=None)]
    )
    mock_embed = AsyncMock(return_value=response)
    with (
        patch.object(generator._client.aio.models, "embed_content", mock_embed),
        pytest.raises(ValueError, match="no values"),
    ):
        await generator.generate_embeddings(["hello", "world"])


@pytest.mark.no_db
async def test_count_mismatch_raises() -> None:
    """Raises ValueError when embedding count doesn't match input count."""
    generator = GoogleEmbeddingGenerator(
        model="gemini-embedding-001", api_key="fake-key"
    )
    response = FakeEmbedResponse(embeddings=[FakeEmbedding(values=[1.0, 2.0])])
    mock_embed = AsyncMock(return_value=response)
    with (
        patch.object(generator._client.aio.models, "embed_content", mock_embed),
        pytest.raises(ValueError, match="returned 1 embeddings for 2"),
    ):
        await generator.generate_embeddings(["hello", "world"])
