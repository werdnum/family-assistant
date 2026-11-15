"""Sign in with ChatGPT for plan usage, and the access tokens it yields.

Implements OpenAI's documented open-source flow
(https://developers.openai.com/siwc/token-sharing-open-source): a dynamically
registered public client, a loopback redirect the user pastes back to us
because the server cannot receive it, and rotating refresh tokens. The
deployment holds one sign-in; see ``docs/design/chatgpt-plan-usage.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from family_assistant.llm.connections import PlanCredentialError
from family_assistant.services.credential_encryption import (
    CredentialDecryptionError,
)
from family_assistant.storage.repositories.chatgpt_plan import (
    ChatGPTPlanCredentialsModel,
    ChatGPTPlanTokenSet,
)
from family_assistant.utils.clock import Clock, SystemClock

if TYPE_CHECKING:
    from family_assistant.services.credential_encryption import CredentialEncryption
    from family_assistant.storage.database import Database

logger = logging.getLogger(__name__)

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
# Nothing listens here: the browser lands on a page that fails to load, and the
# user copies its address back into the settings page.
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"

PENDING_SIGN_IN_TTL = timedelta(minutes=10)
# Refresh this close to expiry so a request never carries a token that lapses
# in flight.
_REFRESH_MARGIN = timedelta(minutes=5)
_HTTP_TIMEOUT_SECONDS = 30.0
# Codes OpenAI documents as "the refresh token is unusable: sign in again".
# `invalid_client` is included because a fresh sign-in registers a new client.
_TERMINAL_REFRESH_ERRORS = frozenset({
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
    "invalid_client",
})


@dataclass(frozen=True)
class ChatGPTPlanEndpoints:
    """OpenAI's auth endpoints; tests point these at a mock transport."""

    authorize: str = f"{ISSUER}/api/accounts/authorize"
    token: str = f"{ISSUER}/api/accounts/oauth/token"
    discovery: str = f"{ISSUER}/.well-known/openid-configuration"


class ChatGPTPlanError(PlanCredentialError):
    """A failure whose message is fit to show the user as-is."""


class ChatGPTPlanNotSignedInError(ChatGPTPlanError):
    def __init__(self) -> None:
        super().__init__(
            "No ChatGPT account is signed in for plan usage. Sign in under "
            "Settings → Connected Accounts."
        )


class ChatGPTPlanReauthRequiredError(ChatGPTPlanError):
    def __init__(self) -> None:
        super().__init__(
            "The ChatGPT sign-in has expired or was revoked. Sign in again under "
            "Settings → Connected Accounts."
        )


class ChatGPTPlanRefreshFailedError(ChatGPTPlanError):
    """A transient refresh failure; the stored sign-in is untouched."""

    transient = True

    def __init__(self, detail: str) -> None:
        super().__init__(f"Couldn't refresh the ChatGPT access token ({detail}).")


class ChatGPTPlanSignInError(ChatGPTPlanError):
    """The pasted sign-in result could not be accepted."""


@dataclass(frozen=True)
class _PendingSignIn:
    state: str
    nonce: str
    code_verifier: str
    client_id: str
    ext_agent_host_id: str
    # The subject a returning sign-in must come back as; None for a new
    # registration.
    expected_subject: str | None
    created_at: datetime


@dataclass(frozen=True)
class ChatGPTPlanStatusView:
    """What the settings page shows. Never any token material."""

    status: str | None
    email: str | None
    connected_by_user_id: str | None
    last_used_at: datetime | None


@dataclass
class _TokenResponse:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_at: datetime
    earliest_refresh_at: datetime | None
    scopes: list[str] = field(default_factory=list)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _jwt_claims(token: str) -> dict[str, object]:
    """The payload of a JWT, unverified.

    Only applied to an ID token received straight from the token endpoint over
    TLS, where OIDC Core 3.1.3.7 lets TLS stand in for the signature check.
    """
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (IndexError, ValueError) as exc:
        raise ChatGPTPlanSignInError("OpenAI returned a malformed ID token.") from exc
    if not isinstance(claims, dict):
        raise ChatGPTPlanSignInError("OpenAI returned a malformed ID token.")
    return claims


def _error_code(response: httpx.Response) -> str | None:
    """The OAuth error code, whether sent as a string or a nested object."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class ChatGPTPlanAuth:
    """The deployment's ChatGPT plan sign-in and its access tokens.

    One instance per application. Refreshes are serialized in-process, and the
    rotated token set is written with a compare-and-swap on the generation it
    was read under, so a refresh racing a new sign-in never overwrites it.
    """

    def __init__(
        self,
        db: Database,
        encryption: CredentialEncryption,
        http_client: httpx.AsyncClient,
        *,
        agent_name: str,
        endpoints: ChatGPTPlanEndpoints | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._encryption = encryption
        self._http = http_client
        self._agent_name = agent_name
        self._endpoints = endpoints or ChatGPTPlanEndpoints()
        self._clock = clock or SystemClock()
        self._refresh_lock = asyncio.Lock()
        self._pending: dict[str, _PendingSignIn] = {}
        # A generation whose access token the API rejected: the next request
        # refreshes instead of trusting the stored expiry.
        self._rejected_generation: str | None = None
        self._last_generation: str | None = None

    # -- inference ---------------------------------------------------------

    async def available(self) -> bool:
        """Whether an active sign-in exists (it may still fail to refresh)."""
        row = await self._db.chatgpt_plan.get()
        return row is not None and row.status == "active"

    async def access_token(self) -> str:
        """A currently valid access token, refreshing it if needed."""
        row = await self._usable_row()
        if not self._needs_refresh(row):
            return await self._hand_out(row)
        async with self._refresh_lock:
            row = await self._usable_row()
            if not self._needs_refresh(row):
                return await self._hand_out(row)
            return await self._refresh(row)

    def reject_access_token(self) -> None:
        """Record that the API refused the current token (HTTP 401)."""
        self._rejected_generation = self._last_generation

    async def _usable_row(self) -> ChatGPTPlanCredentialsModel:
        row = await self._db.chatgpt_plan.get()
        if row is None or row.status == "signed_out":
            raise ChatGPTPlanNotSignedInError()
        if row.status == "needs_reauth":
            raise ChatGPTPlanReauthRequiredError()
        return row

    def _needs_refresh(self, row: ChatGPTPlanCredentialsModel) -> bool:
        if row.credential_generation == self._rejected_generation:
            return True
        expires_at = row.access_token_expires_at
        if expires_at is None or row.access_token_encrypted is None:
            return True
        now = self._clock.now()
        if expires_at - now > _REFRESH_MARGIN:
            return False
        if now >= expires_at:
            return True
        # Inside the margin but still valid: honour OpenAI's earliest time.
        return row.earliest_refresh_at is None or now >= row.earliest_refresh_at

    async def _hand_out(self, row: ChatGPTPlanCredentialsModel) -> str:
        assert row.access_token_encrypted is not None
        token = self._decrypt(row.access_token_encrypted)
        self._last_generation = row.credential_generation
        now = self._clock.now()
        if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=5):
            await self._db.chatgpt_plan.touch_last_used(row.credential_generation)
        return token

    async def _refresh(self, row: ChatGPTPlanCredentialsModel) -> str:
        if row.refresh_token_encrypted is None:
            await self._db.chatgpt_plan.mark_needs_reauth(row.credential_generation)
            raise ChatGPTPlanReauthRequiredError()
        refresh_token = self._decrypt(row.refresh_token_encrypted)
        try:
            response = await self._http.post(
                self._endpoints.token,
                data={
                    "grant_type": "refresh_token",
                    "client_id": row.issued_client_id,
                    "refresh_token": refresh_token,
                    "resource": RESOURCE,
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.warning("ChatGPT token refresh network error: %s", exc)
            raise ChatGPTPlanRefreshFailedError("network error") from exc

        if response.status_code != httpx.codes.OK:
            code = _error_code(response)
            if code in _TERMINAL_REFRESH_ERRORS or response.status_code == 401:
                logger.warning("ChatGPT refresh token unusable (%s)", code)
                await self._db.chatgpt_plan.mark_needs_reauth(row.credential_generation)
                raise ChatGPTPlanReauthRequiredError()
            logger.warning(
                "ChatGPT token refresh failed: HTTP %s %s", response.status_code, code
            )
            raise ChatGPTPlanRefreshFailedError(f"HTTP {response.status_code}")

        tokens = self._parse_token_response(response)
        stored = await self._db.chatgpt_plan.store_refreshed_tokens(
            row.credential_generation,
            ChatGPTPlanTokenSet(
                id_token_encrypted=(
                    self._encryption.encrypt(tokens.id_token)
                    if tokens.id_token
                    else row.id_token_encrypted
                ),
                access_token_encrypted=self._encryption.encrypt(tokens.access_token),
                # OpenAI always rotates it; keeping the old one if a response
                # ever omits it is the only token that could still work.
                refresh_token_encrypted=(
                    self._encryption.encrypt(tokens.refresh_token)
                    if tokens.refresh_token
                    else row.refresh_token_encrypted
                ),
                access_token_expires_at=tokens.expires_at,
                earliest_refresh_at=tokens.earliest_refresh_at,
                scopes=tokens.scopes or row.scopes,
            ),
        )
        if stored is None:
            # A sign-in or sign-out replaced the row mid-refresh; its state wins.
            replacement = await self._usable_row()
            if self._needs_refresh(replacement):
                raise ChatGPTPlanRefreshFailedError("sign-in changed during refresh")
            return await self._hand_out(replacement)
        self._last_generation = stored
        return tokens.access_token

    def _parse_token_response(self, response: httpx.Response) -> _TokenResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatGPTPlanRefreshFailedError("unreadable token response") from exc
        access_token = (
            payload.get("access_token") if isinstance(payload, dict) else None
        )
        if not isinstance(access_token, str) or not access_token:
            raise ChatGPTPlanRefreshFailedError("token response had no access token")
        expires_in = payload.get("expires_in")
        lifetime = (
            float(expires_in)
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            else 3600.0
        )
        refresh_token = payload.get("refresh_token")
        id_token = payload.get("id_token")
        scope = payload.get("scope")
        return _TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            id_token=id_token if isinstance(id_token, str) else None,
            expires_at=self._clock.now() + timedelta(seconds=lifetime),
            earliest_refresh_at=_timestamp(payload.get("earliest_refresh_at")),
            scopes=scope.split() if isinstance(scope, str) else [],
        )

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self._encryption.decrypt(ciphertext)
        except CredentialDecryptionError as exc:
            raise ChatGPTPlanError(
                "The stored ChatGPT sign-in can't be decrypted. Check "
                "CREDENTIAL_ENCRYPTION_KEY; the sign-in is kept unchanged."
            ) from exc

    # -- sign-in -----------------------------------------------------------

    async def status(self) -> ChatGPTPlanStatusView:
        row = await self._db.chatgpt_plan.get()
        if row is None:
            return ChatGPTPlanStatusView(None, None, None, None)
        return ChatGPTPlanStatusView(
            status=row.status,
            email=row.email,
            connected_by_user_id=row.connected_by_user_id,
            last_used_at=row.last_used_at,
        )

    async def start_sign_in(self, user_id: str, *, use_saved_account: bool) -> str:
        """Begin a sign-in attempt and return the URL to open in the browser.

        ``use_saved_account`` signs the stored registration in again; otherwise
        a new client is registered, which is how a different ChatGPT account is
        chosen.
        """
        self._drop_expired_pending()
        saved = await self._db.chatgpt_plan.get()
        host_id = saved.ext_agent_host_id if saved else f"urn:uuid:{uuid.uuid4()}"
        reuse = use_saved_account and saved is not None
        pending = _PendingSignIn(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            client_id=saved.issued_client_id if reuse and saved else DYNAMIC_CLIENT_ID,
            ext_agent_host_id=host_id,
            expected_subject=saved.subject if reuse and saved else None,
            created_at=self._clock.now(),
        )
        self._pending[user_id] = pending
        params: dict[str, str] = {
            "client_id": pending.client_id,
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge_method": "S256",
            "code_challenge": _pkce_challenge(pending.code_verifier),
        }
        if reuse and saved is not None:
            if saved.email:
                params["login_hint"] = saved.email
            if saved.id_token_encrypted:
                params["id_token_hint"] = self._decrypt(saved.id_token_encrypted)
        else:
            params["agent_name_hint"] = self._agent_name
        return f"{self._endpoints.authorize}?{urlencode(params)}"

    async def complete_sign_in(
        self, user_id: str, callback_url: str
    ) -> ChatGPTPlanCredentialsModel:
        """Accept the loopback URL the browser was sent to, and store the sign-in."""
        self._drop_expired_pending()
        pending = self._pending.get(user_id)
        if pending is None:
            raise ChatGPTPlanSignInError(
                "No sign-in is in progress, or it expired. Start again."
            )
        query = parse_qs(urlsplit(callback_url.strip()).query)

        def param(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        state = param("state")
        if state is None or not secrets.compare_digest(state, pending.state):
            raise ChatGPTPlanSignInError(
                "That address doesn't belong to the current sign-in attempt. Paste "
                "the address from the most recent sign-in tab."
            )
        del self._pending[user_id]

        if (error := param("error")) is not None:
            raise ChatGPTPlanSignInError(f"ChatGPT sign-in did not complete ({error}).")
        code = param("code")
        if not code:
            raise ChatGPTPlanSignInError("The address has no authorization code.")
        client_id = self._issued_client_id(pending, param("client_id"))

        try:
            response = await self._http.post(
                self._endpoints.token,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": pending.code_verifier,
                    "redirect_uri": REDIRECT_URI,
                    "resource": RESOURCE,
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ChatGPTPlanSignInError(
                "Couldn't reach OpenAI to finish signing in. Start again."
            ) from exc
        if response.status_code != httpx.codes.OK:
            raise ChatGPTPlanSignInError(
                "OpenAI refused the sign-in "
                f"({_error_code(response) or response.status_code}). Start again."
            )
        try:
            tokens = self._parse_token_response(response)
        except ChatGPTPlanRefreshFailedError as exc:
            raise ChatGPTPlanSignInError(
                "OpenAI's token response was unusable."
            ) from exc
        if not tokens.refresh_token or not tokens.id_token:
            raise ChatGPTPlanSignInError(
                "OpenAI didn't return a renewable sign-in. Start again."
            )
        if PLAN_SCOPE not in tokens.scopes:
            raise ChatGPTPlanSignInError(
                "ChatGPT plan usage wasn't granted. Sign in again and allow Family "
                "Assistant to use your ChatGPT plan."
            )
        subject, email = self._validate_id_token(tokens.id_token, client_id, pending)

        return await self._db.chatgpt_plan.replace_sign_in(
            ext_agent_host_id=pending.ext_agent_host_id,
            issued_client_id=client_id,
            subject=subject,
            email=email,
            tokens=ChatGPTPlanTokenSet(
                id_token_encrypted=self._encryption.encrypt(tokens.id_token),
                access_token_encrypted=self._encryption.encrypt(tokens.access_token),
                refresh_token_encrypted=self._encryption.encrypt(tokens.refresh_token),
                access_token_expires_at=tokens.expires_at,
                earliest_refresh_at=tokens.earliest_refresh_at,
                scopes=tokens.scopes,
            ),
            connected_by_user_id=user_id,
        )

    @staticmethod
    def _issued_client_id(pending: _PendingSignIn, returned: str | None) -> str:
        if pending.client_id == DYNAMIC_CLIENT_ID:
            if not returned or returned == DYNAMIC_CLIENT_ID:
                raise ChatGPTPlanSignInError(
                    "OpenAI didn't finish registering Family Assistant. Start again."
                )
            return returned
        if returned and returned != pending.client_id:
            raise ChatGPTPlanSignInError(
                "The sign-in came back for a different registration. Start again."
            )
        return pending.client_id

    def _validate_id_token(
        self, id_token: str, client_id: str, pending: _PendingSignIn
    ) -> tuple[str, str | None]:
        claims = _jwt_claims(id_token)
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        exp = claims.get("exp")
        subject = claims.get("sub")
        if claims.get("iss") != ISSUER or client_id not in audiences:
            raise ChatGPTPlanSignInError("OpenAI's ID token was not issued for us.")
        if not isinstance(exp, (int, float)) or exp <= self._clock.now().timestamp():
            raise ChatGPTPlanSignInError("OpenAI's ID token has expired. Start again.")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str) or not secrets.compare_digest(
            nonce, pending.nonce
        ):
            raise ChatGPTPlanSignInError(
                "OpenAI's ID token doesn't match this sign-in."
            )
        if not isinstance(subject, str) or not subject:
            raise ChatGPTPlanSignInError("OpenAI's ID token names no account.")
        if pending.expected_subject is not None and subject != pending.expected_subject:
            raise ChatGPTPlanSignInError(
                "You signed in as a different ChatGPT account. To switch accounts, "
                "choose “Use a different account”."
            )
        email = claims.get("email")
        return subject, email if isinstance(email, str) else None

    def _drop_expired_pending(self) -> None:
        cutoff = self._clock.now() - PENDING_SIGN_IN_TTL
        for user_id in [
            user_id
            for user_id, pending in self._pending.items()
            if pending.created_at < cutoff
        ]:
            del self._pending[user_id]

    # -- sign-out ----------------------------------------------------------

    async def sign_out(self) -> bool:
        """Revoke the renewable session and clear the tokens.

        Returns whether OpenAI confirmed the revocation. The tokens are cleared
        either way; an unconfirmed revocation can be finished from ChatGPT's
        own settings.

        The tokens are cleared before the revocation round trip, and only if
        they are still the ones read: a sign-in completed meanwhile wins. The
        refresh lock keeps a refresh from rotating them in between.
        """
        async with self._refresh_lock:
            row = await self._db.chatgpt_plan.get()
            if row is None or row.status == "signed_out":
                return True
            if not await self._db.chatgpt_plan.sign_out(row.credential_generation):
                return True
        if row.refresh_token_encrypted is None:
            return False
        try:
            refresh_token = self._decrypt(row.refresh_token_encrypted)
        except ChatGPTPlanError:
            logger.warning("Could not decrypt the ChatGPT refresh token to revoke")
            return False
        return await self._revoke(refresh_token, row.issued_client_id)

    async def _revoke(self, refresh_token: str, client_id: str) -> bool:
        try:
            discovery = await self._http.get(
                self._endpoints.discovery, timeout=_HTTP_TIMEOUT_SECONDS
            )
            endpoint = discovery.json().get("revocation_endpoint")
            if discovery.status_code != httpx.codes.OK or not isinstance(endpoint, str):
                logger.warning("OpenAI discovery has no revocation endpoint")
                return False
            response = await self._http.post(
                endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("ChatGPT sign-out revocation failed: %s", exc)
            return False
        if response.status_code != httpx.codes.OK:
            logger.warning(
                "ChatGPT sign-out revocation returned HTTP %s", response.status_code
            )
            return False
        return True
