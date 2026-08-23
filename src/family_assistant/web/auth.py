import contextlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, NoReturn

from authlib.integrations.starlette_client import OAuth  # type: ignore
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from passlib.context import CryptContext
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.config import Config
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from family_assistant.services.user_identity import (
    UserIdentityResolutionError,
    UserIdentityResolver,
)
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web import jwt_tokens, route_auth

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
ALLOWED_OIDC_EMAILS = config("ALLOWED_OIDC_EMAILS", default=None)

# Check if OIDC is configured
AUTH_ENABLED = bool(
    OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_DISCOVERY_URL and SESSION_SECRET_KEY
)

# Define paths that should be publicly accessible (no login required)
PUBLIC_PATHS = [
    re.compile(r"^/login$"),
    re.compile(r"^/logout$"),
    re.compile(r"^/auth$"),
    re.compile(r"^/app-auth$"),
    re.compile(r"^/app-auth-callback$"),
    re.compile(r"^/webhook(/.*)?$"),
    # /api is NOT blanket-public: AuthMiddleware classifies every /api request
    # via route_auth and enforces authentication by default (fail-closed).
    re.compile(r"^/health$"),
    re.compile(r"^/privacy$"),
    re.compile(r"^/static(/.*)?$"),
    re.compile(r"^/favicon.ico$"),
    re.compile(r"^/\.well-known(/.*)?$"),
    re.compile(r"^/manifest\.webmanifest$"),
    re.compile(r"^/sw\.js$"),
]


# --- User type and dependency ---
# ast-grep-ignore: no-dict-any - OIDC session data has provider-specific fields that vary by identity provider
User = dict[str, Any]


def extract_api_credential(request: Request) -> str | None:
    """Return the bearer/API token from the request headers, if present.

    Shared by AuthMiddleware and the request dependencies so both accept
    exactly the same credential headers.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1]
    return request.headers.get("X-API-Token")


_AUTHENTICATED_API_USER_STATE_KEY = "family_assistant_authenticated_api_user"


def set_request_authenticated_api_user(request: Request, user: User) -> None:
    """Cache middleware-authenticated API identity for downstream dependencies."""
    request.scope.setdefault("state", {})[_AUTHENTICATED_API_USER_STATE_KEY] = user


def get_request_authenticated_api_user(request: Request) -> User | None:
    """Return the API identity already authenticated for this request, if any."""
    state = request.scope.get("state")
    if not state:
        return None
    user = state.get(_AUTHENTICATED_API_USER_STATE_KEY)
    return user if isinstance(user, dict) else None


def api_authentication_enabled(auth_service: object) -> bool:
    """Whether API requests must authenticate through OIDC or signed JWTs."""
    jwt_service = getattr(auth_service, "jwt_tokens", None)
    return bool(
        getattr(auth_service, "auth_enabled", False)
        or (jwt_service and getattr(jwt_service, "enabled", False))
    )


def _clear_token_session_binding(request: Request) -> None:
    """Remove credential bindings when a session changes authentication source."""
    request.session.pop("api_token_id", None)
    request.session.pop("session_jwt_exp", None)


class AuthService:
    """Service class for authentication operations with proper dependency injection."""

    def __init__(
        self,
        database_engine: AsyncEngine | None = None,
        jwt_token_service: jwt_tokens.JWTTokenService | None = None,
    ) -> None:
        """
        Initialize the AuthService with dependencies.

        Args:
            database_engine: The database engine for database operations
        """
        self.database_engine = database_engine
        self.jwt_tokens = (
            jwt_token_service or jwt_tokens.JWTTokenService.from_environment()
        )
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
                logger.exception(f"Failed to initialize OAuth: {e}")
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

        if self.jwt_tokens.enabled and jwt_tokens.looks_like_jwt(token_value):
            return await self._user_from_jwt_token(token_value)

        # Assuming the prefix is the first 8 characters of the token_value
        # and the rest is the secret part that was hashed.
        if len(token_value) <= 8:
            logger.warning(
                "API token value is too short to contain a prefix and secret."
            )
            return None

        token_prefix = token_value[:8]
        token_secret_part = token_value[8:]

        db = Database(self.database_engine)
        query = select(api_tokens_table).where(
            api_tokens_table.c.prefix == token_prefix,
            api_tokens_table.c.token_type == "api",
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
        row_expires = token_row["expires_at"]
        if row_expires and row_expires.tzinfo is None:
            # SQLite returns naive datetimes even for DateTime(timezone=True).
            row_expires = row_expires.replace(tzinfo=UTC)
        if row_expires and row_expires < now:
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
        await db.execute(update_query)
        # No need to commit explicitly if Database handles transaction lifecycle

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

    async def _user_from_jwt_token(self, token_value: str) -> dict | None:
        """Authenticate a signed access-token JWT against its api_tokens row.

        The signature/expiry/issuer/audience are verified statelessly; the row
        identified by the ``tid`` claim is still consulted so revocation and
        manual expiry take effect immediately at this layer.
        """
        if not self.database_engine:
            logger.error("Database engine not available in AuthService")
            return None

        claims = self.jwt_tokens.verify_access_token(token_value)
        if not claims:
            logger.warning("Rejected invalid JWT access token.")
            return None

        db = Database(self.database_engine)
        query = select(api_tokens_table).where(
            api_tokens_table.c.id == claims["tid"],
            api_tokens_table.c.token_type.in_(("api", "browser")),
        )
        token_row = await db.fetch_one(query)

        if not token_row or token_row["is_revoked"]:
            logger.warning("JWT references missing or revoked API token.")
            return None

        now = datetime.now(UTC)
        row_expires = token_row["expires_at"]
        if row_expires and row_expires.tzinfo is None:
            # SQLite returns naive datetimes even for DateTime(timezone=True).
            row_expires = row_expires.replace(tzinfo=UTC)
        if row_expires and row_expires < now:
            logger.warning(
                "JWT references expired API token (ID: %s).", token_row["id"]
            )
            return None

        await db.execute(
            update(api_tokens_table)
            .where(api_tokens_table.c.id == token_row["id"])
            .values(last_used_at=now)
        )

        user_identifier = str(claims["sub"])
        return {
            "sub": user_identifier,
            "name": user_identifier,
            "email": user_identifier,
            "source": "jwt_access_token",
            "token_id": token_row["id"],
            # Carried so session-minting bridges can bind the session to the
            # JWT's own expiry instead of the backing row's lifetime.
            "exp": int(claims["exp"]),
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
                # Check email allowlist if configured
                if ALLOWED_OIDC_EMAILS:
                    email = user_info.get("email")
                    if not email:
                        logger.warning(
                            f"OIDC login attempt without email in userinfo (sub: {user_info.get('sub')})"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail="Authentication failed: No email provided by OIDC provider and email allowlist is enabled.",
                        )

                    allowed_emails = [
                        e.strip().lower() for e in ALLOWED_OIDC_EMAILS.split(",")
                    ]
                    if email.lower() not in allowed_emails:
                        logger.warning(
                            f"Unauthorized OIDC login attempt for email: {email}"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Access denied: Email '{email}' is not in the allowlist.",
                        )

                config_from_state = getattr(request.app.state, "config", None)
                if config_from_state is not None:
                    resolver = getattr(
                        request.app.state, "user_identity_resolver", None
                    )
                    if resolver is None:
                        resolver = UserIdentityResolver(config_from_state)
                        request.app.state.user_identity_resolver = resolver
                    try:
                        resolver.resolve_oidc_user(dict(user_info))
                    except UserIdentityResolutionError as exc:
                        logger.warning("Unmapped OIDC login rejected: %s", exc)
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=str(exc),
                        ) from exc

                _clear_token_session_binding(request)
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
        except HTTPException:
            # Re-raise HTTPExceptions as-is (e.g., 403 Forbidden from allowlist)
            raise
        except Exception as e:
            logger.exception(f"Error during OIDC authentication callback: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication failed: {e}",
            ) from e

    async def handle_logout(self, request: Request) -> RedirectResponse:
        """Clears the user session."""
        request.session.pop("user", None)
        _clear_token_session_binding(request)
        logger.info("User logged out.")
        return RedirectResponse(url="/")


# ast-grep-ignore: no-dict-any - ASGI receive() messages are untyped dicts by protocol definition
ScopeMessage = dict[str, Any]


# Request-body cap for unauthenticated bootstrap/public API routes.
class BootstrapBodyLimitMiddleware:
    """Cap request bodies on unauthenticated bootstrap/public API routes.

    These routes bypass default authentication, so an oversized body must be
    rejected while streaming — before FastAPI buffers and parses it. The cap
    comes from route_auth's classification, keeping one source of truth for
    which routes are exposed without credentials.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = route_auth.api_route_body_limit(scope["method"], scope["path"])
        if limit is None:
            await self.app(scope, receive, send)
            return

        if route_auth.is_public_error_intake(scope["method"], scope["path"]):
            from family_assistant.web.routers.errors_api import (  # noqa: PLC0415 - router imports auth helpers
                ErrorIntakeRateLimiter,
            )

            app = scope.get("app")
            limiter = getattr(
                getattr(app, "state", None),
                "error_intake_address_admission_limiter",
                None,
            )
            if not isinstance(limiter, ErrorIntakeRateLimiter):
                await JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={
                        "detail": "Error intake admission limiter is unavailable."
                    },
                )(scope, receive, send)
                return
            client = scope.get("client")
            client_address = client[0] if client else "unknown"
            if not limiter.allow(f"address:{client_address}"):
                await JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "Too many error reports."},
                )(scope, receive, send)
                return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = -1
            if declared_length < 0:
                await JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "Invalid Content-Length header."},
                )(scope, receive, send)
                return
            if declared_length > limit:
                await JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body too large."},
                )(scope, receive, send)
                return

        # Buffer with the cap applied so memory stays bounded even when no
        # Content-Length is provided (chunked transfer).
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > limit:
                await JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "Request body too large."},
                )(scope, receive, send)
                return

        replayed = False

        async def replay_receive() -> ScopeMessage:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


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
        # Cache for api_token_id validity checks to avoid DB query per request
        self._token_valid_cache: dict[int, dict[str, bool | float]] = {}
        self.TOKEN_VALID_CACHE_TTL = 30  # seconds
        logger.info(
            "AuthMiddleware initialized (oidc_enabled=%s, api_auth_enabled=%s)",
            self.auth_service.auth_enabled,
            api_authentication_enabled(self.auth_service),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        app = scope.get("app")
        auth_service = getattr(
            getattr(app, "state", None), "auth_service", self.auth_service
        )
        if not api_authentication_enabled(auth_service):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        # Use scope["path"] instead of request.url.path to avoid
        # CVE-2026-48710 Host header poisoning of the parsed URL path.
        request_path: str = scope["path"]

        for pattern in self.public_paths:
            if pattern.match(request_path):
                await self.app(scope, receive, send)
                return

        is_api_request = route_auth.is_api_path(request_path)
        if not is_api_request and not auth_service.auth_enabled:
            # Signed JWTs protect the API surface only. Page authentication and
            # its /login redirect exist only when OIDC is configured.
            await self.app(scope, receive, send)
            return

        # Try to get user from session first: token-bound sessions must be
        # revalidated BEFORE any classification-based early return, so an
        # expired credential cannot ride an exempt route past its checks.
        try:
            user = request.session.get("user")
        except AssertionError:
            # Session middleware not available. API requests stay fail-closed
            # (bearer auth below, then 401); page requests keep the legacy
            # pass-through since the login redirect needs a session too.
            if is_api_request:
                user = None
            else:
                await self.app(scope, receive, send)
                return

        # If session was created from an API token (e.g., iOS app), verify the
        # token is still valid (not revoked/expired) and that any bound short-
        # lived JWT has not expired. Uses a short TTL cache to avoid a DB query
        # on every request.
        if user:
            jwt_exp = request.session.get("session_jwt_exp")
            if jwt_exp is not None and time.time() >= float(jwt_exp):
                logger.warning(
                    "Session invalidated: bound JWT access token has expired."
                )
                with contextlib.suppress(AssertionError):
                    request.session.pop("user", None)
                    request.session.pop("api_token_id", None)
                    request.session.pop("session_jwt_exp", None)
                user = None

        if user:
            api_token_id = request.session.get("api_token_id")
            if api_token_id and auth_service.database_engine:
                now = time.monotonic()
                cache_key = api_token_id
                if len(self._token_valid_cache) > 1000:
                    self._token_valid_cache.clear()
                cached = self._token_valid_cache.get(cache_key)
                if cached and now - cached["checked_at"] < self.TOKEN_VALID_CACHE_TTL:
                    is_valid = cached["valid"]
                else:
                    from family_assistant.storage import (  # noqa: PLC0415 - deferred to avoid circular import at module level
                        api_tokens as api_tokens_storage,
                    )
                    from family_assistant.storage.database import (  # noqa: PLC0415 - deferred to avoid circular import at module level
                        Database,
                    )

                    db = Database(auth_service.database_engine)
                    is_valid = await api_tokens_storage.is_token_valid(db, api_token_id)
                    self._token_valid_cache[cache_key] = {
                        "valid": is_valid,
                        "checked_at": now,
                    }

                if not is_valid:
                    logger.warning(
                        "Session invalidated: API token %s is no longer valid",
                        api_token_id,
                    )
                    self._token_valid_cache.pop(cache_key, None)
                    request.session.pop("user", None)
                    request.session.pop("api_token_id", None)
                    request.session.pop("session_jwt_exp", None)
                    user = None

        # Exempt API routes carry their own route-specific authentication. A
        # token-bound session is still revalidated above before the request can
        # reach that mechanism.
        if is_api_request and not route_auth.api_route_requires_default_auth(
            request.method, request_path
        ):
            await self.app(scope, receive, send)
            return

        # Attempt API token authentication if no session user
        if not user:
            credential = extract_api_credential(request)
            if credential:
                api_user = await auth_service.get_user_from_api_token(
                    f"Bearer {credential}", request
                )
                if api_user:
                    # Session middleware might not be available, can't store user in session.
                    # JWT credentials are deliberately not persisted: the
                    # session would outlive the one-hour token expiry, since
                    # the validity check binds to the long-lived backing row.
                    # Opaque-token sessions are bound via api_token_id below,
                    # so their revocation check still applies.
                    if api_user.get("source") != "jwt_access_token":
                        with contextlib.suppress(AssertionError):
                            request.session["user"] = api_user
                            # Bind the session to the token row so the
                            # validity check above terminates access when the
                            # credential is revoked or expires, not just at
                            # session lifetime.
                            if api_user.get("token_id"):
                                request.session["api_token_id"] = api_user["token_id"]
                    set_request_authenticated_api_user(request, api_user)
                    user = api_user  # Update user for the current request flow
                    logger.debug(
                        f"User authenticated via API token for path {request_path}"
                    )

        if not user:
            if is_api_request:
                logger.debug(
                    "No session or valid API token for API path %s; rejecting with 401.",
                    request_path,
                )
                unauthorized = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "detail": "Not authenticated: session or API token required."
                    },
                    headers={
                        "WWW-Authenticate": 'Bearer realm="api", error="missing_token"'
                    },
                )
                await unauthorized(scope, receive, send)
                return
            # Store intended URL before redirecting to login (for OIDC flow)
            # Session middleware might not be available
            with contextlib.suppress(AssertionError):
                request.session["redirect_after_login"] = str(request.url)
            logger.debug(
                f"No user session or valid API token for protected path {request_path}, redirecting to OIDC login."
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
                _clear_token_session_binding(request)
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
