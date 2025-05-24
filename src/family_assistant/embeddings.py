"""
Module defining the interface and implementations for generating text embeddings.
"""

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable  # Added Type, Any

from litellm import aembedding
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

# Declare module/class variables that will be conditionally populated
_SentenceTransformer_cls: type[Any] | None = None
_np_module: Any | None = None
SENTENCE_TRANSFORMERS_AVAILABLE: bool  # Will be set in the try/except block

# Import sentence-transformers if available, otherwise skip the class definition
try:
    # sentence-transformers returns numpy arrays or torch tensors, need numpy for conversion
    import numpy
    from sentence_transformers import SentenceTransformer as ActualSentenceTransformer

    _np_module = numpy
    _SentenceTransformer_cls = ActualSentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    # _SentenceTransformer_cls and _np_module remain None as initialized

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """Represents the result of generating embeddings for a list of texts."""

    embeddings: list[list[float]]
    model_name: str


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


class LiteLLMEmbeddingGenerator:
    """Embedding generator implementation using the LiteLLM library."""

    def __init__(self, model: str, **kwargs: object) -> None:
        """
        Initializes the LiteLLM embedding generator.

        Args:
            model: The identifier of the embedding model to use (e.g., "text-embedding-ada-002").
            **kwargs: Additional keyword arguments to pass directly to litellm.aembedding.
        """
        if not model:
            raise ValueError("Embedding model identifier cannot be empty.")
        self._model_name = model
        self.embedding_kwargs = kwargs
        logger.info(
            f"LiteLLMEmbeddingGenerator initialized for model: {self._model_name} with kwargs: {self.embedding_kwargs}"
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        """Generates embeddings using LiteLLM's aembedding."""
        if not texts:
            logger.warning("generate_embeddings called with empty list of texts.")
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        logger.debug(
            f"Calling LiteLLM embedding model {self.model_name} for {len(texts)} texts."
        )
        try:
            # Combine fixed kwargs with per-call args
            call_kwargs = {
                **self.embedding_kwargs,
                "model": self.model_name,
                "input": texts,
            }
            response = await aembedding(**call_kwargs)

            # Extract embeddings from the response
            # LiteLLM's EmbeddingResponse structure has a 'data' field which is a list of Embedding objects
            # Each Embedding object has an 'embedding' field.
            embeddings_list = [item.embedding for item in response.data]

            logger.debug(
                f"LiteLLM embedding response received. Generated {len(embeddings_list)} embeddings."
            )
            return EmbeddingResult(
                embeddings=embeddings_list, model_name=self.model_name
            )

        except (
            APIConnectionError,
            Timeout,
            RateLimitError,
            ServiceUnavailableError,
            APIError,
        ) as e:
            logger.error(
                f"LiteLLM API error during embedding generation for model {self.model_name}: {e}",
                exc_info=True,
            )
            raise  # Re-raise the specific LiteLLM exception
        except Exception as e:
            logger.error(
                f"Unexpected error during LiteLLM embedding call for model {self.model_name}: {e}",
                exc_info=True,
            )
            # Wrap unexpected errors in a generic APIError or a custom exception
            raise APIError(
                message=f"Unexpected error during embedding: {e}",
                llm_provider="litellm",
                model=self.model_name,
                status_code=500,
            ) from e


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
            hash_val = hash(token)
            index = hash_val % self.dimensionality
            # Ensure index is positive, as hash() can return negative numbers
            if index < 0:
                index += self.dimensionality
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


# --- Sentence Transformer Implementation (Conditional) ---

if SENTENCE_TRANSFORMERS_AVAILABLE:

    class SentenceTransformerEmbeddingGenerator:
        """
        Embedding generator implementation using the sentence-transformers library
        for local embedding generation.
        """

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
            """
            if not model_name_or_path:
                raise ValueError(
                    "SentenceTransformer model name or path cannot be empty."
                )

            self._model_name = model_name_or_path  # Store the identifier used
            self.model_kwargs = kwargs
            try:
                logger.info(
                    f"Loading SentenceTransformer model: {model_name_or_path} on device: {device or 'auto'}"
                )
                # Ensure _SentenceTransformer_cls is not None before calling
                if (
                    _SentenceTransformer_cls is None
                ):  # Check the correctly typed variable
                    raise RuntimeError(
                        "SentenceTransformer class is None, library likely not installed."
                    )
                self.model = (
                    _SentenceTransformer_cls(  # Use the correctly typed variable
                        model_name_or_path, device=device, **self.model_kwargs
                    )
                )
                logger.info(
                    f"SentenceTransformer model {model_name_or_path} loaded successfully."
                )
            except Exception as e:
                logger.error(
                    f"Failed to load SentenceTransformer model '{model_name_or_path}': {e}",
                    exc_info=True,
                )
                raise ValueError(
                    f"Could not load SentenceTransformer model '{model_name_or_path}'"
                ) from e

        @property
        def model_name(self) -> str:
            # Return the identifier used, which might be a path or a hub name
            return self._model_name

        async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
            """Generates embeddings using the loaded SentenceTransformer model."""
            if not texts:
                logger.warning("generate_embeddings called with empty list of texts.")
                return EmbeddingResult(embeddings=[], model_name=self.model_name)

            logger.debug(
                f"Generating embeddings with SentenceTransformer model {self.model_name} for {len(texts)} texts."
            )
            try:
                # sentence-transformers encode is synchronous, run it in an executor
                # to avoid blocking the asyncio event loop.
                loop = asyncio.get_running_loop()
                # The encode method might return numpy arrays or torch tensors depending on config
                embeddings_np = await loop.run_in_executor(
                    None,  # Use default executor
                    self.model.encode,
                    texts,
                )

                # Convert numpy arrays to lists of floats
                # embeddings_np is expected to be a list of numpy arrays here.
                # The availability of numpy (_np_module) is implied by SENTENCE_TRANSFORMERS_AVAILABLE being true,
                # so an explicit check for _np_module is not strictly needed here for functionality,
                # but was the source of the original mypy error on np = None.
                embeddings_list = [arr.tolist() for arr in embeddings_np]

                logger.debug(
                    f"SentenceTransformer generated {len(embeddings_list)} embeddings."
                )
                return EmbeddingResult(
                    embeddings=embeddings_list, model_name=self.model_name
                )
            except Exception as e:
                logger.error(
                    f"Error during SentenceTransformer embedding generation for model {self.model_name}: {e}",
                    exc_info=True,
                )
                # Wrap errors appropriately
                raise RuntimeError(
                    f"Failed to generate embeddings with SentenceTransformer: {e}"
                ) from e

else:
    logger.warning(
        "sentence-transformers library not found. SentenceTransformerEmbeddingGenerator will not be available."
    )

    # Define a placeholder if the library is missing, so imports don't break elsewhere
    # if code explicitly tries to import SentenceTransformerEmbeddingGenerator
    class SentenceTransformerEmbeddingGenerator:  # pyright: ignore[reportRedeclaration] # type: ignore[no-redef] # noqa: F811
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ImportError(
                "sentence-transformers library is not installed. Cannot use SentenceTransformerEmbeddingGenerator."
            )

        async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
            """Placeholder for generate_embeddings to satisfy the protocol."""
            # This method should not be called if the library is not available.
            # Raising an error is consistent with the __init__ behavior.
            raise ImportError(
                "sentence-transformers library is not installed. Cannot use SentenceTransformerEmbeddingGenerator."
            )

        @property
        def model_name(self) -> str:
            """Placeholder for model_name to satisfy the protocol."""
            # Provide a distinct model name for the placeholder.
            return "unavailable-sentence-transformer"


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
    "EmbeddingResult",
    "EmbeddingGenerator",
    "LiteLLMEmbeddingGenerator",
    "HashingWordEmbeddingGenerator",  # Added new class
    "SentenceTransformerEmbeddingGenerator",
    "MockEmbeddingGenerator",
]

# Conditionally remove SentenceTransformerEmbeddingGenerator from __all__ if not available
if not SENTENCE_TRANSFORMERS_AVAILABLE:
    __all__.remove("SentenceTransformerEmbeddingGenerator")
