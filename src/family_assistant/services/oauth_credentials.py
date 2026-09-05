"""Per-user OAuth access-token resolution — the cross-user scoping chokepoint.

The resolver turns the turn's *execution context* into a valid access token for
the acting user at one OAuth provider, refreshing the stored refresh token when
needed. It takes the execution context rather than a user id so no code path
from tool arguments can address another user's data (see
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
from typing import TYPE_CHECKING

import httpx

from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from family_assistant.config_models import OAuthIntegrationConfig
    from family_assistant.services.credential_encryption import CredentialEncryption
    from family_assistant.services.notifier import Notifier
    from family_assistant.services.oauth_provider import OAuthProviderSpec
    from family_assistant.storage.repositories.oauth_connections import (
        OAuthConnectionModel,
    )
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Refresh the access token when this little of its lifetime remains, so an
# in-flight request never races the expiry.
_REFRESH_MARGIN_SECONDS = 60
# Fallback lifetime when the token endpoint omits ``expires_in``.
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600


class OAuthCredentialError(Exception):
    """Base class for credential-resolution failures.

    Every subclass carries an actionable, user-renderable message so tools can
    surface it directly as a tool error.
    """


class OAuthNoActingUserError(OAuthCredentialError):
    """Raised when the turn has no acting user (system/ambient context)."""

    def __init__(self, provider_display_name: str) -> None:
        super().__init__(
            f"{provider_display_name} access is only available when acting on "
            "behalf of a specific user; this context has no acting user."
        )


class OAuthNotConnectedError(OAuthCredentialError):
    """Raised when the acting user has no connection at this provider."""

    def __init__(self, provider_display_name: str) -> None:
        super().__init__(
            f"no {provider_display_name} account connected — connect from Settings"
        )


class OAuthReauthRequiredError(OAuthCredentialError):
    """Raised when the connection needs re-consent (needs_reauth / invalid_grant)."""

    def __init__(self, provider_display_name: str) -> None:
        super().__init__(
            f"your {provider_display_name} connection needs to be re-authorized — "
            "reconnect from Settings"
        )


class OAuthScopeNotGrantedError(OAuthCredentialError):
    """Raised when the required scope was not granted for this user's connection."""

    def __init__(self, provider_display_name: str, scope: str) -> None:
        self.scope = str(scope)
        super().__init__(
            f"your {provider_display_name} connection doesn't include "
            f"{self.scope} access — reconnect from Settings and approve it"
        )


class OAuthRefreshFailedError(OAuthCredentialError):
    """Raised on a transient refresh failure (network/5xx/unexpected response).

    The connection row is left untouched, so a later retry can succeed.
    """

    def __init__(self, provider_display_name: str) -> None:
        super().__init__(
            f"couldn't refresh your {provider_display_name} access token right "
            "now — please try again"
        )


@dataclass(frozen=True)
class _CachedToken:
    """An access token and the moment it should be considered expired."""

    access_token: str
    expires_at: float


class OAuthCredentialResolver:
    """Resolves per-user access tokens for one OAuth provider.

    Constructed once at app wiring. Not safe to share a single instance across
    event loops, but all use is within one app loop.
    """

    def __init__(
        self,
        provider: OAuthProviderSpec,
        config: OAuthIntegrationConfig,
        encryption: CredentialEncryption,
        http_client: httpx.AsyncClient,
        notifier: Notifier | None,
        *,
        token_endpoint: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            provider: The OAuth provider this resolver serves.
            config: The provider's integration config (OAuth client id/secret).
            encryption: Decrypts the stored refresh token.
            http_client: Async HTTP client used to POST the token endpoint.
            notifier: Optional user-notification channel for needs_reauth alerts.
            token_endpoint: OAuth token endpoint (defaults to the provider's;
                override for tests).
            clock: Clock for token-expiry math (defaults to the system clock).
        """
        self._provider = provider
        self._config = config
        self._encryption = encryption
        self._http_client = http_client
        self._notifier = notifier
        self._token_endpoint = token_endpoint or provider.token_url
        self._clock = clock or SystemClock()
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._operation_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def user_operation_lock(self, user_id: str, operation: str) -> asyncio.Lock:
        """Return the app-loop lock for one user's serialized provider operation."""
        return self._operation_locks.setdefault((user_id, operation), asyncio.Lock())

    async def access_token_for(
        self, exec_context: ToolExecutionContext, scope: str
    ) -> str:
        """Return a valid access token for the turn's acting user.

        Raises one of the ``OAuthCredentialError`` subclasses — all rendered as
        actionable tool errors — when a token cannot be produced.
        """
        display_name = self._provider.display_name
        user_id = exec_context.user_id
        if not user_id:
            raise OAuthNoActingUserError(display_name)

        connection = await exec_context.db_context.oauth_connections.get_connection(
            user_id, self._provider.name
        )
        if connection is None:
            raise OAuthNotConnectedError(display_name)
        if connection.status == "needs_reauth":
            raise OAuthReauthRequiredError(display_name)
        if scope not in connection.scopes:
            raise OAuthScopeNotGrantedError(display_name, scope)

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

        Called by tools after a 401 from a provider data API so the next
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
        connection: OAuthConnectionModel,
    ) -> str:
        """Refresh and cache the access token while holding the user's lock."""
        display_name = self._provider.display_name
        generation = connection.credential_generation
        refresh_token = self._encryption.decrypt(connection.refresh_token_encrypted)

        try:
            response = await self._http_client.post(
                self._token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._config.oauth_client_id,
                    "client_secret": self._config.oauth_client_secret.get_secret_value(),
                    "refresh_token": refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("%s token refresh network error: %s", display_name, exc)
            raise OAuthRefreshFailedError(display_name) from exc

        if response.status_code == httpx.codes.OK:
            token, expiry = self._parse_token_response(response)
            self._store_token(user_id, generation, token, expiry)
            return token

        if self._is_invalid_grant(response):
            await self._handle_invalid_grant(
                exec_context, user_id, connection.provider, generation
            )
            raise OAuthReauthRequiredError(display_name)

        logger.warning(
            "%s token refresh failed with status %s",
            display_name,
            response.status_code,
        )
        raise OAuthRefreshFailedError(display_name)

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
            logger.warning(
                "%s token response missing access_token",
                self._provider.display_name,
            )
            raise OAuthRefreshFailedError(self._provider.display_name)
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
        """Whether a non-200 response is the provider's revoked/expired signal."""
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
        flipped = await exec_context.db_context.oauth_connections.mark_needs_reauth(
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
        display_name = self._provider.display_name
        try:
            await self._notifier.send_notification(
                user_id,
                f"{display_name} connection needs attention",
                f"Your {display_name} connection needs re-authorization — "
                "reconnect from Settings.",
                exec_context.db_context,
            )
        except Exception:
            logger.exception("Failed to send needs_reauth notification to %s", user_id)
