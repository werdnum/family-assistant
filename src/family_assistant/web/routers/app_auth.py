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
from sqlalchemy import select
from sqlalchemy import update as sa_update

from family_assistant.services.user_identity import UserIdentityResolutionError
from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web import jwt_tokens, route_auth
from family_assistant.web.dependencies import (
    get_current_session_user,
    get_current_user,
    get_db,
    get_user_identity_resolver,
)
from family_assistant.web.models import (
    CodeExchangeRequest,
    CodeExchangeResponse,
    OpaqueTokenExchangeRequest,
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

# HttpOnly cookie carrying the browser's short-lived JWT past the gateway.
# SameSite=Lax so browser-managed flows that return from cross-site redirects
# (OAuth callbacks) keep authenticating; cross-site POSTs never carry it.
BROWSER_TOKEN_COOKIE_NAME = "fa_access_token"

# Name of the internal api_tokens row backing browser-session JWTs. One live
# row per user, reused across bridge calls; never issued as a credential.
BROWSER_SESSION_TOKEN_NAME = "browser-session"

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
        logger.exception("OIDC callback failed during app auth: %s", e)
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

    # When AASA is configured (APPLE_TEAM_ID + APPLE_BUNDLE_ID set), redirect
    # via the Universal Link. Otherwise fall back to a custom URL scheme so
    # local iOS testing works without a deployed AASA file.
    if os.environ.get("APPLE_TEAM_ID") and os.environ.get("APPLE_BUNDLE_ID"):
        scheme = (
            "https"
            if (
                request.headers.get("x-forwarded-proto") == "https"
                or request.url.scheme == "https"
            )
            else request.url.scheme
        )
        redirect_url = str(
            request.url.replace(
                scheme=scheme,
                path="/.well-known/app-auth-callback",
                query=f"code={auth_code}",
            )
        )
    else:
        redirect_url = f"familyassistant://callback?code={auth_code}"

    # Render a simple page that redirects to the Universal Link
    html = f"""<!DOCTYPE html>
<html>
<head><meta http-equiv="refresh" content="0;url={redirect_url}"></head>
<body><p>Redirecting to app...</p><a href="{redirect_url}">Tap here if not redirected</a></body>
</html>"""
    return HTMLResponse(content=html)


def _access_token_response_value(
    api_minted: api_tokens_storage.MintedApiToken,
    api_token_id: int,
    user_identifier: str,
) -> str:
    """Return the client-visible access token for a minted row.

    With JWT signing configured this is a short-lived signed JWT bound to the
    row (which remains the revocation registry); otherwise it is the opaque
    secret itself.
    """
    if jwt_tokens.jwt_auth_enabled():
        return jwt_tokens.mint_access_token(user_identifier, api_token_id)
    return api_minted.full_token


# --- API endpoints (mounted under /api/auth) ---


@api_auth_router.post("/exchange")
async def exchange_code(
    request: Request,
    payload: CodeExchangeRequest,
    db_context: Annotated[Database, Depends(get_db)],
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

    # Minted before the block: hashing is blocking bcrypt, and on SQLite it
    # would hold the engine-wide transaction lock for its whole duration.
    api_minted = api_tokens_storage.mint_api_token()
    refresh_minted = api_tokens_storage.mint_api_token()
    api_token_expires = datetime.now(UTC) + timedelta(days=API_TOKEN_EXPIRY_DAYS)
    refresh_expires = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRY_DAYS)

    async with db_context.transaction() as txn:
        # API-token insert + refresh-token insert must be atomic: a failed second
        # write commits a live credential whose secret was never returned.
        api_token_id = await api_tokens_storage.add_api_token(
            db_context=txn,
            user_identifier=user_identifier,
            name="iOS App",
            hashed_token=api_minted.hashed_secret,
            prefix=api_minted.prefix,
            created_at=api_minted.created_at,
            expires_at=api_token_expires,
            token_type="api",
        )
        await api_tokens_storage.add_api_token(
            db_context=txn,
            user_identifier=user_identifier,
            name="iOS App (refresh)",
            hashed_token=refresh_minted.hashed_secret,
            prefix=refresh_minted.prefix,
            created_at=refresh_minted.created_at,
            expires_at=refresh_expires,
            token_type="refresh",
            parent_token_id=api_token_id,
        )

    access_token = _access_token_response_value(
        api_minted, api_token_id, user_identifier
    )
    full_refresh_token = refresh_minted.full_token
    token_ttl = (
        jwt_tokens.access_token_ttl_seconds()
        if jwt_tokens.jwt_auth_enabled()
        else API_TOKEN_EXPIRY_SECONDS
    )

    logger.info(
        "App auth exchange: issued API token %s and refresh token for user %s",
        api_token_id,
        user_identifier,
    )

    return CodeExchangeResponse(
        api_token=access_token,
        refresh_token=full_refresh_token,
        expires_in=token_ttl,
    )


@api_auth_router.post("/refresh")
async def refresh_token(
    payload: RefreshTokenRequest,
    db_context: Annotated[Database, Depends(get_db)],
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

    # Minted before the block; see the exchange endpoint for why.
    api_minted = api_tokens_storage.mint_api_token()
    api_token_expires = datetime.now(UTC) + timedelta(days=API_TOKEN_EXPIRY_DAYS)

    async with db_context.transaction() as txn:
        # Replacement insert + refresh-token relink must be atomic: a failed second
        # write commits a live credential that was never validated with the original
        # refresh token's identity.
        api_token_id = await api_tokens_storage.add_api_token(
            db_context=txn,
            user_identifier=user_identifier,
            name="iOS App",
            hashed_token=api_minted.hashed_secret,
            prefix=api_minted.prefix,
            created_at=api_minted.created_at,
            expires_at=api_token_expires,
            token_type="api",
        )

        await txn.execute(
            sa_update(api_tokens_table)
            .where(api_tokens_table.c.id == token_row["id"])
            .values(parent_token_id=api_token_id)
        )

    access_token = _access_token_response_value(
        api_minted, api_token_id, user_identifier
    )
    token_ttl = (
        jwt_tokens.access_token_ttl_seconds()
        if jwt_tokens.jwt_auth_enabled()
        else API_TOKEN_EXPIRY_SECONDS
    )

    logger.info(
        "Token refresh: issued new API token %s for user %s",
        api_token_id,
        user_identifier,
    )

    return RefreshTokenResponse(
        api_token=access_token,
        expires_in=token_ttl,
    )


@api_auth_router.post("/token")
async def exchange_opaque_token(
    payload: OpaqueTokenExchangeRequest,
    db_context: Annotated[Database, Depends(get_db)],
) -> RefreshTokenResponse:
    """Upgrade a valid opaque API token to a signed JWT.

    Opaque tokens are rejected by the gateway (it cannot consult the
    database), so remote script clients exchange their opaque token once for a
    short-lived JWT. The opaque token itself stays valid wherever the gateway
    is not in the request path (LAN/Tailscale).
    """
    if not jwt_tokens.jwt_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT auth is not configured on this server.",
        )

    token_row = await api_tokens_storage.validate_token_by_value(
        db_context, payload.token, expected_type="api"
    )
    if not token_row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API token.",
        )

    token = jwt_tokens.mint_access_token(
        str(token_row["user_identifier"]), int(token_row["id"])
    )
    logger.info(
        "Opaque token upgrade: issued JWT for API token %s (user %s)",
        token_row["id"],
        token_row["user_identifier"],
    )
    return RefreshTokenResponse(
        api_token=token,
        expires_in=jwt_tokens.access_token_ttl_seconds(),
    )


@api_auth_router.get("/browser-token")
async def browser_token(
    db_context: Annotated[Database, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_session_user)],
) -> JSONResponse:
    """Exchange the OIDC session for a short-lived browser JWT cookie.

    The JWT is set as an HttpOnly SameSite=Lax cookie scoped to /api so every
    browser-managed API request (fetch, EventSource, img tags) authenticates
    past the gateway without per-request headers. Returns ``{"enabled":
    false}`` when JWT auth is not configured so clients can proceed without
    the cookie (and without logging a console error per page load).
    """
    if not jwt_tokens.jwt_auth_enabled():
        return JSONResponse(content={"enabled": False})
    if current_user.get("readonly"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Scoped tokens cannot mint browser sessions.",
        )

    user_identifier = str(
        current_user.get("sub") or current_user.get("user_identifier")
    )

    # Browser sessions have no api_tokens row of their own, so the bridge binds
    # each JWT to a dedicated short-lived row: it keeps pointing at a real
    # revocation registry entry, and the row expires with (a grace past) the
    # JWT. One live row per user is reused across bridge calls so routine page
    # reloads do not grow the table; the secret is never issued to anyone.
    ttl = jwt_tokens.access_token_ttl_seconds()
    now = datetime.now(UTC)
    reuse_query = (
        select(api_tokens_table.c.id)
        .where(
            api_tokens_table.c.user_identifier == user_identifier,
            api_tokens_table.c.name == BROWSER_SESSION_TOKEN_NAME,
            api_tokens_table.c.token_type == "api",
            api_tokens_table.c.is_revoked == False,  # noqa: E712 - SQL comparison
            api_tokens_table.c.expires_at > now,
        )
        .order_by(api_tokens_table.c.id.desc())
        .limit(1)
    )
    existing = await db_context.fetch_one(reuse_query)
    if existing:
        api_token_id = int(existing["id"])
    else:
        minted = api_tokens_storage.mint_api_token()
        async with db_context.transaction() as txn:
            api_token_id = await api_tokens_storage.add_api_token(
                db_context=txn,
                user_identifier=user_identifier,
                name=BROWSER_SESSION_TOKEN_NAME,
                hashed_token=minted.hashed_secret,
                prefix=minted.prefix,
                created_at=minted.created_at,
                expires_at=now + timedelta(seconds=ttl + 60),
                token_type="api",
            )

    token = jwt_tokens.mint_access_token(user_identifier, api_token_id)
    # The credential travels only via the HttpOnly cookie; the body carries no
    # token material so injected scripts cannot read it out of the response.
    response = JSONResponse(content={"expires_in": ttl})
    response.set_cookie(
        key=BROWSER_TOKEN_COOKIE_NAME,
        value=token,
        max_age=ttl,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api",
    )
    return response


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


@wellknown_router.get("/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    """Publish the verification public key for gateway JWT validation.

    Unauthenticated by design; the key is public. Short cache so key rotation
    propagates promptly while sparing the app per-request cost at the edge.
    """
    if not jwt_tokens.jwt_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="JWT auth is not configured on this server.",
        )
    return JSONResponse(
        content=jwt_tokens.jwks_document(),
        headers={"Cache-Control": "public, max-age=300"},
    )


@wellknown_router.get("/.well-known/auth-route-classification")
async def auth_route_classification() -> JSONResponse:
    """Publish which /api routes are exempt from default/JWT authentication.

    Single source of truth for the edge deployment's JWT-enforcement route
    split; consumed there via generation or contract testing so the two lists
    cannot silently drift.
    """
    return JSONResponse(content=route_auth.api_route_classification())


@wellknown_router.get("/.well-known/apple-app-site-association")
async def apple_app_site_association() -> JSONResponse:
    """Serve the Apple App Site Association file for Universal Links.

    The iOS app uses this to claim the app-auth callback and shared-conversation
    paths. Team ID and bundle ID can be overridden via environment variables.
    """
    team_id = os.environ.get("APPLE_TEAM_ID", "H7NBC2S52X")
    bundle_id = os.environ.get("APPLE_BUNDLE_ID", "dev.andrewgarrett.assistant")
    app_id = f"{team_id}.{bundle_id}"

    return JSONResponse(
        content={
            "applinks": {
                "apps": [],
                "details": [
                    {
                        "appID": app_id,
                        "paths": [
                            "/.well-known/app-auth-callback*",
                            "/shared/conversations/*",
                        ],
                    }
                ],
            },
        },
        headers={"Content-Type": "application/json"},
    )
