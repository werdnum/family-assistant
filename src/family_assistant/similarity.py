"""Similarity strategies for comparing calendar event titles.

Provides pluggable strategies for computing similarity between event titles,
used for duplicate detection and enhanced search functionality.
"""

from __future__ import annotations

import asyncio
import difflib
import functools
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import (  # pyright: ignore[reportMissingImports]
        SentenceTransformer,
    )

    from family_assistant.tools.types import CalendarConfig

logger = logging.getLogger(__name__)


@runtime_checkable
class SimilarityStrategy(Protocol):
    """Protocol for computing similarity between calendar event titles."""

    async def compute_similarities(
        self, query: str, titles: Sequence[str]
    ) -> list[float]:
        """Similarity of *query* to each of *titles*, in order.

        Scores run from 0.0 (completely different) to 1.0 (identical). One
        call scores a whole batch, so an implementation backed by a model can
        encode everything at once.
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

    async def compute_similarities(
        self, query: str, titles: Sequence[str]
    ) -> list[float]:
        query_lower = query.lower()
        return [
            difflib.SequenceMatcher(None, query_lower, title.lower()).ratio()
            for title in titles
        ]

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
            from sentence_transformers import (  # noqa: PLC0415  # pyright: ignore[reportMissingImports]
                SentenceTransformer,
            )
        except ImportError as e:
            raise ImportError(
                "sentence-transformers library is not installed. "
                "Install with: uv sync --extra local-embeddings"
            ) from e

        self.model_name = model_name
        self.device = device

        logger.info(
            f"Loading sentence-transformer model: {model_name} on device: {device}"
        )
        self.model: SentenceTransformer = SentenceTransformer(model_name, device=device)
        logger.info(f"Model {model_name} loaded successfully")

    async def compute_similarities(
        self, query: str, titles: Sequence[str]
    ) -> list[float]:
        """Cosine similarity of *query* to each title, from one batched encode."""
        if not titles:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._score_batch, query, titles)

    def _score_batch(self, query: str, titles: Sequence[str]) -> list[float]:
        embeddings = np.asarray(self.model.encode([query, *titles]))
        query_embedding, title_embeddings = embeddings[0], embeddings[1:]
        norms = np.linalg.norm(title_embeddings, axis=1) * np.linalg.norm(
            query_embedding
        )
        dots = title_embeddings @ query_embedding
        scores = np.divide(dots, norms, out=np.zeros_like(dots), where=norms != 0)
        return [float(score) for score in scores]

    @property
    def name(self) -> str:
        return f"embedding ({self.model_name})"


@functools.lru_cache(maxsize=4)
def load_embedding_strategy(
    model_name: str, device: str
) -> EmbeddingSimilarityStrategy:
    """The process-wide strategy for *model_name* on *device*.

    Loading a sentence-transformer takes seconds and the loaded model is
    read-only, so every search and duplicate check shares one instance.
    """
    return EmbeddingSimilarityStrategy(model_name=model_name, device=device)


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
        return load_embedding_strategy(model_name, device)
    else:
        raise ValueError(
            f"Unknown similarity strategy: {strategy_type}. "
            f"Valid options: 'fuzzy', 'embedding'"
        )


def create_similarity_strategy_from_config(
    calendar_config: CalendarConfig,
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
    dup_detection = calendar_config.get("duplicate_detection")

    # Check if duplicate detection is enabled
    if not dup_detection or not dup_detection.get("enabled", True):
        logger.info("Duplicate detection is disabled, using fuzzy strategy as fallback")
        return FuzzySimilarityStrategy()

    strategy_type = dup_detection.get("similarity_strategy", "embedding")

    if strategy_type == "fuzzy":
        return FuzzySimilarityStrategy()
    elif strategy_type == "embedding":
        embedding_config = dup_detection.get("embedding") or {}
        model_name = str(
            embedding_config.get("model", "sentence-transformers/all-MiniLM-L6-v2")
        )
        device = str(embedding_config.get("device", "cpu"))
        return load_embedding_strategy(model_name, device)
    else:
        logger.warning(
            f"Unknown similarity strategy '{strategy_type}', falling back to fuzzy"
        )
        return FuzzySimilarityStrategy()
