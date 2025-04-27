"""
Module defining the interface and implementations for generating text embeddings.
"""

import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Protocol

from litellm import aembedding
from litellm.exceptions import (
    APIConnectionError,
    Timeout,
    RateLimitError,
    ServiceUnavailableError,
    APIError,
)

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """Represents the result of generating embeddings for a list of texts."""

    embeddings: List[List[float]]
    model_name: str


class EmbeddingGenerator(Protocol):
    """Protocol defining the interface for generating text embeddings."""

    async def generate_embeddings(self, texts: List[str]) -> EmbeddingResult:
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

    def __init__(self, model: str, **kwargs: Any):
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

    async def generate_embeddings(self, texts: List[str]) -> EmbeddingResult:
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


class MockEmbeddingGenerator:
    """
    A mock embedding generator that returns predefined embeddings based on input text.
    Useful for testing without making actual API calls.
    """

    def __init__(
        self,
        embedding_map: Dict[str, List[float]],
        model_name: str = "mock-embedding-model",
        default_embedding: Optional[List[float]] = None,
    ):
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

    async def generate_embeddings(self, texts: List[str]) -> EmbeddingResult:
        """Looks up embeddings in the map or returns default/raises error."""
        if not texts:
            return EmbeddingResult(embeddings=[], model_name=self.model_name)

        results = []
        for text in texts:
            if text in self.embedding_map:
                results.append(self.embedding_map[text])
            elif self.default_embedding is not None:
                logger.warning(
                    f"Text '{text[:50]}...' not found in mock map, using default embedding."
                )
                results.append(self.default_embedding)
            else:
                logger.error(f"Text '{text[:50]}...' not found in mock embedding map.")
                raise LookupError(
                    f"Text '{text[:50]}...' not found in mock embedding map and no default embedding provided."
                )

        logger.debug(f"Mock generator returning {len(results)} embeddings.")
        return EmbeddingResult(embeddings=results, model_name=self.model_name)


__all__ = [
    "EmbeddingResult",
    "EmbeddingGenerator",
    "LiteLLMEmbeddingGenerator",
    "MockEmbeddingGenerator",
]
