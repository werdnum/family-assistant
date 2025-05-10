import logging
import re

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from starlette.config import Config
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

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

oauth: OAuth | None = None # Initialize oauth as None

if AUTH_ENABLED:
    logger.info("OIDC Authentication is ENABLED in auth.py.")
    oauth = OAuth(config) # type: ignore
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
        if not user:
            request.session["redirect_after_login"] = str(request.url)
            logger.debug(
                f"No user session for protected path {request.url.path}, redirecting to login."
            )
            redirect_response = RedirectResponse(
                url=request.url_for("login"), # Relies on auth_router being named 'login'
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )
            await redirect_response(scope, receive, send)
            return

        await self.app(scope, receive, send)


auth_router = APIRouter()

if AUTH_ENABLED and oauth:
    @auth_router.get("/login", name="login") # Add name for url_for
    async def login(request: Request) -> RedirectResponse:
        """Redirects the user to the OIDC provider for authentication."""
        redirect_uri = request.url_for("auth_callback") # Use new name for callback
        if (
            request.headers.get("x-forwarded-proto") == "https"
            or request.url.scheme == "https"
        ):
            redirect_uri = redirect_uri.replace(scheme="https")

        logger.debug(
            f"Initiating login redirect to OIDC provider. Callback URL: {redirect_uri}"
        )
        return await oauth.oidc_provider.authorize_redirect(request, redirect_uri) # type: ignore

    @auth_router.get("/auth", name="auth_callback")  # Callback URL, named for url_for
    async def auth_callback(request: Request) -> RedirectResponse:
        """Handles the callback from the OIDC provider after authentication."""
        try:
            token = await oauth.oidc_provider.authorize_access_token(request) # type: ignore
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
