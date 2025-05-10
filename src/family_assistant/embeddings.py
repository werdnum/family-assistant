"""
Module defining the interface and implementations for generating text embeddings.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Import sentence-transformers if available, otherwise skip the class definition
try:
    # sentence-transformers returns numpy arrays or torch tensors, need numpy for conversion
    import numpy as np
    from sentence_transformers import SentenceTransformer

    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False
    SentenceTransformer = None  # Define as None to satisfy type checkers if needed
    np = None  # Define as None

from litellm import aembedding
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

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
                # Ensure SentenceTransformer is not None before calling
                if SentenceTransformer is None:
                    raise RuntimeError(
                        "SentenceTransformer class is None, library likely not installed."
                    )
                self.model = SentenceTransformer(
                    model_name_or_path, device=device, **self.model_kwargs
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
                # Ensure np is available due to conditional import
                if np is None:
                    raise RuntimeError("Numpy is required but not available.")
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
    class SentenceTransformerEmbeddingGenerator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ImportError(
                "sentence-transformers library is not installed. Cannot use SentenceTransformerEmbeddingGenerator."
            )


class MockEmbeddingGenerator:
    """
    A mock embedding generator that returns predefined embeddings based on input text.
    Useful for testing without making actual API calls.
    """

    def __init__(
        self,
        embedding_map: dict[str, list[float]],
        model_name: str = "mock-embedding-model",
        default_embedding: list[float] | None = None,
    ) -> None:
        """
        Initializes the mock embedding generator.

        Args:
            embedding_map: A dictionary mapping input text strings to their corresponding
                           embedding vectors (List[float]).
            model_name: The model name to report in the EmbeddingResult.
            default_embedding: An optional default embedding to return if a text is not
                               found in the map. If None, a LookupError is raised.
        """
        self.embedding_map = embedding_map
        self._model_name = model_name
        self.default_embedding = default_embedding
        logger.info(
            f"MockEmbeddingGenerator initialized for model: {self._model_name}. Map size: {len(embedding_map)}"
        )

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
                results.append(self.embedding_map[text])
            elif self.default_embedding is not None:
                logger.warning(
                    f"Text '{text[:1000]}...' not found in mock map, using default embedding."
                )
                results.append(self.default_embedding)
            else:
                logger.error(
                    f"Text '{text[:1000]}...' not found in mock embedding map."
                )
                raise LookupError(
                    f"Text '{text[:1000]}...' not found in mock embedding map and no default embedding provided."
                )

        logger.debug(f"Mock generator returning {len(results)} embeddings.")
        return EmbeddingResult(embeddings=results, model_name=self.model_name)


__all__ = [
    "EmbeddingResult",
    "EmbeddingGenerator",
    "LiteLLMEmbeddingGenerator",
    "SentenceTransformerEmbeddingGenerator",  # Add the new class
    "MockEmbeddingGenerator",
]

# Conditionally remove SentenceTransformerEmbeddingGenerator from __all__ if not available
if not SENTENCE_TRANSFORMERS_AVAILABLE:
    __all__.remove("SentenceTransformerEmbeddingGenerator")
