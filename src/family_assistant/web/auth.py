import logging
import re
from datetime import datetime, timezone
from typing import Any

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select, update
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

oauth: OAuth | None = None  # Initialize oauth as None

if AUTH_ENABLED:
    logger.info("OIDC Authentication is ENABLED in auth.py.")
    oauth = OAuth(config)  # type: ignore
    oauth.register(
        name="oidc_provider",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=OIDC_DISCOVERY_URL,
        client_kwargs={
            "scope": "openid email profile",
        },
    )
else:
    logger.info("OIDC Authentication is DISABLED in auth.py.")


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
User = dict[str, Any]  # User information stored in session is a dictionary


async def get_current_user_optional(request: Request) -> User | None:
    """FastAPI dependency to get the current user from session, if any."""
    return request.session.get("user")


async def get_user_from_api_token(
    auth_header: str,
    request: Request,  # pylint: disable=unused-argument
) -> dict | None:
    """
    Verifies an API token and returns user information if valid.
    Updates the token's last_used_at timestamp.
    """
    if not auth_header.startswith("Bearer "):
        return None

    token_value = auth_header.split(" ", 1)[1]

    # Assuming the prefix is the first 8 characters of the token_value
    # and the rest is the secret part that was hashed.
    if len(token_value) <= 8:
        logger.warning("API token value is too short to contain a prefix and secret.")
        return None

    token_prefix = token_value[:8]
    token_secret_part = token_value[8:]

    async with get_db_context() as db:
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

        now = datetime.now(timezone.utc)
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


# Define AuthMiddleware class
class AuthMiddleware:
    def __init__(
        self, app: ASGIApp, public_paths: list[re.Pattern], auth_enabled: bool
    ) -> None:
        self.app = app
        self.public_paths = public_paths
        self.auth_enabled = auth_enabled
        logger.info(f"AuthMiddleware initialized (auth_enabled={self.auth_enabled})")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.auth_enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        for pattern in self.public_paths:
            if pattern.match(request.url.path):
                await self.app(scope, receive, send)
                return

        user = request.session.get("user")

        # Attempt API token authentication if no session user
        if not user:
            auth_header = request.headers.get("Authorization")
            if auth_header:
                api_user = await get_user_from_api_token(auth_header, request)
                if api_user:
                    request.session["user"] = api_user
                    user = api_user  # Update user for the current request flow
                    logger.debug(
                        f"User authenticated via API token for path {request.url.path}"
                    )

        if not user:
            # Store intended URL before redirecting to login (for OIDC flow)
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


auth_router = APIRouter()

if AUTH_ENABLED and oauth:

    @auth_router.get("/login", name="login")  # Add name for url_for
    async def login(request: Request) -> RedirectResponse:
        """Redirects the user to the OIDC provider for authentication."""
        redirect_uri = request.url_for("auth_callback")  # Use new name for callback
        if (
            request.headers.get("x-forwarded-proto") == "https"
            or request.url.scheme == "https"
        ):
            redirect_uri = redirect_uri.replace(scheme="https")

        logger.debug(
            f"Initiating login redirect to OIDC provider. Callback URL: {redirect_uri}"
        )
        return await oauth.oidc_provider.authorize_redirect(request, redirect_uri)  # type: ignore

    @auth_router.get("/auth", name="auth_callback")  # Callback URL, named for url_for
    async def auth_callback(request: Request) -> RedirectResponse:
        """Handles the callback from the OIDC provider after authentication."""
        try:
            token = await oauth.oidc_provider.authorize_access_token(request)  # type: ignore
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

    @auth_router.get("/logout", name="logout")
    async def logout(request: Request) -> RedirectResponse:
        """Clears the user session."""
        request.session.pop("user", None)
        logger.info("User logged out.")
        return RedirectResponse(url="/")
