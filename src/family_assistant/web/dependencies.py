import logging

from fastapi import HTTPException, Request

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import ToolsProvider

logger = logging.getLogger(__name__)


async def get_embedding_generator_dependency(request: Request) -> EmbeddingGenerator:
    """Retrieves the configured EmbeddingGenerator instance from app state."""
    generator = getattr(request.app.state, "embedding_generator", None)
    if not generator:
        logger.error("Embedding generator not found in app state.")
        # Raise HTTPException so FastAPI returns a proper error response
        raise HTTPException(
            status_code=500, detail="Embedding generator not configured or available."
        )
    if not isinstance(generator, EmbeddingGenerator):
        logger.error(
            f"Object in app state is not an EmbeddingGenerator: {type(generator)}"
        )
        raise HTTPException(
            status_code=500, detail="Invalid embedding generator configuration."
        )
    return generator


async def get_db() -> DatabaseContext:
    """FastAPI dependency to get a DatabaseContext."""
    # Uses the engine configured in storage/base.py by default.
    async with await get_db_context() as db_context:
        yield db_context


async def get_tools_provider_dependency(request: Request) -> ToolsProvider:
    """Retrieves the configured ToolsProvider instance from app state."""
    provider = getattr(request.app.state, "tools_provider", None)
    if not provider:
        logger.error("ToolsProvider not found in app state.")
        raise HTTPException(
            status_code=500, detail="ToolsProvider not configured or available."
        )
    return provider
