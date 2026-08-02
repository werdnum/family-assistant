"""Web endpoints for the per-user OAuth connect flow (one router per provider).

Session-authenticated via :func:`get_current_user` — deliberately NOT covered by
the diagnostics read-only token, so ``DIAGNOSTICS_READONLY_TOKEN`` never unlocks
connecting, disconnecting, or reading a user's connection status.

The flow follows the approved design in
``docs/design/user-scoped-google-data-access.md`` (§"1. Connections > OAuth flow").
Mounted at ``/api/integrations/{provider}`` (see ``routers/api.py``):

- ``GET  /api/integrations/{provider}``           — connection status for the current user.
- ``GET  /api/integrations/{provider}/authorize`` — start the authorization-code + PKCE flow.
- ``GET  /api/integrations/{provider}/callback``  — atomically consume the pending flow,
  exchange the code, upsert the connection, and notify the user naming the account.
- ``DELETE /api/integrations/{provider}``         — best-effort revoke, then delete the row.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from family_assistant.config_models import OAuthIntegrationConfig
from family_assistant.services.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryption,
    CredentialEncryptionError,
)
from family_assistant.services.oauth_integration_state import OAuthIntegrationState
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import get_current_session_user, get_db

if TYPE_CHECKING:
    from family_assistant.services.oauth_provider import OAuthProviderSpec

logger = logging.getLogger(__name__)


class OAuthIntegrationStatus(BaseModel):
    """Connection status for the current user (never any token material)."""

    enabled: bool
    reason: str | None
    require_taint_enforcement_waived: bool
    connected: bool
    provider_account_email: str | None
    status: str | None
    granted_scopes: list[str]
    configured_scopes: list[str]
    missing_configured_scopes: list[str]
    last_used_at: str | None


# Pending flows expire after 10 minutes (enforced on claim + opportunistic cleanup).
PENDING_FLOW_TTL_SECONDS = 600

# Settings page the browser lands on after connect/disconnect / errors.
SETTINGS_REDIRECT_PATH = "/settings/accounts"


def get_oauth_http_client(request: Request) -> httpx.AsyncClient:
    """Return the async HTTP client used for OAuth outbound calls.

    The app installs its lifecycle-managed shared client on
    ``app.state.oauth_http_client`` at startup; tests inject an
    ``httpx.MockTransport``-backed client the same way. There is deliberately
    no module-default fallback — an unmanaged client would outlive the
    application lifespan.
    """
    injected = getattr(request.app.state, "oauth_http_client", None)
    if isinstance(injected, httpx.AsyncClient):
        return injected
    raise HTTPException(
        status_code=503,
        detail="OAuth HTTP client is not configured on this application.",
    )


CurrentUser = Annotated[dict, Depends(get_current_session_user)]
Db = Annotated[Database, Depends(get_db)]
OAuthClient = Annotated[httpx.AsyncClient, Depends(get_oauth_http_client)]


def _oauth_urls(request: Request, spec: OAuthProviderSpec) -> dict[str, str]:
    """Return the provider's OAuth endpoint URLs, honoring a per-app test override.

    ``app.state.oauth_url_overrides`` is a dict keyed by provider name whose
    values are partial dicts with the keys ``authorize``/``token``/``revoke``/
    ``userinfo``, merged over the spec's URLs.
    """
    defaults = {
        "authorize": spec.authorize_url,
        "token": spec.token_url,
        "revoke": spec.revoke_url,
        "userinfo": spec.userinfo_url,
    }
    overrides = getattr(request.app.state, "oauth_url_overrides", None)
    if isinstance(overrides, dict):
        provider_override = overrides.get(spec.name)
        if isinstance(provider_override, dict):
            return {**defaults, **provider_override}
    return defaults


def _oauth_integration_config(
    request: Request, spec: OAuthProviderSpec
) -> OAuthIntegrationConfig | None:
    """Return the provider's integration config section from app state."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        return None
    integration = getattr(config, spec.config_attr, None)
    if isinstance(integration, OAuthIntegrationConfig):
        return integration
    return None


def _shared_integration_state(
    request: Request, spec: OAuthProviderSpec
) -> OAuthIntegrationState | None:
    """Return the startup-computed integration state, if the app installed one."""
    states = getattr(request.app.state, "oauth_integration_states", None)
    if not isinstance(states, dict):
        return None
    state = states.get(spec.name)
    if isinstance(state, OAuthIntegrationState):
        return state
    return None


def _integration_enablement(
    request: Request, spec: OAuthProviderSpec
) -> tuple[bool, str | None]:
    """Return ``(enabled, reason)`` for the provider's integration.

    Reads the single startup-computed :class:`OAuthIntegrationState` — the sole
    authority, which validates credentials, the scope allowlist, real web
    authentication, and the taint floor together. When the app did not install
    that state we fail closed rather than re-deriving a reduced local check: a
    partial fallback could authorize a scope the startup evaluator would refuse
    (e.g. a write scope), so an absent state is treated as DISABLED.
    """
    shared = _shared_integration_state(request, spec)
    if shared is None:
        return False, f"{spec.display_name} integration state unavailable"
    return shared.enabled, shared.reason


def _require_enabled(
    request: Request, spec: OAuthProviderSpec
) -> OAuthIntegrationConfig:
    """Return the integration config, or raise 409 if the integration is disabled."""
    enabled, reason = _integration_enablement(request, spec)
    integration = _oauth_integration_config(request, spec)
    if not enabled or integration is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    return integration


def _sha256_hex(value: str) -> str:
    """Return the SHA-256 hex digest of a string (used to hash the state nonce)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    """Return an (RFC 7636) ``(code_verifier, code_challenge)`` S256 PKCE pair."""
    # 32 random bytes -> 43-char urlsafe string, within the 43-128 char range.
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _callback_redirect_uri(request: Request, spec: OAuthProviderSpec) -> str:
    """Return the absolute callback redirect URI for this deployment.

    Behind an HTTPS-terminating proxy ``request.url_for`` yields the backend's
    ``http://`` URL; honor ``x-forwarded-proto`` the same way the OIDC and
    app-auth flows do, since the provider validates the redirect_uri scheme.
    """
    redirect_uri = request.url_for(f"{spec.name}_integration_callback")
    if (
        request.headers.get("x-forwarded-proto") == "https"
        or request.url.scheme == "https"
    ):
        redirect_uri = redirect_uri.replace(scheme="https")
    return str(redirect_uri)


def _configured_scopes(integration: OAuthIntegrationConfig) -> list[str]:
    """Return the operator-configured data scopes."""
    return list(integration.scopes)


def _settings_redirect(query: dict[str, str]) -> RedirectResponse:
    """302-redirect the browser back to the settings page with query params."""
    url = f"{SETTINGS_REDIRECT_PATH}?{urlencode(query)}"
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


def _decode_id_token_email(id_token: str) -> str | None:
    """Return the ``email`` claim from an ID token JWT payload, if present.

    The token arrived directly from the provider's token endpoint over TLS, so
    an unverified payload decode is acceptable here — we are not relying on it
    as a security assertion, only to display which account was connected.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        return None
    email = payload.get("email")
    return email if isinstance(email, str) else None


async def _resolve_account_email(
    request: Request,
    spec: OAuthProviderSpec,
    http_client: httpx.AsyncClient,
    id_token: str | None,
    access_token: str | None,
) -> str | None:
    """Return the connected account email from the ID token or userinfo endpoint."""
    if id_token is not None:
        email = _decode_id_token_email(id_token)
        if email:
            return email

    if not access_token:
        return None
    try:
        response = await http_client.get(
            _oauth_urls(request, spec)["userinfo"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    data = response.json()
    email = data.get("email")
    return email if isinstance(email, str) else None


def create_oauth_integration_router(spec: OAuthProviderSpec) -> APIRouter:
    """Build the connect-flow router for one OAuth provider."""
    router = APIRouter()

    @router.get("")
    async def get_integration_status(
        request: Request,
        current_user: CurrentUser,
        db: Db,
    ) -> OAuthIntegrationStatus:
        """Return the current user's connection status (never token material)."""
        enabled, reason = _integration_enablement(request, spec)
        integration = _oauth_integration_config(request, spec)
        configured_scopes = (
            _configured_scopes(integration) if integration is not None else []
        )
        shared_state = _shared_integration_state(request, spec)
        require_taint_enforcement_waived = (
            shared_state.taint_enforcement_waived
            if shared_state is not None
            else (integration is not None and not integration.require_taint_enforcement)
        )

        # Report the stored connection even when the integration is currently
        # disabled (lost config / taint floor). Otherwise a user who connected
        # while it was enabled could neither see nor remove their persisted
        # refresh token.
        user_id = current_user["user_identifier"]
        connection = await db.oauth_connections.get_connection(user_id, spec.name)

        granted_scopes = list(connection.scopes) if connection is not None else []
        missing_configured_scopes = [
            scope for scope in configured_scopes if scope not in granted_scopes
        ]

        return OAuthIntegrationStatus(
            enabled=enabled,
            reason=reason,
            require_taint_enforcement_waived=require_taint_enforcement_waived,
            connected=connection is not None,
            provider_account_email=(
                connection.provider_account_email if connection is not None else None
            ),
            status=connection.status if connection is not None else None,
            granted_scopes=granted_scopes,
            configured_scopes=configured_scopes,
            missing_configured_scopes=missing_configured_scopes,
            last_used_at=(
                connection.last_used_at.isoformat()
                if connection is not None and connection.last_used_at is not None
                else None
            ),
        )

    @router.get("/authorize")
    async def start_authorize(
        request: Request,
        current_user: CurrentUser,
        db: Db,
    ) -> RedirectResponse:
        """Start the authorization-code + PKCE flow and redirect to consent."""
        integration = _require_enabled(request, spec)
        user_id = current_user["user_identifier"]

        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = _pkce_pair()

        await db.oauth_connections.cleanup_expired_flows(PENDING_FLOW_TTL_SECONDS)
        await db.oauth_connections.create_pending_flow(
            _sha256_hex(state), code_verifier, user_id
        )

        scopes = [*_configured_scopes(integration), *spec.identity_scopes]
        params = {
            "client_id": integration.oauth_client_id,
            "redirect_uri": _callback_redirect_uri(request, spec),
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **spec.extra_authorize_params,
        }
        authorize_url = f"{_oauth_urls(request, spec)['authorize']}?{urlencode(params)}"
        return RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)

    @router.get("/callback", name=f"{spec.name}_integration_callback")
    async def authorize_callback(
        request: Request,
        current_user: CurrentUser,
        db: Db,
        http_client: OAuthClient,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Complete the flow: consume the pending flow, exchange, upsert, notify."""
        integration = _require_enabled(request, spec)

        # The provider reports user-denied consent via the ``error`` param.
        # Consume the pending flow so the authorization URL dies with the denial
        # instead of remaining claimable for the rest of its TTL.
        if error:
            if state:
                await db.oauth_connections.claim_pending_flow(
                    _sha256_hex(state), PENDING_FLOW_TTL_SECONDS
                )
            return _settings_redirect({spec.name: "error", "message": error})

        if not code or not state:
            return _settings_redirect({
                spec.name: "error",
                "message": "missing_code_or_state",
            })

        # a. Atomically claim (single-use consume) the pending flow.
        flow = await db.oauth_connections.claim_pending_flow(
            _sha256_hex(state), PENDING_FLOW_TTL_SECONDS
        )
        if flow is None:
            # Tampering / replay / expiry — a JSON error is appropriate; there is
            # no trusted flow to tie a friendly redirect to.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unknown, expired, or already-used state.",
            )

        # b. The session user must match the flow's initiating user.
        user_id = current_user["user_identifier"]
        if user_id != flow.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated user does not match the initiating user of this flow.",
            )

        redirect_uri = _callback_redirect_uri(request, spec)

        # c. Exchange the code (server-side, including the claimed PKCE code_verifier).
        try:
            token_result = await http_client.post(
                _oauth_urls(request, spec)["token"],
                data={
                    "client_id": integration.oauth_client_id,
                    "client_secret": integration.oauth_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code_verifier": flow.code_verifier,
                },
            )
        except httpx.HTTPError:
            logger.warning(
                "%s token exchange request failed", spec.display_name, exc_info=True
            )
            return _settings_redirect({
                spec.name: "error",
                "message": "token_exchange_failed",
            })
        if token_result.status_code != 200:
            # Never echo the code or any token material back to the user.
            logger.warning(
                "%s token exchange returned status %s",
                spec.display_name,
                token_result.status_code,
            )
            return _settings_redirect({
                spec.name: "error",
                "message": "token_exchange_failed",
            })
        token_response = token_result.json()

        # d. A refresh token is required (we request offline consent via the
        # provider's extra authorize params).
        refresh_token = token_response.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            logger.warning(
                "%s token exchange returned no refresh_token", spec.display_name
            )
            return _settings_redirect({
                spec.name: "error",
                "message": "no_refresh_token",
            })

        # Granted scopes: partial grants are accepted (store what was granted).
        granted_scope_str = token_response.get("scope", "")
        granted_scopes = granted_scope_str.split() if granted_scope_str else []

        # e. Identify the connected account.
        id_token = token_response.get("id_token")
        access_token = token_response.get("access_token")
        account_email = await _resolve_account_email(
            request,
            spec,
            http_client,
            id_token if isinstance(id_token, str) else None,
            access_token if isinstance(access_token, str) else None,
        )
        if not account_email:
            logger.warning(
                "Could not resolve connected %s account email", spec.display_name
            )
            return _settings_redirect({
                spec.name: "error",
                "message": "account_unresolved",
            })

        # f. Encrypt the refresh token and upsert the connection.
        try:
            encryption = CredentialEncryption(integration.credential_encryption_key)
        except CredentialEncryptionError:
            logger.exception("Invalid CREDENTIAL_ENCRYPTION_KEY")
            return _settings_redirect({
                spec.name: "error",
                "message": "encryption_key_invalid",
            })
        refresh_token_encrypted = encryption.encrypt(refresh_token)

        await db.oauth_connections.upsert_connection(
            user_id=user_id,
            provider=spec.name,
            provider_account_email=account_email,
            scopes=granted_scopes,
            refresh_token_encrypted=refresh_token_encrypted,
        )

        # g. Notify the user, naming the account, so any swap is immediately visible.
        dispatcher = getattr(request.app.state, "notification_dispatcher", None)
        if dispatcher is not None:
            try:
                await dispatcher.send_notification(
                    user_identifier=user_id,
                    title=f"{spec.display_name} account connected",
                    body=(
                        f"{spec.display_name} account {account_email} was connected "
                        "to your Family Assistant account"
                    ),
                    db_context=db,
                )
            except Exception:
                logger.warning(
                    "Failed to send %s connection notification",
                    spec.display_name,
                    exc_info=True,
                )

        # h. Redirect to the settings page.
        return _settings_redirect({spec.name: "connected"})

    @router.delete("", status_code=status.HTTP_204_NO_CONTENT)
    async def disconnect_integration(
        request: Request,
        current_user: CurrentUser,
        db: Db,
        http_client: OAuthClient,
    ) -> None:
        """Best-effort revoke the refresh token, then delete the connection row.

        Deliberately NOT gated on enablement: if the integration becomes disabled
        (lost config / taint floor) after a user connected, they must still be
        able to remove their stored refresh token. Revocation is attempted only
        when the config still carries a well-formed encryption key so the token
        can be decrypted; otherwise the row is deleted regardless.
        """
        user_id = current_user["user_identifier"]

        connection = await db.oauth_connections.get_connection(user_id, spec.name)
        if connection is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No {spec.display_name} connection to disconnect.",
            )

        integration = _oauth_integration_config(request, spec)
        if integration is not None and integration.credential_encryption_key:
            # Best-effort revocation: a decryption failure or a failed revoke
            # still deletes the row below.
            try:
                encryption = CredentialEncryption(integration.credential_encryption_key)
                refresh_token = encryption.decrypt(connection.refresh_token_encrypted)
                await http_client.post(
                    _oauth_urls(request, spec)["revoke"],
                    data={"token": refresh_token},
                )
            except (
                CredentialEncryptionError,
                CredentialDecryptionError,
                httpx.HTTPError,
            ):
                logger.warning(
                    "%s token revocation failed; deleting connection anyway",
                    spec.display_name,
                    exc_info=True,
                )

        await db.oauth_connections.delete_connection(user_id, spec.name)

    return router
