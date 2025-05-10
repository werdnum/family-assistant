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


async def get_current_active_user(request: Request) -> dict:
    """
    Dependency to get the current user from the session.
    Ensures the user is authenticated via OIDC (not an API token)
    for operations like creating new API tokens.
    """
    if not hasattr(request.app.state, "config"):
        logger.error("Application config not found in app.state.")
        raise HTTPException(
            status_code=500,  # Internal Server Error
            detail="Server configuration error.",
        )

    auth_enabled = request.app.state.config.get("auth_enabled", False)

    if not auth_enabled:
        # If auth is not enabled, create a mock user for development/testing.
        logger.warning(
            "Auth is disabled. Returning a mock user for get_current_active_user."
        )
        return {
            "sub": "mock_user_sub_for_disabled_auth",
            "name": "Mock User (Auth Disabled)",
            "email": "mock@example.com",
            "source": "mock_auth_disabled",
        }

    user = request.session.get("user")
    if not user:
        logger.debug("No user in session. Raising 401 Unauthorized.")
        raise HTTPException(
            status_code=401,  # Unauthorized
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if the user was authenticated via an API token
    if user.get("source") == "api_token":
        logger.warning(
            "Attempt to access token creation/management endpoint with an API token. User: %s",
            user.get("sub"),
        )
        raise HTTPException(
            status_code=403,  # Forbidden
            detail="API tokens cannot be used to manage other API tokens. Please log in via the web UI.",
        )

    # User is authenticated via OIDC (or another primary method)
    logger.debug("Current active user (OIDC): %s", user.get("sub"))
    return user
