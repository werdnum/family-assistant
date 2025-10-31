import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.config import Config
from starlette.types import ASGIApp, Receive, Scope, Send

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.context import get_db_context

logger = logging.getLogger(__name__)

# --- CryptContext for hashing API tokens ---
# We will use bcrypt for hashing. The actual token generation (creating the hash)
# will be handled elsewhere (e.g., a UI or CLI tool). Here we only verify.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- Auth Configuration ---
# Load config from environment variables
config = Config()

# OIDC configuration
OIDC_CLIENT_ID = config("OIDC_CLIENT_ID", default=None)
OIDC_CLIENT_SECRET = config("OIDC_CLIENT_SECRET", default=None)
OIDC_DISCOVERY_URL = config("OIDC_DISCOVERY_URL", default=None)
SESSION_SECRET_KEY = config("SESSION_SECRET_KEY", default=None)

# Check if OIDC is configured
AUTH_ENABLED = bool(
    OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_DISCOVERY_URL and SESSION_SECRET_KEY
)

# Define paths that should be publicly accessible (no login required)
PUBLIC_PATHS = [
    re.compile(r"^/login$"),
    re.compile(r"^/logout$"),
    re.compile(r"^/auth$"),
    re.compile(r"^/webhook(/.*)?$"),
    re.compile(r"^/api(/.*)?$"),
    re.compile(r"^/health$"),
    re.compile(r"^/static(/.*)?$"),
    re.compile(r"^/favicon.ico$"),
]


# --- User type and dependency ---
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
User = dict[str, Any]  # User information stored in session is a dictionary


class AuthService:
    """Service class for authentication operations with proper dependency injection."""

    def __init__(self, database_engine: AsyncEngine | None = None) -> None:
        """
        Initialize the AuthService with dependencies.

        Args:
            database_engine: The database engine for database operations
        """
        self.database_engine = database_engine
        self.auth_enabled = AUTH_ENABLED
        self.oauth: OAuth | None = None

        if AUTH_ENABLED:
            logger.info("OIDC Authentication is ENABLED in AuthService.")
            try:
                logger.info(
                    f"Initializing OAuth with authlib (client_id={OIDC_CLIENT_ID}, discovery_url={OIDC_DISCOVERY_URL})"
                )
                self.oauth = OAuth(config)  # type: ignore
                self.oauth.register(
                    name="oidc_provider",
                    client_id=OIDC_CLIENT_ID,
                    client_secret=OIDC_CLIENT_SECRET,
                    server_metadata_url=OIDC_DISCOVERY_URL,
                    client_kwargs={
                        "scope": "openid email profile",
                    },
                )
                logger.info(
                    "OAuth successfully initialized and OIDC provider registered"
                )
            except Exception as e:
                logger.error(f"Failed to initialize OAuth: {e}", exc_info=True)
                self.oauth = None
                # Don't raise here - let create_auth_router handle it
                # This allows the app to start but with proper error logging
        else:
            logger.info("OIDC Authentication is DISABLED in AuthService.")

    async def get_current_user_optional(self, request: Request) -> User | None:
        """FastAPI dependency to get the current user from session, if any."""
        try:
            return request.session.get("user")
        except AssertionError:
            # Session middleware not installed
            return None

    def get_user_from_request(self, request: Request) -> User | None:
        """
        Safely get user from request, handling cases where SessionMiddleware is not installed.
        This is a synchronous helper for use in template contexts.
        """
        try:
            return request.session.get("user")
        except AssertionError:
            # Session middleware not installed
            return None

    async def get_user_from_api_token(
        self,
        auth_header: str,
        request: Request,  # pylint: disable=unused-argument
    ) -> dict | None:
        """
        Verifies an API token and returns user information if valid.
        Updates the token's last_used_at timestamp.
        """
        if not auth_header.startswith("Bearer "):
            return None

        if not self.database_engine:
            logger.error("Database engine not available in AuthService")
            return None

        token_value = auth_header.split(" ", 1)[1]

        # Assuming the prefix is the first 8 characters of the token_value
        # and the rest is the secret part that was hashed.
        if len(token_value) <= 8:
            logger.warning(
                "API token value is too short to contain a prefix and secret."
            )
            return None

        token_prefix = token_value[:8]
        token_secret_part = token_value[8:]

        async with get_db_context(self.database_engine) as db:
            query = select(api_tokens_table).where(
                api_tokens_table.c.prefix == token_prefix
            )
            token_row = await db.fetch_one(query)

            if not token_row:
                logger.debug(f"API token with prefix {token_prefix} not found.")
                return None

            if not pwd_context.verify(token_secret_part, token_row["hashed_token"]):
                logger.warning(
                    f"Invalid API token provided for prefix {token_prefix}."
                )  # Potentially log user_identifier if available and safe
                return None

            if token_row["is_revoked"]:
                logger.warning(
                    f"Attempt to use revoked API token (ID: {token_row['id']}, User: {token_row['user_identifier']})."
                )
                return None

            now = datetime.now(UTC)
            if token_row["expires_at"] and token_row["expires_at"] < now:
                logger.warning(
                    f"Attempt to use expired API token (ID: {token_row['id']}, User: {token_row['user_identifier']})."
                )
                return None

            # Update last_used_at
            update_query = (
                update(api_tokens_table)
                .where(api_tokens_table.c.id == token_row["id"])
                .values(last_used_at=now)
            )
            await db.execute_with_retry(update_query)
            # No need to commit explicitly if get_db_context handles transaction lifecycle

            logger.info(
                f"API token authenticated for user: {token_row['user_identifier']} (Token ID: {token_row['id']})"
            )
            # Mimic OIDC userinfo structure for session consistency
            return {
                "sub": token_row["user_identifier"],
                "name": token_row["user_identifier"],  # Or a display name if available
                "email": token_row["user_identifier"],  # Or actual email if available
                "source": "api_token",
                "token_id": token_row["id"],
            }

    async def handle_login(self, request: Request) -> RedirectResponse:
        """Redirects the user to the OIDC provider for authentication."""
        if not self.oauth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC authentication not configured",
            )

        redirect_uri = request.url_for("auth_callback")  # Use new name for callback
        if (
            request.headers.get("x-forwarded-proto") == "https"
            or request.url.scheme == "https"
        ):
            redirect_uri = redirect_uri.replace(scheme="https")

        logger.debug(
            f"Initiating login redirect to OIDC provider. Callback URL: {redirect_uri}"
        )
        return await self.oauth.oidc_provider.authorize_redirect(request, redirect_uri)  # type: ignore

    async def handle_auth_callback(self, request: Request) -> RedirectResponse:
        """Handles the callback from the OIDC provider after authentication."""
        if not self.oauth:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OIDC authentication not configured",
            )

        try:
            token = await self.oauth.oidc_provider.authorize_access_token(request)  # type: ignore
            user_info = token.get("userinfo")
            if user_info:
                request.session["user"] = dict(user_info)
                logger.info(
                    f"User logged in successfully: {user_info.get('email') or user_info.get('sub')}"
                )
                redirect_url = request.session.pop("redirect_after_login", "/")
                return RedirectResponse(url=redirect_url)
            else:
                logger.warning(
                    "OIDC callback successful but no userinfo found in token."
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not fetch user information.",
                )
        except Exception as e:
            logger.error(
                f"Error during OIDC authentication callback: {e}", exc_info=True
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication failed: {e}",
            ) from e

    async def handle_logout(self, request: Request) -> RedirectResponse:
        """Clears the user session."""
        request.session.pop("user", None)
        logger.info("User logged out.")
        return RedirectResponse(url="/")


# Define AuthMiddleware class
class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        auth_service: AuthService,
        public_paths: list[re.Pattern] | None = None,
    ) -> None:
        self.app = app
        self.auth_service = auth_service
        self.public_paths = public_paths or PUBLIC_PATHS
        logger.info(
            f"AuthMiddleware initialized (auth_enabled={self.auth_service.auth_enabled})"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.auth_service.auth_enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        for pattern in self.public_paths:
            if pattern.match(request.url.path):
                await self.app(scope, receive, send)
                return

        # Try to get user from session
        try:
            user = request.session.get("user")
        except AssertionError:
            # Session middleware not available, so no authentication is possible
            await self.app(scope, receive, send)
            return

        # Attempt API token authentication if no session user
        if not user:
            auth_header = request.headers.get("Authorization")
            if auth_header:
                api_user = await self.auth_service.get_user_from_api_token(
                    auth_header, request
                )
                if api_user:
                    # Session middleware might not be available, can't store user in session
                    with contextlib.suppress(AssertionError):
                        request.session["user"] = api_user
                    user = api_user  # Update user for the current request flow
                    logger.debug(
                        f"User authenticated via API token for path {request.url.path}"
                    )

        if not user:
            # Store intended URL before redirecting to login (for OIDC flow)
            # Session middleware might not be available
            with contextlib.suppress(AssertionError):
                request.session["redirect_after_login"] = str(request.url)
            logger.debug(
                f"No user session or valid API token for protected path {request.url.path}, redirecting to OIDC login."
            )
            redirect_response = RedirectResponse(
                url=request.url_for("login"),
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )
            await redirect_response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# Create auth router
def create_auth_router(auth_service: AuthService) -> APIRouter:
    """Create the auth router with proper dependency injection."""
    auth_router = APIRouter()

    if auth_service.auth_enabled:
        if not auth_service.oauth:
            # OAuth initialization failed but auth is enabled
            logger.error(
                "AUTH_ENABLED is True but OAuth is not initialized. "
                "Creating fallback error routes for /login, /auth, /logout"
            )

            # Add fallback routes that show clear error messages
            @auth_router.get("/login", name="login")
            def login_error(request: Request) -> NoReturn:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication system initialization failed. "
                    "OAuth client could not be configured. "
                    "Please check server logs for details.",
                )

            @auth_router.get("/auth", name="auth_callback")
            def auth_error(request: Request) -> NoReturn:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Authentication callback cannot function - OAuth not initialized.",
                )

            @auth_router.get("/logout", name="logout")
            async def logout_error(request: Request) -> RedirectResponse:
                # Allow logout to work even if OAuth is broken - just clear session
                request.session.pop("user", None)
                logger.info(
                    "User logged out (OAuth not initialized, session cleared only)"
                )
                return RedirectResponse(url="/")

            logger.warning(
                "Auth router created with ERROR routes due to OAuth initialization failure"
            )
        else:
            # Normal case - OAuth is properly initialized
            @auth_router.get("/login", name="login")  # Add name for url_for
            async def login(request: Request) -> RedirectResponse:
                """Redirects the user to the OIDC provider for authentication."""
                return await auth_service.handle_login(request)

            @auth_router.get(
                "/auth", name="auth_callback"
            )  # Callback URL, named for url_for
            async def auth_callback(request: Request) -> RedirectResponse:
                """Handles the callback from the OIDC provider after authentication."""
                return await auth_service.handle_auth_callback(request)

            @auth_router.get("/logout", name="logout")
            async def logout(request: Request) -> RedirectResponse:
                """Clears the user session."""
                return await auth_service.handle_logout(request)

            logger.info(
                "Auth router created with /login, /auth, and /logout routes (OAuth initialized successfully)"
            )
    else:
        logger.info("Auth router created empty (AUTH_ENABLED=False)")

    return auth_router


# Backward compatibility exports for easier migration
def get_current_user_optional(request: Request) -> User | None:
    """Legacy function for backward compatibility."""
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service:
        return auth_service.get_user_from_request(request)
    return None


def get_user_from_request(request: Request) -> User | None:
    """Legacy function for backward compatibility."""
    auth_service = getattr(request.app.state, "auth_service", None)
    if auth_service:
        return auth_service.get_user_from_request(request)
    return None
