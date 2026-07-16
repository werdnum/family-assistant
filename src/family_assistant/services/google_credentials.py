"""Per-user Google access-token resolution — the cross-user scoping chokepoint.

The resolver turns the turn's *execution context* into a valid Google access
token for the acting user, refreshing the stored refresh token when needed. It
takes the execution context rather than a user id so no code path from tool
arguments can address another user's data (see
``docs/design/user-scoped-google-data-access.md`` §2).

Key semantics:

- Access tokens are cached in memory only, keyed by
  ``(user_id, credential_generation)``. A reconnect/disconnect/revocation rotates
  the generation, so stale cache entries become unreachable immediately.
- A per-user :class:`asyncio.Lock` serializes refreshes: refresh is single-flight
  and a refresh re-checks the connection's generation before persisting any
  ``needs_reauth`` flip, so it can never invalidate a *replacement* connection.
- ``invalid_grant`` on refresh (revoked/expired) flips the connection to
  ``needs_reauth`` (generation-conditional) and notifies the owning user. Any
  other refresh failure is transient and leaves the row untouched. A decryption
  failure is a configuration error and also leaves the row untouched.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from family_assistant.config_models import GoogleIntegrationConfig
    from family_assistant.services.credential_encryption import CredentialEncryption
    from family_assistant.services.notifier import Notifier
    from family_assistant.storage.repositories.google_connections import (
        GoogleConnectionModel,
    )
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Refresh the access token when this little of its lifetime remains, so an
# in-flight request never races the expiry.
_REFRESH_MARGIN_SECONDS = 60
# Fallback lifetime when the token endpoint omits ``expires_in``.
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


class GoogleScope(StrEnum):
    """OAuth scopes the shipped Google tools can exercise (read-only)."""

    GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
    DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
    DRIVE_METADATA_READONLY = "https://www.googleapis.com/auth/drive.metadata.readonly"


# Startup allowlist: scopes an operator may configure in v1. Narrowing the grant
# is allowed; broadening beyond these read-only scopes is not.
SUPPORTED_GOOGLE_SCOPES: frozenset[str] = frozenset(
    scope.value for scope in GoogleScope
)


class GoogleCredentialError(Exception):
    """Base class for credential-resolution failures.

    Every subclass carries an actionable, user-renderable message so tools can
    surface it directly as a tool error.
    """


class GoogleNoActingUserError(GoogleCredentialError):
    """Raised when the turn has no acting user (system/ambient context)."""

    def __init__(
        self,
        message: str = (
            "Google access is only available when acting on behalf of a specific "
            "user; this context has no acting user."
        ),
    ) -> None:
        super().__init__(message)


class GoogleNotConnectedError(GoogleCredentialError):
    """Raised when the acting user has no Google connection."""

    def __init__(
        self,
        message: str = "no Google account connected — connect from Settings",
    ) -> None:
        super().__init__(message)


class GoogleReauthRequiredError(GoogleCredentialError):
    """Raised when the connection needs re-consent (needs_reauth / invalid_grant)."""

    def __init__(
        self,
        message: str = (
            "your Google connection needs to be re-authorized — reconnect from Settings"
        ),
    ) -> None:
        super().__init__(message)


class GoogleScopeNotGrantedError(GoogleCredentialError):
    """Raised when the required scope was not granted for this user's connection."""

    def __init__(self, scope: GoogleScope) -> None:
        self.scope = scope
        super().__init__(
            f"your Google connection doesn't include {scope.value} access — "
            "reconnect from Settings and approve it"
        )


class GoogleRefreshFailedError(GoogleCredentialError):
    """Raised on a transient refresh failure (network/5xx/unexpected response).

    The connection row is left untouched, so a later retry can succeed.
    """

    def __init__(
        self,
        message: str = (
            "couldn't refresh your Google access token right now — please try again"
        ),
    ) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class _CachedToken:
    """An access token and the moment it should be considered expired."""

    access_token: str
    expires_at: float


class GoogleCredentialResolver:
    """Resolves per-user Google access tokens from the turn's execution context.

    Constructed once at app wiring. Not safe to share a single instance across
    event loops, but all use is within one app loop.
    """

    def __init__(
        self,
        config: GoogleIntegrationConfig,
        encryption: CredentialEncryption,
        http_client: httpx.AsyncClient,
        notifier: Notifier | None,
        *,
        token_endpoint: str = GOOGLE_TOKEN_ENDPOINT,
        clock: Clock | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            config: Google integration config (OAuth client id/secret).
            encryption: Decrypts the stored refresh token.
            http_client: Async HTTP client used to POST the token endpoint.
            notifier: Optional user-notification channel for needs_reauth alerts.
            token_endpoint: Google's OAuth token endpoint (override for tests).
            clock: Clock for token-expiry math (defaults to the system clock).
        """
        self._config = config
        self._encryption = encryption
        self._http_client = http_client
        self._notifier = notifier
        self._token_endpoint = token_endpoint
        self._clock = clock or SystemClock()
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    async def access_token_for(
        self, exec_context: ToolExecutionContext, scope: GoogleScope
    ) -> str:
        """Return a valid access token for the turn's acting user.

        Raises one of the ``GoogleCredentialError`` subclasses — all rendered as
        actionable tool errors — when a token cannot be produced.
        """
        user_id = exec_context.user_id
        if not user_id:
            raise GoogleNoActingUserError()

        connection = await exec_context.db_context.google_connections.get_connection(
            user_id
        )
        if connection is None:
            raise GoogleNotConnectedError()
        if connection.status == "needs_reauth":
            raise GoogleReauthRequiredError()
        if scope.value not in connection.scopes:
            raise GoogleScopeNotGrantedError(scope)

        generation = connection.credential_generation
        cached = self._get_cached(user_id, generation)
        if cached is not None:
            return cached

        lock = self._user_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # Single-flight: another waiter may have refreshed while we blocked.
            cached = self._get_cached(user_id, generation)
            if cached is not None:
                return cached
            return await self._refresh_locked(exec_context, user_id, connection)

    def evict_cached_token(self, user_id: str) -> None:
        """Drop the user's cached access token(s).

        Called by tools after a 401 from a Google data API so the next
        ``access_token_for`` forces a refresh (and, if that fails with
        ``invalid_grant``, fires the needs_reauth path immediately).
        """
        stale = [key for key in self._cache if key[0] == user_id]
        for key in stale:
            del self._cache[key]

    def _get_cached(self, user_id: str, generation: str) -> str | None:
        """Return a still-valid cached token for this generation, or None."""
        cached = self._cache.get((user_id, generation))
        if cached is None:
            return None
        now = self._clock.now().timestamp()
        if cached.expires_at - now <= _REFRESH_MARGIN_SECONDS:
            return None
        return cached.access_token

    async def _refresh_locked(
        self,
        exec_context: ToolExecutionContext,
        user_id: str,
        connection: GoogleConnectionModel,
    ) -> str:
        """Refresh and cache the access token while holding the user's lock."""
        generation = connection.credential_generation
        refresh_token = self._encryption.decrypt(connection.refresh_token_encrypted)

        try:
            response = await self._http_client.post(
                self._token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._config.oauth_client_id,
                    "client_secret": self._config.oauth_client_secret,
                    "refresh_token": refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("Google token refresh network error: %s", exc)
            raise GoogleRefreshFailedError() from exc

        if response.status_code == httpx.codes.OK:
            token, expiry = self._parse_token_response(response)
            self._store_token(user_id, generation, token, expiry)
            await exec_context.db_context.google_connections.update_last_used(
                user_id, connection.provider
            )
            return token

        if self._is_invalid_grant(response):
            await self._handle_invalid_grant(
                exec_context, user_id, connection.provider, generation
            )
            raise GoogleReauthRequiredError()

        logger.warning(
            "Google token refresh failed with status %s", response.status_code
        )
        raise GoogleRefreshFailedError()

    def _store_token(
        self, user_id: str, generation: str, token: str, expires_at: float
    ) -> None:
        """Cache the token for this generation, dropping stale generations."""
        stale = [
            key for key in self._cache if key[0] == user_id and key[1] != generation
        ]
        for key in stale:
            del self._cache[key]
        self._cache[(user_id, generation)] = _CachedToken(
            access_token=token, expires_at=expires_at
        )

    def _parse_token_response(self, response: httpx.Response) -> tuple[str, float]:
        """Extract the access token and absolute expiry from a 200 response."""
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            logger.warning("Google token response missing access_token")
            raise GoogleRefreshFailedError()
        expires_in = payload.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, (int, float))
            else _DEFAULT_TOKEN_LIFETIME_SECONDS
        )
        expires_at = self._clock.now().timestamp() + lifetime
        return token, expires_at

    @staticmethod
    def _is_invalid_grant(response: httpx.Response) -> bool:
        """Whether a non-200 response is Google's revoked/expired signal."""
        if response.status_code != httpx.codes.BAD_REQUEST:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("error") == "invalid_grant"

    async def _handle_invalid_grant(
        self,
        exec_context: ToolExecutionContext,
        user_id: str,
        provider: str,
        generation: str,
    ) -> None:
        """Flip the connection to needs_reauth and notify the owning user.

        The flip is conditional on the generation read at the top of the resolve,
        so a refresh cannot invalidate a *replacement* connection created by a
        concurrent reconnect. Notify only when we actually flipped the row.
        """
        flipped = await exec_context.db_context.google_connections.mark_needs_reauth(
            user_id, provider, expected_generation=generation
        )
        if not flipped:
            logger.info(
                "Skipping needs_reauth notification for %s: generation changed "
                "(likely a concurrent reconnect)",
                user_id,
            )
            return
        self.evict_cached_token(user_id)
        await self._notify_reauth(exec_context, user_id)

    async def _notify_reauth(
        self, exec_context: ToolExecutionContext, user_id: str
    ) -> None:
        """Best-effort user notification that re-authorization is required."""
        if self._notifier is None:
            logger.info("No notifier configured; skipping needs_reauth alert")
            return
        try:
            await self._notifier.send_notification(
                user_id,
                "Google connection needs attention",
                "Your Google connection needs re-authorization — reconnect from "
                "Settings.",
                exec_context.db_context,
            )
        except Exception:
            logger.exception("Failed to send needs_reauth notification to %s", user_id)
