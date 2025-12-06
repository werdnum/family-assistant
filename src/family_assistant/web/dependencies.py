import logging
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from family_assistant.embeddings import EmbeddingGenerator
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import ToolsProvider
from family_assistant.web.voice_client import GoogleGeminiLiveClient, LiveAudioClient

if TYPE_CHECKING:
    from family_assistant.processing import ProcessingService  # Import for type hinting
    from family_assistant.web.web_chat_interface import WebChatInterface

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


async def get_db(request: Request) -> AsyncGenerator[DatabaseContext]:
    """FastAPI dependency to get a DatabaseContext."""
    # Get engine from app.state (set by Assistant during setup)
    engine = request.app.state.database_engine
    if not engine:
        raise RuntimeError("Database engine not initialized in app.state")

    # Get message_notifier from app.state (optional, for live message updates)
    message_notifier = getattr(request.app.state, "message_notifier", None)

    async with get_db_context(engine, message_notifier=message_notifier) as db_context:
        yield db_context


async def get_tools_provider_dependency(request: Request) -> ToolsProvider:
    """Retrieves the configured ToolsProvider instance from app state."""
    provider = getattr(request.app.state, "tools_provider", None)
    if not provider:
        logger.error("ToolsProvider not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ToolsProvider not configured or available.",
        )
    return provider


async def get_processing_service(request: Request) -> "ProcessingService":
    """Retrieves the ProcessingService instance from app state."""
    # Forward reference for ProcessingService is handled by TYPE_CHECKING block

    service = getattr(request.app.state, "processing_service", None)
    if not service:
        logger.error("ProcessingService not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ProcessingService not configured or available.",
        )
    # isinstance check would require importing ProcessingService, which can cause circular deps
    # Rely on correct setup in main.py for now.
    return service


async def get_current_user(request: Request) -> dict:
    """
    Dependency to get the current user from either session (web UI) or API token.
    Validates authentication and returns user details.

    This dependency supports both:
    - Session-based auth (web UI with cookies)
    - API token auth (API clients with Authorization header)
    """
    # Get AuthService from app state
    auth_service = getattr(request.app.state, "auth_service", None)
    if not auth_service or not auth_service.auth_enabled:
        # Return test user when auth is disabled (e.g., in tests)
        logger.debug("Auth is disabled, returning test user.")
        return {
            "user_identifier": "test_user",
            "token_id": 0,
            "token_name": "test_token",
            "expires_at": None,
        }

    # First try session auth (for web UI)
    try:
        session_user = request.session.get("user")
        if session_user:
            logger.debug("User authenticated via session.")
            return {
                "user_identifier": session_user.get(
                    "sub", session_user.get("email", "session_user")
                ),
                **session_user,
            }
    except AssertionError:
        # Session middleware not available
        pass

    # Fall back to API token auth (for API clients)
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
        logger.warning("No authentication provided (session or API token).")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated: session or API token required.",
            headers={"WWW-Authenticate": 'Bearer realm="api", error="missing_token"'},
        )

    # Construct the header string as expected by get_user_from_api_token
    # get_user_from_api_token expects the full "Bearer <token>" string.
    auth_header_for_validation = f"Bearer {token_value}"
    api_user = await auth_service.get_user_from_api_token(
        auth_header_for_validation, request
    )

    if not api_user:
        # Log prefix for security, avoid logging full token
        logger.warning(
            f"Invalid or expired API token provided. Token prefix: {token_value[:8]}..."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API token.",
            headers={"WWW-Authenticate": 'Bearer realm="api", error="invalid_token"'},
        )

    logger.info(f"API user authenticated: {api_user.get('sub')}")
    return {
        "user_identifier": api_user.get("sub", "api_user"),
        **api_user,
    }


async def get_current_api_user(request: Request) -> dict:
    """
    Legacy dependency for API token authentication only.
    Use get_current_user for endpoints that support both session and API token auth.
    """
    return await get_current_user(request)


async def get_current_active_user(request: Request) -> dict:
    """
    Dependency to get the current user from the session.
    Ensures the user is authenticated via OIDC (not an API token)
    for operations like creating new API tokens.
    """
    # Get AuthService from app state
    auth_service = getattr(request.app.state, "auth_service", None)
    if not auth_service:
        logger.error("AuthService not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service not configured.",
        )

    auth_enabled = auth_service.auth_enabled

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

    # Try to get user from session
    try:
        user = request.session.get("user")
    except AssertionError:
        # Session middleware not available
        logger.warning("Session middleware not available. Cannot authenticate user.")
        raise HTTPException(
            status_code=401,  # Unauthorized
            detail="Session not available - authentication not configured",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
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


async def get_attachment_registry(request: Request) -> AttachmentRegistry:
    """Retrieves the AttachmentRegistry instance from app state."""
    registry = getattr(request.app.state, "attachment_registry", None)
    if not registry:
        logger.error("AttachmentRegistry not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AttachmentRegistry not configured or available.",
        )
    return registry


async def get_web_chat_interface(request: Request) -> "WebChatInterface":
    """Retrieves the WebChatInterface instance from app state."""
    web_chat_interface = getattr(request.app.state, "web_chat_interface", None)
    if not web_chat_interface:
        logger.error("WebChatInterface not found in app state.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="WebChatInterface not configured or available.",
        )
    return web_chat_interface


async def get_live_audio_client(request: Request) -> LiveAudioClient:
    """Dependency to get a configured LiveAudioClient."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not found in environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Voice mode not configured (missing API key).",
        )

    # Get configuration from app state
    config = getattr(request.app.state, "config", {})
    # Use nested get to safely retrieve model
    voice_config = config.get("voice_mode", {})
    # Default model if not specified
    model = voice_config.get("model", "gemini-2.5-flash-native-audio-preview-09-2025")

    return GoogleGeminiLiveClient(api_key=api_key, model=model)
