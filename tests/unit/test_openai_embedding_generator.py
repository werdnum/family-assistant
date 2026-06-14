"""Unit tests for OpenAIEmbeddingGenerator (protocol compliance, no API calls)."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from family_assistant.config_models import AppConfig
from family_assistant.embeddings import EmbeddingGenerator, OpenAIEmbeddingGenerator


@dataclass
class FakeEmbeddingDatum:
    embedding: list[float]
    index: int


@dataclass
class FakeEmbeddingsResponse:
    data: list[FakeEmbeddingDatum]


def test_protocol_compliance() -> None:
    """OpenAIEmbeddingGenerator satisfies the EmbeddingGenerator protocol."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key"
    )
    assert isinstance(generator, EmbeddingGenerator)


def test_model_name_used_verbatim() -> None:
    """model_name is preserved verbatim, including any vendor prefix (OpenRouter)."""
    generator = OpenAIEmbeddingGenerator(
        model="openai/text-embedding-3-small", api_key="fake-key"
    )
    assert generator.model_name == "openai/text-embedding-3-small"


def test_empty_model_raises() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        OpenAIEmbeddingGenerator(model="")


@pytest.mark.no_db
async def test_empty_input_returns_empty() -> None:
    """Empty input returns empty result without making an API call."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key"
    )
    result = await generator.generate_embeddings([])
    assert result.embeddings == []
    assert result.model_name == "text-embedding-3-small"


@pytest.mark.no_db
async def test_model_sent_verbatim_to_api() -> None:
    """The model identifier is sent to the API exactly as configured."""
    generator = OpenAIEmbeddingGenerator(
        model="openai/text-embedding-3-small", api_key="fake-key"
    )
    response = FakeEmbeddingsResponse(
        data=[FakeEmbeddingDatum(embedding=[1.0, 2.0, 3.0], index=0)]
    )
    mock_create = AsyncMock(return_value=response)
    with patch.object(generator._client.embeddings, "create", mock_create):
        await generator.generate_embeddings(["hello"])

    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["model"] == "openai/text-embedding-3-small"
    assert mock_create.call_args.kwargs["input"] == ["hello"]


@pytest.mark.no_db
async def test_dimensions_passed_when_set() -> None:
    """The dimensions parameter is forwarded when configured."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key", dimensions=256
    )
    response = FakeEmbeddingsResponse(
        data=[FakeEmbeddingDatum(embedding=[0.1] * 256, index=0)]
    )
    mock_create = AsyncMock(return_value=response)
    with patch.object(generator._client.embeddings, "create", mock_create):
        await generator.generate_embeddings(["hello"])

    assert mock_create.call_args.kwargs["dimensions"] == 256


@pytest.mark.no_db
async def test_dimensions_omitted_when_none() -> None:
    """No dimensions parameter is sent when dimensions is None."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-ada-002", api_key="fake-key"
    )
    response = FakeEmbeddingsResponse(
        data=[FakeEmbeddingDatum(embedding=[0.1, 0.2], index=0)]
    )
    mock_create = AsyncMock(return_value=response)
    with patch.object(generator._client.embeddings, "create", mock_create):
        await generator.generate_embeddings(["hello"])

    assert "dimensions" not in mock_create.call_args.kwargs


@pytest.mark.no_db
async def test_embeddings_reordered_by_index() -> None:
    """Out-of-order API responses are realigned with input order via index."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key"
    )
    response = FakeEmbeddingsResponse(
        data=[
            FakeEmbeddingDatum(embedding=[2.0], index=1),
            FakeEmbeddingDatum(embedding=[0.0], index=0),
        ]
    )
    mock_create = AsyncMock(return_value=response)
    with patch.object(generator._client.embeddings, "create", mock_create):
        result = await generator.generate_embeddings(["first", "second"])

    assert result.embeddings == [[0.0], [2.0]]


@pytest.mark.no_db
async def test_missing_embeddings_raises() -> None:
    """Raises ValueError when API returns no embeddings for non-empty input."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key"
    )
    mock_create = AsyncMock(return_value=FakeEmbeddingsResponse(data=[]))
    with (
        patch.object(generator._client.embeddings, "create", mock_create),
        pytest.raises(ValueError, match="returned no embeddings"),
    ):
        await generator.generate_embeddings(["hello"])


def test_embedding_dimensions_explicit_flag() -> None:
    """The openai provider relies on model_fields_set to know if dimensions was set.

    The assistant only forwards `dimensions` to the API when the operator
    explicitly configured embedding_dimensions, so unset configs (which keep the
    1536 default for vector storage) must not appear in model_fields_set.
    """
    default_cfg = AppConfig.model_validate({"embedding_provider": "openai"})
    assert "embedding_dimensions" not in default_cfg.model_fields_set
    assert default_cfg.embedding_dimensions == 1536

    explicit_cfg = AppConfig.model_validate({
        "embedding_provider": "openai",
        "embedding_dimensions": 512,
    })
    assert "embedding_dimensions" in explicit_cfg.model_fields_set
    assert explicit_cfg.embedding_dimensions == 512


@pytest.mark.no_db
async def test_count_mismatch_raises() -> None:
    """Raises ValueError when embedding count doesn't match input count."""
    generator = OpenAIEmbeddingGenerator(
        model="text-embedding-3-small", api_key="fake-key"
    )
    response = FakeEmbeddingsResponse(
        data=[FakeEmbeddingDatum(embedding=[1.0, 2.0], index=0)]
    )
    mock_create = AsyncMock(return_value=response)
    with (
        patch.object(generator._client.embeddings, "create", mock_create),
        pytest.raises(ValueError, match="returned 1 embeddings for 2"),
    ):
        await generator.generate_embeddings(["hello", "world"])
