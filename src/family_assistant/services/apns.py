"""APNs (Apple Push Notification service) sender for iOS push notifications.

Uses provider-token (JWT) authentication with an Apple ``.p8`` auth key and sends HTTP/2 requests
to APNs. The service mirrors the interface of :class:`PushNotificationService` so it can be used
interchangeably (and combined) via :class:`NotificationDispatcher`.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable

import httpx
import jwt

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.ios_push_token import IosPushToken

logger = logging.getLogger(__name__)

# APNs hosts. Token-based auth uses port 443.
APNS_HOST_PRODUCTION = "https://api.push.apple.com"
APNS_HOST_SANDBOX = "https://api.sandbox.push.apple.com"

# Apple requires provider tokens to be refreshed between 20 and 60 minutes. Refresh well within
# that window to avoid ExpiredProviderToken responses.
PROVIDER_TOKEN_REFRESH_SECONDS = 40 * 60

# APNs reasons that mean the device token is permanently invalid and should be removed.
_UNREGISTERED_REASONS = frozenset({
    "Unregistered",
    "ExpiredToken",
    "DeviceTokenNotForTopic",
})
# APNs reasons that mean our provider (JWT) token is invalid and should be regenerated.
_PROVIDER_TOKEN_REASONS = frozenset({
    "ExpiredProviderToken",
    "InvalidProviderToken",
    "MissingProviderToken",
})


def _host_for_environment(environment: str) -> str:
    """Return the APNs host for a token environment."""
    return APNS_HOST_SANDBOX if environment == "sandbox" else APNS_HOST_PRODUCTION


def _other_environment(environment: str) -> str:
    """Return the opposite APNs environment."""
    return "production" if environment == "sandbox" else "sandbox"


class APNsService:
    """Service for sending push notifications to iOS devices via APNs."""

    def __init__(
        self,
        *,
        team_id: str | None,
        key_id: str | None,
        auth_key: str | None,
        bundle_id: str | None,
        use_sandbox: bool = False,
        client: httpx.AsyncClient | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the APNs service.

        Args:
            team_id: Apple Developer Team ID (JWT ``iss`` claim).
            key_id: APNs auth key id (JWT ``kid`` header).
            auth_key: Contents of the ``.p8`` private key (PEM).
            bundle_id: App bundle id, used as the ``apns-topic`` header.
            use_sandbox: Default environment for tokens that do not specify one.
            client: Optional pre-configured HTTP/2 client (primarily for tests). When omitted, a
                client is created lazily on first use.
            time_fn: Callable returning the current wall-clock time, injectable for tests.
        """
        self._team_id = team_id
        self._key_id = key_id
        self._auth_key = auth_key
        self._bundle_id = bundle_id
        self._default_environment = "sandbox" if use_sandbox else "production"
        self._time_fn = time_fn

        self._client = client
        self._owns_client = client is None

        self._cached_token: str | None = None
        self._cached_token_issued_at: float = 0.0

        self.enabled = bool(team_id and key_id and auth_key and bundle_id)

    def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP/2 client, creating it lazily if needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(http2=True, timeout=10.0)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this service owns it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _provider_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached or freshly-signed APNs provider (JWT) token."""
        if self._auth_key is None:
            raise RuntimeError("APNs auth key is not configured")
        now = self._time_fn()
        if (
            not force_refresh
            and self._cached_token is not None
            and now - self._cached_token_issued_at < PROVIDER_TOKEN_REFRESH_SECONDS
        ):
            return self._cached_token

        token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            self._auth_key,
            algorithm="ES256",
            headers={"kid": self._key_id, "alg": "ES256"},
        )
        self._cached_token = token
        self._cached_token_issued_at = now
        return token

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: DatabaseContext,
    ) -> None:
        """Send an APNs alert to all iOS tokens registered for a user.

        Args:
            user_identifier: The user identifier.
            title: Notification title.
            body: Notification body text.
            db_context: Database context for accessing and pruning tokens.
        """
        if not self.enabled:
            logger.debug(
                "APNs disabled - team_id/key_id/auth_key/bundle_id not configured"
            )
            return

        tokens = await db_context.ios_push_tokens.get_by_user(user_identifier)
        if not tokens:
            return

        payload = json.dumps({
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"}
        }).encode("utf-8")

        results = await asyncio.gather(
            *(self._deliver(token, payload) for token in tokens)
        )

        # Apply token mutations sequentially to avoid concurrent DB writes.
        for token, action in zip(tokens, results, strict=True):
            if action == "delete":
                await db_context.ios_push_tokens.delete_by_token(token.device_token)
            elif action is not None:
                # A corrected environment ("production"/"sandbox").
                await db_context.ios_push_tokens.update_environment(
                    token.device_token, action
                )

    async def _deliver(self, token: IosPushToken, payload: bytes) -> str | None:
        """Deliver a notification to a single device token.

        Returns:
            ``"delete"`` if the token should be removed, the corrected environment string if it
            should be updated, or ``None`` if no token mutation is required.
        """
        environment = token.environment or self._default_environment
        status_code, reason = await self._post(token.device_token, environment, payload)

        # Provider token rejected: regenerate and retry once in the same environment.
        if status_code == 403 and reason in _PROVIDER_TOKEN_REASONS:
            logger.warning(
                "APNs provider token rejected (%s); refreshing and retrying", reason
            )
            status_code, reason = await self._post(
                token.device_token, environment, payload, force_token_refresh=True
            )

        if status_code == 200:
            logger.info("Sent APNs notification to token %s…", token.device_token[:8])
            return None

        if status_code == 410 or reason in _UNREGISTERED_REASONS:
            logger.info(
                "APNs token %s… unregistered (status=%s reason=%s); deleting",
                token.device_token[:8],
                status_code,
                reason,
            )
            return "delete"

        if reason == "BadDeviceToken":
            # Likely a sandbox/production mismatch. Retry against the other environment.
            other = _other_environment(environment)
            logger.info(
                "APNs token %s… rejected as BadDeviceToken for %s; retrying %s",
                token.device_token[:8],
                environment,
                other,
            )
            retry_status, retry_reason = await self._post(
                token.device_token, other, payload
            )
            if retry_status == 200:
                logger.info(
                    "APNs token %s… delivered on %s; updating stored environment",
                    token.device_token[:8],
                    other,
                )
                return other
            logger.warning(
                "APNs token %s… invalid in both environments (%s/%s); deleting",
                token.device_token[:8],
                reason,
                retry_reason,
            )
            return "delete"

        logger.warning(
            "APNs delivery failed for token %s… (status=%s reason=%s)",
            token.device_token[:8],
            status_code,
            reason,
        )
        return None

    async def _post(
        self,
        device_token: str,
        environment: str,
        payload: bytes,
        *,
        force_token_refresh: bool = False,
    ) -> tuple[int, str | None]:
        """Send a single APNs request, returning ``(status_code, reason)``."""
        url = f"{_host_for_environment(environment)}/3/device/{device_token}"
        headers = {
            "authorization": f"bearer {self._provider_token(force_refresh=force_token_refresh)}",
            "apns-topic": self._bundle_id or "",
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(self._time_fn()) + 3600),
            "content-type": "application/json",
        }
        try:
            response = await self._get_client().post(
                url, content=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "APNs request error for token %s…: %s", device_token[:8], exc
            )
            return 0, None

        if response.status_code == 200:
            return 200, None

        reason: str | None = None
        try:
            reason = response.json().get("reason")
        except (json.JSONDecodeError, ValueError):
            reason = response.text or None
        return response.status_code, reason
