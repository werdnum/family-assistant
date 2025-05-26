import logging
from collections.abc import AsyncGenerator

from fastapi import HTTPException, Request, status  # Added status

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


async def get_db() -> AsyncGenerator[DatabaseContext, None]:
    """FastAPI dependency to get a DatabaseContext."""
    # Uses the engine configured in storage/base.py by default.
    async with get_db_context() as db_context:
        yield db_context


async def get_tools_provider_dependency(request: Request) -> ToolsProvider:
    """Retrieves the configured ToolsProvider instance from app state."""
    provider = getattr(request.app.state, "tools_provider", None)
    if not provider:
        logger.error("ToolsProvider not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ToolsProvider not configured or available."
        )
    return provider


async def get_processing_service(request: Request) -> "ProcessingService":  # type: ignore
    """Retrieves the ProcessingService instance from app state."""
    # Forward reference for ProcessingService, will be resolved at runtime
    # from family_assistant.processing import ProcessingService # Avoid circular import at top level

    service = getattr(request.app.state, "processing_service", None)
    if not service:
        logger.error("ProcessingService not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ProcessingService not configured or available."
        )
    # isinstance check would require importing ProcessingService, which can cause circular deps
    # Rely on correct setup in main.py for now.
    return service


async def get_current_api_user(request: Request) -> dict:
    """
    Dependency to get the current user from an API token.
    Validates the token and fetches user details.
    """
    from family_assistant.web.auth import (  # Import locally to avoid top-level circularity if any
        get_user_from_api_token,
    )

    token_value: str | None = None
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.lower().startswith("bearer "):
        token_value = auth_header.split(" ", 1)[1]
        logger.debug("Attempting API token auth using Authorization Bearer header.")
    else:
        # Fallback to X-API-Token if Authorization header is not a Bearer token or not present
        token_value = request.headers.get("X-API-Token")
        if token_value:
            logger.debug("Attempting API token auth using X-API-Token header.")

    if not token_value:
        logger.warning("API token not provided in Authorization or X-API-Token header.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: API token required.",
            headers={"WWW-Authenticate": 'Bearer realm="api", error="missing_token"'},
        )

    # Construct the header string as expected by get_user_from_api_token
    # get_user_from_api_token expects the full "Bearer <token>" string.
    auth_header_for_validation = f"Bearer {token_value}"
    api_user = await get_user_from_api_token(auth_header_for_validation, request)

    if not api_user:
        # Log prefix for security, avoid logging full token
        logger.warning(f"Invalid or expired API token provided. Token prefix: {token_value[:8]}...")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API token.",
            headers={"WWW-Authenticate": 'Bearer realm="api", error="invalid_token"'},
        )
    
    logger.info(f"API user authenticated: {api_user.get('sub')}")
    return api_user


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
