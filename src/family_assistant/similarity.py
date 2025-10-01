"""Similarity strategies for comparing calendar event titles.

Provides pluggable strategies for computing similarity between event titles,
used for duplicate detection and enhanced search functionality.
"""

import asyncio
import difflib
import logging
from typing import Protocol, runtime_checkable

import numpy as np

logger = logging.getLogger(__name__)


@runtime_checkable
class SimilarityStrategy(Protocol):
    """Protocol for computing similarity between calendar event titles."""

    async def compute_similarity(self, title1: str, title2: str) -> float:
        """
        Compute similarity score between two event titles.

        Args:
            title1: First event title
            title2: Second event title

        Returns:
            float: Similarity score between 0.0 (completely different) and 1.0 (identical)
        """
        ...

    @property
    def name(self) -> str:
        """Name of this similarity strategy for logging/debugging."""
        ...


class FuzzySimilarityStrategy:
    """Fuzzy string matching using difflib.SequenceMatcher.

    Fast, zero dependencies, suitable for unit tests and lightweight deployments.
    Performance: ~0.03ms per comparison, F1=0.843 at threshold 0.30
    """

    async def compute_similarity(self, title1: str, title2: str) -> float:
        """Compute fuzzy string similarity using difflib.

        Args:
            title1: First event title
            title2: Second event title

        Returns:
            float: Similarity ratio between 0.0 and 1.0
        """
        return difflib.SequenceMatcher(None, title1.lower(), title2.lower()).ratio()

    @property
    def name(self) -> str:
        return "fuzzy_string"


class EmbeddingSimilarityStrategy:
    """Semantic similarity using sentence-transformers embeddings.

    Uses local sentence-transformer models for semantic matching.
    Performance: ~55ms per comparison, F1=0.888 at threshold 0.30, 98.8% recall
    Requires: local-embeddings extra (~87MB for all-MiniLM-L6-v2)

    Default model: sentence-transformers/all-MiniLM-L6-v2
    - 30% faster than granite-30m-english
    - 25% smaller (87MB vs 116MB)
    - Nearly identical F1 score (0.888 vs 0.889)
    """

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str = "cpu",
    ) -> None:
        """Initialize embedding similarity strategy.

        Args:
            model_name: Name or path of sentence-transformers model
            device: Device to run model on ("cpu", "cuda", "mps")

        Raises:
            ImportError: If sentence-transformers is not installed
        """
        try:
            # Import here rather than top-level because sentence-transformers is an optional
            # dependency (local-embeddings extra). This allows FuzzySimilarityStrategy to work
            # without requiring the extra to be installed.
            from sentence_transformers import (  # noqa: PLC0415
                SentenceTransformer,
            )
        except ImportError as e:
            raise ImportError(
                "sentence-transformers library is not installed. "
                "Install with: uv pip install -e '.[local-embeddings]'"
            ) from e

        self.model_name = model_name
        self.device = device

        logger.info(
            f"Loading sentence-transformer model: {model_name} on device: {device}"
        )
        self.model = SentenceTransformer(model_name, device=device)
        logger.info(f"Model {model_name} loaded successfully")

    async def compute_similarity(self, title1: str, title2: str) -> float:
        """Compute semantic similarity using embeddings.

        Args:
            title1: First event title
            title2: Second event title

        Returns:
            float: Cosine similarity between embeddings (0.0 to 1.0)
        """
        # Run embedding generation in executor to avoid blocking
        loop = asyncio.get_running_loop()

        emb1 = await loop.run_in_executor(None, self.model.encode, title1)
        emb2 = await loop.run_in_executor(None, self.model.encode, title2)

        # Compute cosine similarity
        dot_product = np.dot(emb1, emb2)
        norm1 = np.linalg.norm(emb1)
        norm2 = np.linalg.norm(emb2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    @property
    def name(self) -> str:
        return f"embedding ({self.model_name})"


def create_similarity_strategy(
    strategy_type: str = "embedding",
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    device: str = "cpu",
) -> SimilarityStrategy:
    """Factory function to create a similarity strategy from configuration.

    Args:
        strategy_type: Type of strategy ("fuzzy" or "embedding")
        model_name: Model name for embedding strategy (ignored for fuzzy)
        device: Device for embedding strategy (ignored for fuzzy)

    Returns:
        SimilarityStrategy: Configured similarity strategy

    Raises:
        ValueError: If strategy_type is not recognized
        ImportError: If embedding strategy is requested but dependencies missing
    """
    if strategy_type == "fuzzy":
        logger.info("Using fuzzy string similarity strategy")
        return FuzzySimilarityStrategy()
    elif strategy_type == "embedding":
        logger.info(f"Using embedding similarity strategy with model: {model_name}")
        return EmbeddingSimilarityStrategy(model_name=model_name, device=device)
    else:
        raise ValueError(
            f"Unknown similarity strategy: {strategy_type}. "
            f"Valid options: 'fuzzy', 'embedding'"
        )


def create_similarity_strategy_from_config(
    calendar_config: dict,
) -> SimilarityStrategy:
    """Create a similarity strategy from calendar configuration dict.

    Args:
        calendar_config: Calendar configuration dictionary containing
                        duplicate_detection settings

    Returns:
        SimilarityStrategy: Configured similarity strategy

    Raises:
        ValueError: If configuration is invalid
        ImportError: If embedding strategy is requested but dependencies missing

    Example:
        >>> config = {
        ...     "duplicate_detection": {
        ...         "similarity_strategy": "embedding",
        ...         "embedding": {
        ...             "model": "sentence-transformers/all-MiniLM-L6-v2",
        ...             "device": "cpu"
        ...         }
        ...     }
        ... }
        >>> strategy = create_similarity_strategy_from_config(config)
    """
    dup_detection = calendar_config.get("duplicate_detection", {})

    # Check if duplicate detection is enabled
    if not dup_detection.get("enabled", True):
        logger.info("Duplicate detection is disabled, using fuzzy strategy as fallback")
        return FuzzySimilarityStrategy()

    strategy_type = dup_detection.get("similarity_strategy", "embedding")

    if strategy_type == "fuzzy":
        return FuzzySimilarityStrategy()
    elif strategy_type == "embedding":
        embedding_config = dup_detection.get("embedding", {})
        model_name = embedding_config.get(
            "model", "sentence-transformers/all-MiniLM-L6-v2"
        )
        device = embedding_config.get("device", "cpu")
        return EmbeddingSimilarityStrategy(model_name=model_name, device=device)
    else:
        logger.warning(
            f"Unknown similarity strategy '{strategy_type}', falling back to fuzzy"
        )
        return FuzzySimilarityStrategy()
