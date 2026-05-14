"""App authentication endpoints for iOS native auth flow.

Implements a PKCE-style authorization code exchange so the iOS app can
obtain API tokens without the user copy-pasting secrets.

Endpoints:
- GET /app-auth: Initiates OIDC login with PKCE challenge in session
- GET /app-auth-callback: OIDC callback that issues a short-lived auth code
- POST /api/auth/exchange: Exchanges auth code + PKCE verifier for tokens
- POST /api/auth/refresh: Exchanges a refresh token for a new API token
- POST /api/auth/token-session: Exchanges a Bearer token for a session cookie
- GET /.well-known/apple-app-site-association: AASA file for Universal Links
"""

import hashlib
import logging
import os
import secrets
import time
from base64 import urlsafe_b64decode
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import update as sa_update

from family_assistant.services.user_identity import UserIdentityResolutionError
from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import (
    get_current_user,
    get_db,
    get_user_identity_resolver,
)
from family_assistant.web.models import (
    CodeExchangeRequest,
    CodeExchangeResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    TokenSessionResponse,
)

logger = logging.getLogger(__name__)

# --- In-memory auth code store ---
# Maps auth_code -> {code_challenge, code_challenge_method, user_info, created_at}
# These are single-use and expire after 60 seconds.
# ast-grep-ignore: no-dict-any - Auth code entries have mixed-type fields (str, float, dict)
auth_codes: dict[str, dict[str, Any]] = {}

AUTH_CODE_TTL_SECONDS = 60
API_TOKEN_EXPIRY_DAYS = 30
REFRESH_TOKEN_EXPIRY_DAYS = 90
API_TOKEN_EXPIRY_SECONDS = API_TOKEN_EXPIRY_DAYS * 86400

# --- Routers ---
# Two routers: one for page-level endpoints, one for API endpoints
page_router = APIRouter()
api_auth_router = APIRouter(prefix="/auth", tags=["App Auth API"])
wellknown_router = APIRouter()


def cleanup_expired_codes() -> None:
    """Remove expired auth codes from the in-memory store."""
    now = time.monotonic()
    expired = [
        code
        for code, data in auth_codes.items()
        if now - data["created_at"] > AUTH_CODE_TTL_SECONDS
    ]
    for code in expired:
        del auth_codes[code]


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify that SHA256(code_verifier) == code_challenge (base64url-encoded)."""
    if method != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    try:
        # code_challenge is base64url-encoded (may or may not have padding)
        padded = code_challenge + "=" * ((4 - len(code_challenge) % 4) % 4)
        decoded_challenge = urlsafe_b64decode(padded)
    except Exception:
        return False
    return secrets.compare_digest(digest, decoded_challenge)


# --- Page endpoints (mounted at root level) ---


@page_router.get("/app-auth", name="app_auth_start")
async def app_auth_start(
    request: Request,
    code_challenge: str,
    code_challenge_method: str = "S256",
) -> Response:
    """Initiate OIDC login flow with PKCE parameters stored in session.

    The iOS app opens this URL in ASWebAuthenticationSession.
    After OIDC completes, the callback generates an auth code.
    """
    if code_challenge_method != "S256":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only S256 code_challenge_method is supported.",
        )

    # Store PKCE params in session for the callback
    request.session["app_auth_code_challenge"] = code_challenge
    request.session["app_auth_code_challenge_method"] = code_challenge_method

    # Redirect to OIDC login, with callback going to /app-auth-callback
    auth_service = getattr(request.app.state, "auth_service", None)
    if not auth_service or not auth_service.oauth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC authentication not configured.",
        )

    redirect_uri = request.url_for("app_auth_oidc_callback")
    if (
        request.headers.get("x-forwarded-proto") == "https"
        or request.url.scheme == "https"
    ):
        redirect_uri = redirect_uri.replace(scheme="https")

    return await auth_service.oauth.oidc_provider.authorize_redirect(
        request, redirect_uri
    )


@page_router.get("/app-auth-callback", name="app_auth_oidc_callback")
async def app_auth_oidc_callback(request: Request) -> HTMLResponse:
    """OIDC callback for the app auth flow.

    After successful OIDC auth, generates a short-lived authorization code
    and renders a page that redirects to the Universal Link callback.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    if not auth_service or not auth_service.oauth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC authentication not configured.",
        )

    # Complete OIDC flow
    try:
        token = await auth_service.oauth.oidc_provider.authorize_access_token(request)
        user_info = token.get("userinfo")
    except Exception as e:
        logger.error("OIDC callback failed during app auth: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {e}",
        ) from e

    if not user_info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No user info returned from OIDC provider.",
        )

    # Enforce email allowlist (same check as the standard web login flow)
    from family_assistant.web.auth import (  # noqa: PLC0415 - deferred to avoid circular import at module level
        ALLOWED_OIDC_EMAILS,
    )

    if ALLOWED_OIDC_EMAILS:
        email = user_info.get("email")
        if not email:
            logger.warning(
                "App auth OIDC login attempt without email in userinfo (sub: %s)",
                user_info.get("sub"),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authentication failed: No email provided by OIDC provider and email allowlist is enabled.",
            )

        allowed_emails = [e.strip().lower() for e in ALLOWED_OIDC_EMAILS.split(",")]
        if email.lower() not in allowed_emails:
            logger.warning(
                "Unauthorized app auth OIDC login attempt for email: %s", email
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Email '{email}' is not in the allowlist.",
            )

    # Retrieve PKCE challenge from session
    code_challenge = request.session.pop("app_auth_code_challenge", None)
    code_challenge_method = request.session.pop(
        "app_auth_code_challenge_method", "S256"
    )

    if not code_challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing PKCE code_challenge in session. Start the flow from /app-auth.",
        )

    # Generate a single-use authorization code
    cleanup_expired_codes()
    auth_code = secrets.token_urlsafe(32)
    auth_codes[auth_code] = {
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "user_info": dict(user_info),
        "created_at": time.monotonic(),
    }

    # TEMPORARY: Use custom URL scheme for local iOS testing instead of
    # the Universal Link (/.well-known/app-auth-callback) which requires a
    # configured AASA file and HTTPS-served app.
    redirect_url = f"familyassistant://callback?code={auth_code}"

    # Render a simple page that redirects to the Universal Link
    html = f"""<!DOCTYPE html>
<html>
<head><meta http-equiv="refresh" content="0;url={redirect_url}"></head>
<body><p>Redirecting to app...</p><a href="{redirect_url}">Tap here if not redirected</a></body>
</html>"""
    return HTMLResponse(content=html)


# --- API endpoints (mounted under /api/auth) ---


@api_auth_router.post("/exchange")
async def exchange_code(
    request: Request,
    payload: CodeExchangeRequest,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> CodeExchangeResponse:
    """Exchange an authorization code + PKCE verifier for API and refresh tokens."""
    cleanup_expired_codes()

    code_data = auth_codes.pop(payload.code, None)
    if not code_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired authorization code.",
        )

    # Check TTL
    if time.monotonic() - code_data["created_at"] > AUTH_CODE_TTL_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Authorization code has expired.",
        )

    # Verify PKCE
    if not _verify_pkce(
        payload.code_verifier,
        code_data["code_challenge"],
        code_data["code_challenge_method"],
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PKCE verification failed: code_verifier does not match code_challenge.",
        )

    user_info = code_data["user_info"]
    resolver = get_user_identity_resolver(request)
    try:
        user_identifier = (
            resolver.resolve_oidc_user(user_info).user_id
            if resolver is not None
            else user_info.get("sub", user_info.get("email", "unknown"))
        )
    except UserIdentityResolutionError as exc:
        logger.warning("App auth exchange rejected unmapped OIDC user: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    # Create API token (30-day expiry)
    api_token_expires = datetime.now(UTC) + timedelta(days=API_TOKEN_EXPIRY_DAYS)
    (
        full_api_token,
        api_token_id,
        _,
    ) = await api_tokens_storage.create_and_store_api_token(
        db_context=db_context,
        user_identifier=user_identifier,
        name="iOS App",
        expires_at=api_token_expires,
        token_type="api",
    )

    # Create refresh token (90-day expiry), linked to API token
    refresh_expires = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRY_DAYS)
    full_refresh_token, _, _ = await api_tokens_storage.create_and_store_api_token(
        db_context=db_context,
        user_identifier=user_identifier,
        name="iOS App (refresh)",
        expires_at=refresh_expires,
        token_type="refresh",
        parent_token_id=api_token_id,
    )

    logger.info(
        "App auth exchange: issued API token %s and refresh token for user %s",
        api_token_id,
        user_identifier,
    )

    return CodeExchangeResponse(
        api_token=full_api_token,
        refresh_token=full_refresh_token,
        expires_in=API_TOKEN_EXPIRY_SECONDS,
    )


@api_auth_router.post("/refresh")
async def refresh_token(
    payload: RefreshTokenRequest,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> RefreshTokenResponse:
    """Exchange a valid refresh token for a new API token."""
    token_row = await api_tokens_storage.validate_token_by_value(
        db_context, payload.refresh_token, expected_type="refresh"
    )
    if not token_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )

    user_identifier = token_row["user_identifier"]

    # Create a new API token
    api_token_expires = datetime.now(UTC) + timedelta(days=API_TOKEN_EXPIRY_DAYS)
    (
        full_api_token,
        api_token_id,
        _,
    ) = await api_tokens_storage.create_and_store_api_token(
        db_context=db_context,
        user_identifier=user_identifier,
        name="iOS App",
        expires_at=api_token_expires,
        token_type="api",
    )

    # Re-link the refresh token to the new API token so cascade revocation works
    await db_context.execute_with_retry(
        sa_update(api_tokens_table)
        .where(api_tokens_table.c.id == token_row["id"])
        .values(parent_token_id=api_token_id)
    )

    logger.info(
        "Token refresh: issued new API token %s for user %s",
        api_token_id,
        user_identifier,
    )

    return RefreshTokenResponse(
        api_token=full_api_token,
        expires_in=API_TOKEN_EXPIRY_SECONDS,
    )


@api_auth_router.post("/token-session")
async def token_session(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
) -> TokenSessionResponse:
    """Exchange a valid API Bearer token for a session cookie.

    The iOS app calls this on each launch so the WKWebView can use
    session cookies instead of injecting auth headers.
    """
    request.session["user"] = {
        "sub": current_user.get("sub", current_user.get("user_identifier")),
        "name": current_user.get("name", current_user.get("user_identifier")),
        "email": current_user.get("email", current_user.get("user_identifier")),
        "source": "app_token_session",
    }
    # Store the token ID so session validity is tied to token validity
    token_id = current_user.get("token_id")
    if token_id:
        request.session["api_token_id"] = token_id

    logger.info(
        "Token-session: established session for user %s (token_id=%s)",
        current_user.get("sub", current_user.get("user_identifier")),
        token_id,
    )

    return TokenSessionResponse(ok=True)


@api_auth_router.get("/me")
async def auth_me(
    current_user: Annotated[dict[str, object], Depends(get_current_user)],
) -> dict[str, object | None]:
    """Return the authenticated application user for auth-oriented clients."""
    return {
        "user_identifier": current_user.get("user_identifier"),
        "raw_user_identifier": current_user.get("raw_user_identifier"),
        "identity_source": current_user.get("identity_source"),
        "identity_source_identifier": current_user.get("identity_source_identifier"),
        "email": current_user.get("email"),
        "sub": current_user.get("sub"),
        "source": current_user.get("source"),
    }


# --- Well-known endpoint ---


@wellknown_router.get("/.well-known/apple-app-site-association")
async def apple_app_site_association() -> JSONResponse:
    """Serve the Apple App Site Association file for Universal Links.

    The iOS app uses this to claim the /.well-known/app-auth-callback path.
    Team ID and bundle ID are configured via environment variables.
    """
    team_id = os.environ.get("APPLE_TEAM_ID", "XXXXXXXXXX")
    bundle_id = os.environ.get("APPLE_BUNDLE_ID", "com.example.FamilyAssistant")
    app_id = f"{team_id}.{bundle_id}"

    return JSONResponse(
        content={
            "applinks": {
                "apps": [],
                "details": [
                    {
                        "appID": app_id,
                        "paths": ["/.well-known/app-auth-callback*"],
                    }
                ],
            },
        },
        headers={"Content-Type": "application/json"},
    )
