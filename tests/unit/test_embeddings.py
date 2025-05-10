import math

import pytest
import pytest_asyncio

from family_assistant.embeddings import HashingWordEmbeddingGenerator


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Computes the cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(vec1, vec2, strict=True))
    magnitude1 = math.sqrt(sum(x * x for x in vec1))
    magnitude2 = math.sqrt(sum(x * x for x in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        if magnitude1 == magnitude2:  # Both are zero vectors
            return 1.0  # Or 0.0 depending on definition, 1.0 if they are identical
        return 0.0  # One is zero, other is not
    return dot_product / (magnitude1 * magnitude2)


def cosine_distance(vec1: list[float], vec2: list[float]) -> float:
    """Computes the cosine distance (1 - similarity)."""
    return 1.0 - cosine_similarity(vec1, vec2)


@pytest_asyncio.fixture
async def default_generator() -> HashingWordEmbeddingGenerator:
    return HashingWordEmbeddingGenerator(dimensionality=128)


class TestHashingWordEmbeddingGenerator:
    @pytest.mark.asyncio
    async def test_dimensionality(self) -> None:
        """Test that the embedding vector has the correct dimensionality."""
        dim = 64
        generator = HashingWordEmbeddingGenerator(dimensionality=dim)
        text = "test dimensionality"
        result = await generator.generate_embeddings([text])
        assert len(result.embeddings) == 1
        assert len(result.embeddings[0]) == dim
        assert result.model_name == "hashing-word-v1"  # Default model name

    @pytest.mark.asyncio
    async def test_model_name_override(self) -> None:
        """Test that the model name can be overridden."""
        custom_model_name = "custom-hash-model"
        generator = HashingWordEmbeddingGenerator(
            model_name=custom_model_name, dimensionality=32
        )
        assert generator.model_name == custom_model_name
        result = await generator.generate_embeddings(["test"])
        assert result.model_name == custom_model_name

    @pytest.mark.parametrize(
        "text_input",
        [
            "hello world",
            "The quick brown fox jumps over the lazy dog.",
            "12345 numbers and words",
        ],
    )
    @pytest.mark.asyncio
    async def test_identity_similarity(
        self, default_generator: HashingWordEmbeddingGenerator, text_input: str
    ) -> None:
        """A text should have zero cosine distance to itself."""
        result1 = await default_generator.generate_embeddings([text_input])
        result2 = await default_generator.generate_embeddings([text_input])

        dist = cosine_distance(result1.embeddings[0], result2.embeddings[0])
        assert math.isclose(
            dist, 0.0, abs_tol=1e-9
        ), f"Text should be identical to itself, distance was {dist}"

    @pytest.mark.parametrize(
        "text_input",
        [
            "A sample sentence for normalization.",
            "another one with different words",
            "short",
        ],
    )
    @pytest.mark.asyncio
    async def test_vector_normalization(
        self, default_generator: HashingWordEmbeddingGenerator, text_input: str
    ) -> None:
        """Generated vectors should be unit length."""
        result = await default_generator.generate_embeddings([text_input])
        vector = result.embeddings[0]
        magnitude = math.sqrt(sum(x * x for x in vector))
        assert math.isclose(
            magnitude, 1.0, abs_tol=1e-9
        ), f"Vector magnitude should be 1.0, was {magnitude}"

    @pytest.mark.parametrize(
        "text1, text2",
        [
            ("Hello World", "hello world"),
            ("Special Characters!", "special characters"),
            ("  leading and trailing spaces  ", "leading and trailing spaces"),
            ("UPPERCASE TEXT", "uppercase text"),
            (
                "MixEd CaSe AnD Punctuation.",
                "mixed case and punctuation",
            ),  # Corrected @ to a
        ],
    )
    @pytest.mark.asyncio
    async def test_case_and_special_char_insensitivity(
        self, default_generator: HashingWordEmbeddingGenerator, text1: str, text2: str
    ) -> None:
        """Embeddings should be insensitive to case and common special characters."""
        result1 = await default_generator.generate_embeddings([text1])
        result2 = await default_generator.generate_embeddings([text2])

        dist = cosine_distance(result1.embeddings[0], result2.embeddings[0])
        assert math.isclose(
            dist, 0.0, abs_tol=1e-9
        ), f"Texts '{text1}' and '{text2}' should have zero distance, was {dist}"

    @pytest.mark.parametrize(
        "empty_text",
        ["", "   ", "\t\n", "!!!", "...,,,###"],  # also test with only special chars
    )
    @pytest.mark.asyncio
    async def test_empty_or_whitespace_input(
        self, default_generator: HashingWordEmbeddingGenerator, empty_text: str
    ) -> None:
        """Empty or whitespace-only input should result in a zero vector."""
        dim = default_generator.dimensionality
        result = await default_generator.generate_embeddings([empty_text])
        vector = result.embeddings[0]
        expected_zero_vector = [0.0] * dim
        assert (
            vector == expected_zero_vector
        ), f"Vector for empty/whitespace text should be all zeros, was {vector}"
        magnitude = math.sqrt(sum(x * x for x in vector))
        assert math.isclose(
            magnitude, 0.0, abs_tol=1e-9
        ), f"Magnitude of zero vector should be 0.0, was {magnitude}"

    @pytest.mark.asyncio
    async def test_quote_similarity(
        self, default_generator: HashingWordEmbeddingGenerator
    ) -> None:
        """A quote should be closer to its source text than to an unrelated text."""
        source_text = "The quick brown fox jumps over the lazy dog near the river bank."
        quote = "quick brown fox"
        unrelated_text = "Computational linguistics and natural language processing."

        source_embedding = (
            await default_generator.generate_embeddings([source_text])
        ).embeddings[0]
        quote_embedding = (
            await default_generator.generate_embeddings([quote])
        ).embeddings[0]
        unrelated_embedding = (
            await default_generator.generate_embeddings([unrelated_text])
        ).embeddings[0]

        dist_source_quote = cosine_distance(source_embedding, quote_embedding)
        dist_source_unrelated = cosine_distance(source_embedding, unrelated_embedding)
        dist_quote_unrelated = cosine_distance(quote_embedding, unrelated_embedding)

        assert (
            dist_source_quote < dist_source_unrelated
        ), f"Quote should be closer to source ({dist_source_quote}) than to unrelated ({dist_source_unrelated})"
        assert (
            dist_source_quote < dist_quote_unrelated
        ), f"Quote should be closer to source ({dist_source_quote}) than quote to unrelated ({dist_quote_unrelated})"
        # Also, source and quote should not be identical if quote is shorter
        if len(quote) < len(source_text):
            assert not math.isclose(
                dist_source_quote, 0.0, abs_tol=1e-9
            ), "Source and its shorter quote should not be identical"

    @pytest.mark.asyncio
    async def test_different_texts_different_embeddings(
        self, default_generator: HashingWordEmbeddingGenerator
    ) -> None:
        """Completely different texts should have different embeddings (distance > 0)."""
        text1 = "This is the first sentence."
        text2 = "This is a completely different second sentence."

        # Ensure they don't become identical after normalization (e.g. "a" vs "A")
        # The case insensitivity test covers this, but good to have a direct check for difference.
        # This test is more about ensuring the hashing produces different results for different content.

        result1 = await default_generator.generate_embeddings([text1])
        result2 = await default_generator.generate_embeddings([text2])

        dist = cosine_distance(result1.embeddings[0], result2.embeddings[0])
        assert dist > 1e-9, f"Different texts should have non-zero distance, was {dist}"

    @pytest.mark.asyncio
    async def test_batch_processing(
        self, default_generator: HashingWordEmbeddingGenerator
    ) -> None:
        """Test processing a batch of texts."""
        texts = [
            "first text",
            "second text is a bit longer",
            "Hello World",
            "hello world",  # Should be same as above
            "",  # Empty string
        ]
        result = await default_generator.generate_embeddings(texts)
        assert len(result.embeddings) == len(texts)

        # Check similarity of "Hello World" and "hello world" in batch
        dist_hello = cosine_distance(result.embeddings[2], result.embeddings[3])
        assert math.isclose(dist_hello, 0.0, abs_tol=1e-9)

        # Check empty string embedding in batch
        dim = default_generator.dimensionality
        expected_zero_vector = [0.0] * dim
        assert result.embeddings[4] == expected_zero_vector

    def test_invalid_initialization(self) -> None:
        """Test that HashingWordEmbeddingGenerator raises errors for invalid init params."""
        with pytest.raises(ValueError, match="Model name cannot be empty"):
            HashingWordEmbeddingGenerator(model_name="", dimensionality=128)
        with pytest.raises(
            ValueError, match="Dimensionality must be a positive integer"
        ):
            HashingWordEmbeddingGenerator(dimensionality=0)
        with pytest.raises(
            ValueError, match="Dimensionality must be a positive integer"
        ):
            HashingWordEmbeddingGenerator(dimensionality=-5)

    @pytest.mark.asyncio
    async def test_empty_input_list(
        self, default_generator: HashingWordEmbeddingGenerator
    ) -> None:
        """Test that providing an empty list of texts returns an empty result."""
        result = await default_generator.generate_embeddings([])
        assert result.embeddings == []
        assert result.model_name == default_generator.model_name
