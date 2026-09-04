"""
Module defining the interface and implementations for generating text embeddings.
"""

import asyncio
import hashlib
import importlib.util
import logging
import math
import re
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol, cast, runtime_checkable

from google import genai
from google.genai import types as genai_types
from google.genai.client import DebugConfig
from openai import AsyncOpenAI

from family_assistant.llm.messages import MessageReasoningInfo
from family_assistant.observability.metrics import instrumented_llm_request

# Whether the optional sentence-transformers stack is installed. This is detected
# WITHOUT importing it: importing sentence_transformers pulls in torch (hundreds of
# MB resident), and most deployments use a cloud embedding provider and never need
# it. The actual import is deferred to SentenceTransformerEmbeddingGenerator, so
# torch is only loaded when a local embedding model is genuinely used.
SENTENCE_TRANSFORMERS_AVAILABLE = (
    importlib.util.find_spec("sentence_transformers") is not None
)

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """Represents the result of generating embeddings for a list of texts."""

    embeddings: list[list[float]]
    model_name: str


def _openai_embedding_usage(
    response: Any,  # noqa: ANN401 - openai CreateEmbeddingResponse
) -> MessageReasoningInfo | None:
    """Billable tokens for an OpenAI embedding request.

    Prompt side only: an embedding has no output, so setting anything else
    would invent a bucket the provider never billed.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return MessageReasoningInfo(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        total_tokens=getattr(usage, "total_tokens", 0) or 0,
    )


@runtime_checkable
class EmbeddingGenerator(Protocol):
    """Protocol defining the interface for generating text embeddings."""

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """
        Generates embeddings for a list of input texts.

        Args:
            texts: A list of strings to embed.

        Returns:
            An EmbeddingResult containing the list of embedding vectors and the model name used.

        Raises:
            Various exceptions (e.g., APIError, Timeout, ConnectionError) specific
            to the underlying implementation upon failure.
            ValueError: If input is invalid (e.g., empty list).
        """
        ...

    @property
    def model_name(self) -> str:
        """The identifier of the embedding model being used."""
        ...


class GoogleEmbeddingGenerator:
    """Embedding generator using the native Google GenAI SDK."""

    def __init__(
        self,
        model: str,
        dimensions: int | None = None,
        task_type: str | None = None,
        api_key: str | None = None,
        debug_config: DebugConfig | None = None,
    ) -> None:
        if not model:
            raise ValueError("Embedding model identifier cannot be empty.")
        self._model_name = model
        self._api_model_name = model.removeprefix("gemini/")
        if not self._api_model_name:
            raise ValueError("Embedding model identifier cannot be just 'gemini/'.")
        self._dimensions = dimensions
        self._task_type = task_type
        self._client = genai.Client(api_key=api_key, debug_config=debug_config)
        logger.info(
            f"GoogleEmbeddingGenerator initialized for model: {self._model_name}, "
            f"dimensions: {self._dimensions}, task_type: {self._task_type}"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """Generates embeddings using the Google GenAI SDK."""
        if not texts:
            logger.warning("generate_embeddings called with empty list of texts.")
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        logger.debug(
            f"Calling Google embedding model {self.model_name} for {len(texts)} texts."
        )
        config = genai_types.EmbedContentConfig(
            output_dimensionality=self._dimensions,
            task_type=self._task_type,
        )
        response = await instrumented_llm_request(
            provider="google",
            model=self.model_name,
            operation="embedding",
            request=lambda: self._client.aio.models.embed_content(
                model=self._api_model_name,
                contents=cast("Any", texts),
                config=config,
            ),
            # Google reports only `billable_character_count` on an embedding
            # response -- no token count at all -- so there is nothing honest to
            # put in a token bucket. The call counter is the meter here; a
            # zero-valued bucket would read as "embedded nothing".
        )

        if not response.embeddings:
            raise ValueError(
                f"Google GenAI API returned no embeddings for {len(texts)} input texts."
            )

        embeddings_list: list[list[float]] = []
        for embedding in response.embeddings:
            if embedding.values is None:
                raise ValueError(
                    "Google GenAI API returned an embedding with no values."
                )
            embeddings_list.append(list(embedding.values))

        if len(embeddings_list) != len(texts):
            raise ValueError(
                f"Google GenAI API returned {len(embeddings_list)} embeddings "
                f"for {len(texts)} input texts."
            )

        logger.debug(
            f"Google embedding response received. Generated {len(embeddings_list)} embeddings."
        )
        return EmbeddingResult(embeddings=embeddings_list, model_name=self.model_name)


class OpenAIEmbeddingGenerator:
    """Embedding generator for any OpenAI-compatible embeddings endpoint.

    Works with the OpenAI API directly as well as OpenAI-compatible gateways such
    as OpenRouter (``base_url="https://openrouter.ai/api/v1"``) or self-hosted
    inference servers. The model identifier is sent to the API verbatim, so it
    must match the provider's expectations (e.g. ``text-embedding-3-small`` for
    OpenAI, ``openai/text-embedding-3-small`` for OpenRouter).
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        if not model:
            raise ValueError("Embedding model identifier cannot be empty.")
        self._model_name = model
        self._dimensions = dimensions
        # AsyncOpenAI requires an api_key; OpenAI-compatible gateways often accept
        # any non-empty value, so fall back to a placeholder for keyless endpoints.
        self._client = AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url)
        logger.info(
            f"OpenAIEmbeddingGenerator initialized for model: {self._model_name}, "
            f"dimensions: {self._dimensions}, base_url: {base_url}"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """Generates embeddings using an OpenAI-compatible embeddings endpoint."""
        if not texts:
            logger.warning("generate_embeddings called with empty list of texts.")
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        logger.debug(
            f"Calling OpenAI-compatible embedding model {self.model_name} "
            f"for {len(texts)} texts."
        )
        create = (
            partial(
                self._client.embeddings.create,
                model=self._model_name,
                input=texts,
                dimensions=self._dimensions,
            )
            if self._dimensions is not None
            else partial(
                self._client.embeddings.create, model=self._model_name, input=texts
            )
        )
        response = await instrumented_llm_request(
            provider="openai",
            model=self.model_name,
            operation="embedding",
            request=create,
            usage=_openai_embedding_usage,
            served_model=lambda response: getattr(response, "model", None),
        )

        if not response.data:
            raise ValueError(
                f"OpenAI embeddings API returned no embeddings for "
                f"{len(texts)} input texts."
            )

        # The API may return data out of order; sort by the index field so each
        # embedding lines up with its corresponding input text.
        ordered = sorted(response.data, key=lambda item: item.index)
        embeddings_list = [list(item.embedding) for item in ordered]

        if len(embeddings_list) != len(texts):
            raise ValueError(
                f"OpenAI embeddings API returned {len(embeddings_list)} embeddings "
                f"for {len(texts)} input texts."
            )

        logger.debug(
            f"OpenAI embedding response received. "
            f"Generated {len(embeddings_list)} embeddings."
        )
        return EmbeddingResult(embeddings=embeddings_list, model_name=self.model_name)


class HashingWordEmbeddingGenerator:
    """
    Generates embeddings by tokenizing input into words, hashing each word
    to an index, incrementing a count at that index, and then normalizing
    the resulting vector to unit length.
    Input text is lowercased and special characters are removed.
    """

    def __init__(
        self, model_name: str = "hashing-word-v1", dimensionality: int = 128
    ) -> None:
        """
        Initializes the HashingWordEmbeddingGenerator.

        Args:
            model_name: The identifier for this embedding model.
            dimensionality: The number of dimensions for the output embedding vectors.
        """
        if not model_name:
            raise ValueError("Model name cannot be empty.")
        if dimensionality <= 0:
            raise ValueError("Dimensionality must be a positive integer.")
        self._model_name = model_name
        self.dimensionality = dimensionality
        logger.info(
            f"HashingWordEmbeddingGenerator initialized: model='{self._model_name}', dimensions={self.dimensionality}"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def _normalize_text(self, text: str) -> str:
        """Converts text to lowercase, removes special characters, and normalizes whitespace."""
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", "", text)  # Keep letters, numbers, and spaces
        text = re.sub(r"\s+", " ", text)  # Replace multiple spaces with a single space
        text = text.strip()  # Remove leading/trailing spaces
        return text

    def _generate_single_embedding(self, text: str) -> list[float]:
        """Generates a single embedding vector for the given text."""
        normalized_text = self._normalize_text(text)
        tokens = normalized_text.split()

        vector = [0.0] * self.dimensionality

        if not tokens:
            return vector  # Return zero vector for empty or whitespace-only text

        for token in tokens:
            # Use a deterministic hash function based on the token's bytes
            # This ensures consistent results across platforms and Python versions
            hash_val = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16)
            index = hash_val % self.dimensionality
            vector[index] += 1.0

        # Normalize the vector to unit length
        magnitude_sq = sum(x * x for x in vector)
        if magnitude_sq == 0:
            return (
                vector  # Should not happen if tokens were present, but as a safeguard
            )

        magnitude = math.sqrt(magnitude_sq)
        if (
            magnitude == 0
        ):  # Double check for safety, e.g. if all hashes collide to cancel out (highly unlikely)
            return vector

        normalized_vector = [x / magnitude for x in vector]
        return normalized_vector

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """
        Generates embeddings for a list of input texts using the hashing method.
        """
        if not texts:
            logger.warning(
                "HashingWordEmbeddingGenerator.generate_embeddings called with empty list of texts."
            )
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        logger.debug(
            f"HashingWordEmbeddingGenerator ({self.model_name}) processing {len(texts)} texts."
        )

        embeddings_list: list[list[float]] = []
        for text_content in texts:
            # This part is CPU-bound but typically very fast per text.
            # If it were significantly slower, asyncio.to_thread might be considered.
            embedding = self._generate_single_embedding(text_content)
            embeddings_list.append(embedding)

        logger.debug(
            f"HashingWordEmbeddingGenerator ({self.model_name}) generated {len(embeddings_list)} embeddings."
        )
        return EmbeddingResult(embeddings=embeddings_list, model_name=self.model_name)


# --- Sentence Transformer Implementation (Unified) ---


class SentenceTransformerEmbeddingGenerator:
    """
    Embedding generator implementation using the sentence-transformers library
    for local embedding generation.
    If sentence-transformers is not available, instantiation or usage will raise an ImportError.
    """

    _model_name: str  # Instance variable for the model name/path
    model: Any  # Instance variable for the loaded SentenceTransformer model
    # ast-grep-ignore: no-dict-any - SentenceTransformer accepts arbitrary keyword arguments
    model_kwargs: dict[str, Any]  # Instance variable for model kwargs

    def __init__(
        self, model_name_or_path: str, device: str | None = None, **kwargs: object
    ) -> None:
        """
        Initializes the SentenceTransformer embedding generator.

        Args:
            model_name_or_path: The name of a model from HuggingFace Hub (e.g., 'all-MiniLM-L6-v2')
                                or a path to a local model directory.
            device: The device to run the model on (e.g., 'cpu', 'cuda', 'mps'). If None,
                    sentence-transformers will attempt auto-detection.
            **kwargs: Additional keyword arguments passed to the SentenceTransformer constructor.

        Raises:
            ImportError: If the sentence-transformers library is not installed.
            ValueError: If model_name_or_path is empty or model loading fails.
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "sentence-transformers library is not installed. Cannot use SentenceTransformerEmbeddingGenerator."
            )

        if not model_name_or_path:
            raise ValueError("SentenceTransformer model name or path cannot be empty.")

        self._model_name = model_name_or_path
        self.model_kwargs = kwargs  # type: ignore[assignment]
        try:
            logger.info(
                f"Loading SentenceTransformer model: {model_name_or_path} on device: {device or 'auto'}"
            )
            # Lazy import so torch is only loaded when a local model is used.
            from sentence_transformers import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports] # lazy import: avoid loading torch at module import
                SentenceTransformer,
            )

            self.model = SentenceTransformer(
                model_name_or_path, device=device, **self.model_kwargs
            )
            logger.info(
                f"SentenceTransformer model {model_name_or_path} loaded successfully."
            )
        except Exception as e:
            logger.exception(
                f"Failed to load SentenceTransformer model '{model_name_or_path}': {e}"
            )
            raise ValueError(
                f"Could not load SentenceTransformer model '{model_name_or_path}'"
            ) from e

    @property
    def model_name(self) -> str:
        """The identifier of the embedding model being used."""
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            # This case should ideally be caught by __init__, but as a fallback:
            return "unavailable-sentence-transformer"
        return self._model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """
        Generates embeddings using the loaded SentenceTransformer model.

        Raises:
            ImportError: If the sentence-transformers library is not installed (should be caught by __init__).
            RuntimeError: If embedding generation fails.
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            # This check is somewhat redundant if __init__ raises, but good for safety.
            raise ImportError(
                "sentence-transformers library is not installed. Cannot use SentenceTransformerEmbeddingGenerator."
            )

        if not texts:
            logger.warning("generate_embeddings called with empty list of texts.")
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        logger.debug(
            f"Generating embeddings with SentenceTransformer model {self.model_name} for {len(texts)} texts."
        )
        try:
            loop = asyncio.get_running_loop()
            embeddings_np = await loop.run_in_executor(None, self.model.encode, texts)

            # numpy ships with sentence-transformers; import lazily alongside it.
            import numpy  # noqa: PLC0415 # deferred to avoid importing torch stack at module load

            embeddings_list = [numpy.array(arr).tolist() for arr in embeddings_np]

            logger.debug(
                f"SentenceTransformer generated {len(embeddings_list)} embeddings."
            )
            return EmbeddingResult(
                embeddings=embeddings_list, model_name=self.model_name
            )
        except Exception as e:
            logger.exception(
                f"Error during SentenceTransformer embedding generation for model {self.model_name}: {e}"
            )
            raise RuntimeError(
                f"Failed to generate embeddings with SentenceTransformer: {e}"
            ) from e


if not SENTENCE_TRANSFORMERS_AVAILABLE:
    logger.warning(
        "sentence-transformers library not found. SentenceTransformerEmbeddingGenerator will not be fully available."
    )


class MockEmbeddingGenerator:
    """
    A mock embedding generator that returns predefined embeddings based on input text.
    Useful for testing without making actual API calls.
    """

    def __init__(
        self,
        model_name: str = "mock-embedding-model",
        dimensions: int = 10,
        embedding_map: dict[str, list[float]] | None = None,
        default_embedding_behavior: str = "generate",
        fixed_default_embedding: list[float] | None = None,
    ) -> None:
        """
        Initializes the mock embedding generator.

        Args:
            model_name: The model name to report in the EmbeddingResult.
            dimensions: The dimensionality of the embeddings to generate.
            embedding_map: An optional dictionary mapping input text strings to their
                           corresponding embedding vectors.
            default_embedding_behavior: Behavior if text not in map or map not provided:
                "generate": Generate a deterministic embedding.
                "error": Raise LookupError (if text not in map).
                "fixed_default": Use `fixed_default_embedding`.
            fixed_default_embedding: A fixed default embedding to return if behavior is "fixed_default"
                                     and text is not in map. Must match `dimensions`.
        """
        self._model_name = model_name
        self.dimensions = dimensions
        self.embedding_map = embedding_map or {}  # Use empty map if None
        self.default_embedding_behavior = default_embedding_behavior

        if default_embedding_behavior == "fixed_default":
            if fixed_default_embedding is None:
                raise ValueError(
                    "fixed_default_embedding must be provided when behavior is 'fixed_default'"
                )
            if len(fixed_default_embedding) != dimensions:
                raise ValueError(
                    f"fixed_default_embedding length ({len(fixed_default_embedding)}) must match dimensions ({dimensions})"
                )
        self.fixed_default_embedding = fixed_default_embedding
        # Remove self.default_embedding as it's superseded by the new behavior args
        logger.info(
            f"MockEmbeddingGenerator initialized for model: {self._model_name}, dimensions: {self.dimensions}, map size: {len(self.embedding_map)}, default_behavior: {self.default_embedding_behavior}"
        )

    def _generate_deterministic_vector(self, text: str) -> list[float]:
        """Generates a simple deterministic vector based on text content."""
        # Simple hash-like vector, ensuring it has `self.dimensions`
        vector = [0.0] * self.dimensions
        if not text:  # Handle empty string
            return vector

        for i, char_code in enumerate(text.encode("utf-8", "ignore")):
            vector[i % self.dimensions] = (
                vector[i % self.dimensions] + float(char_code)
            ) / 2.0

        # Normalize or scale if necessary, for now, keep it simple
        return [val / 255.0 for val in vector]  # Basic scaling

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """Looks up embeddings in the map or returns default/raises error."""
        if not texts:
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        results = []
        for text in texts:
            if text in self.embedding_map:
                # Ensure the stored embedding matches the expected dimensions
                stored_embedding = self.embedding_map[text]
                if len(stored_embedding) == self.dimensions:
                    results.append(stored_embedding)
                else:
                    logger.warning(
                        f"Embedding for '{text[:50]}...' in map has length {len(stored_embedding)}, expected {self.dimensions}. Generating new one."
                    )
                    # Fall through to generation/default logic if dimensions mismatch
                    if self.default_embedding_behavior == "generate":
                        results.append(self._generate_deterministic_vector(text))
                    elif (
                        self.default_embedding_behavior == "fixed_default"
                        and self.fixed_default_embedding
                    ):
                        results.append(self.fixed_default_embedding)
                    else:  # "error" or misconfiguration
                        logger.error(
                            f"Dimension mismatch for '{text[:50]}...' and no valid fallback. Map length: {len(stored_embedding)}, expected: {self.dimensions}."
                        )
                        raise ValueError(
                            f"Dimension mismatch for '{text[:50]}...' and no valid fallback."
                        )
            elif self.default_embedding_behavior == "generate":
                results.append(self._generate_deterministic_vector(text))
            elif (
                self.default_embedding_behavior == "fixed_default"
                and self.fixed_default_embedding is not None
            ):
                logger.debug(
                    f"Text '{text[:50]}...' not found in mock map, using fixed default embedding."
                )
                results.append(self.fixed_default_embedding)
            elif self.default_embedding_behavior == "error":
                logger.error(
                    f"Text '{text[:50]}...' not found in mock embedding map and behavior is 'error'."
                )
                raise LookupError(
                    f"Text '{text[:50]}...' not found in mock embedding map."
                )
            else:  # Should not happen with proper config
                logger.error(
                    f"Invalid default_embedding_behavior: {self.default_embedding_behavior} for text '{text[:50]}...'"
                )
                raise ValueError(
                    f"Invalid default_embedding_behavior: {self.default_embedding_behavior}"
                )

        logger.debug(
            f"Mock generator returning {len(results)} embeddings for model {self.model_name}."
        )
        return EmbeddingResult(embeddings=results, model_name=self.model_name)


__all__ = [
    "EmbeddingGenerator",
    "EmbeddingResult",
    "GoogleEmbeddingGenerator",
    "HashingWordEmbeddingGenerator",  # Added new class
    "MockEmbeddingGenerator",
    "OpenAIEmbeddingGenerator",
    "SentenceTransformerEmbeddingGenerator",
]

# Conditionally remove SentenceTransformerEmbeddingGenerator from __all__ if not available
if not SENTENCE_TRANSFORMERS_AVAILABLE:
    __all__.remove("SentenceTransformerEmbeddingGenerator")
