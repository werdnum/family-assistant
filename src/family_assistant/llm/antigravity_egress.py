"""Egress policy and credential injection for the Antigravity sandbox.

The Interactions API's ``environment.network`` block configures the sandbox's
egress proxy: which domains a run may reach, and which headers the proxy
injects on the way out. That last part is what makes credentials usable here at
all -- the sandbox never receives the token, so nothing the agent can print,
log or write to a file contains it.

This module turns an ``AntigravityEnvironmentConfig`` into that payload,
minting a short-lived credential for each rule that names one. See
docs/design/antigravity-environment-and-credentials.md.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

import httpx
import jwt

from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.config_models import (
        AntigravityEgressCredentialConfig,
        AntigravityEnvironmentConfig,
    )
    from family_assistant.utils.clock import Clock

logger = logging.getLogger(__name__)

# Environment variable names the GitHub App credential reads. These match what
# the k8s-agent StatefulSet and the ai-worker SandboxTemplate already set, so a
# deployment that already runs a GitHub App needs no new secret plumbing.
GITHUB_APP_ID_ENV = "GITHUB_APP_ID"
GITHUB_APP_INSTALLATION_ID_ENV = "GITHUB_APP_INSTALLATION_ID"
GITHUB_APP_PRIVATE_KEY_ENV = "GITHUB_APP_PRIVATE_KEY"
GITHUB_APP_PRIVATE_KEY_PATH_ENV = "GITHUB_APP_PRIVATE_KEY_PATH"

GITHUB_API_BASE_URL = "https://api.github.com"

# GitHub caps App JWT lifetime at 10 minutes and rejects an `iat` in its own
# future, so the token is backdated to absorb clock skew between us and GitHub.
_APP_JWT_LIFETIME = timedelta(minutes=9)
_APP_JWT_BACKDATE = timedelta(seconds=60)

# How long a minted installation token stays reusable. Deliberately far shorter
# than the token's own ~1h life: the proxy is handed a fixed header that it uses
# for the whole of an agent run, so a run inherits whatever lifetime was left at
# submit. Caching by "not yet expired" would let a two-hour run start on a token
# with minutes to live and lose GitHub partway through -- including on the final
# push, after all the work. Reuse therefore only spans one submission, where a
# config naming `github_app` on several domains resolves them within
# milliseconds and all of them should carry the same token. Every new run mints
# fresh and so gets the longest window the credential can give it.
_INSTALLATION_TOKEN_REUSE_WINDOW = timedelta(seconds=60)

_GITHUB_GIT_BASIC_USERNAME = "x-access-token"


class AntigravityEgressError(RuntimeError):
    """A configured egress credential could not be resolved.

    Raised rather than omitting the header: a run that reaches a private
    repository unauthenticated fails deep inside the agent as a 404, which
    reads as the agent being confused rather than as a credential problem.
    """


class EgressAllowlistEntry(TypedDict, total=False):
    """One ``environment.network.allowlist`` entry."""

    domain: str
    transform: list[dict[str, str]]


class EgressAllowlistPayload(TypedDict):
    """The object form of ``environment.network``."""

    allowlist: list[EgressAllowlistEntry]


# ``environment.network`` is either the allowlist object or the literal string
# "disabled"; ``None`` means send no network block and take the API's default.
EgressNetworkPayload = EgressAllowlistPayload | str


class EgressNetworkResolver(Protocol):
    """Resolves the ``environment.network`` payload for one agent run."""

    async def resolve_network(self) -> EgressNetworkPayload | None:
        """Return the network block to send, or ``None`` to send none."""
        ...


def _read_github_app_private_key(env: Mapping[str, str]) -> str:
    """Read the App private key from an inline PEM or the path naming one."""
    inline = env.get(GITHUB_APP_PRIVATE_KEY_ENV)
    if inline and inline.strip():
        return inline
    key_path = env.get(GITHUB_APP_PRIVATE_KEY_PATH_ENV)
    if not key_path:
        raise AntigravityEgressError(
            f"GitHub App egress credential needs a private key: set "
            f"{GITHUB_APP_PRIVATE_KEY_ENV} to the PEM contents or "
            f"{GITHUB_APP_PRIVATE_KEY_PATH_ENV} to a file holding it."
        )
    try:
        return Path(key_path).read_text(encoding="utf-8")
    except OSError as e:
        raise AntigravityEgressError(
            f"GitHub App private key at {key_path!r} could not be read: {e}"
        ) from e


class GitHubAppInstallationTokenSource:
    """Mints GitHub App installation access tokens, one per agent run.

    The App private key never leaves this process: it signs a short-lived JWT,
    which is exchanged with GitHub for an installation token, and only that
    token is handed to the egress proxy.

    Reuse spans a single submission rather than the token's whole life (see
    ``_INSTALLATION_TOKEN_REUSE_WINDOW``), because the proxy holds one fixed
    header for the duration of a run. A run therefore always starts with the
    longest window the credential can give it -- though a run that outlives
    the token entirely still loses GitHub partway through, which no amount of
    caching policy here can fix.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        env: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = GITHUB_API_BASE_URL,
    ) -> None:
        self._clock = clock or SystemClock()
        self._env = env if env is not None else os.environ
        self._http_client = http_client
        self._owns_client = http_client is None
        self._api_base_url = api_base_url.rstrip("/")
        self._cached_token: str | None = None
        self._cached_expiry: datetime | None = None
        self._cached_minted_at: datetime | None = None
        # Two rules naming `github_app` resolve concurrently within one submit;
        # without this they would mint two tokens and carry different ones.
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=15.0)
        return self._http_client

    async def aclose(self) -> None:
        """Close the HTTP client if this source created it."""
        if self._http_client is not None and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None

    def _app_jwt(self) -> str:
        app_id = self._env.get(GITHUB_APP_ID_ENV)
        if not app_id:
            raise AntigravityEgressError(
                f"GitHub App egress credential requires {GITHUB_APP_ID_ENV}"
            )
        private_key = _read_github_app_private_key(self._env)
        now = self._clock.now()
        try:
            return jwt.encode(
                {
                    "iat": int((now - _APP_JWT_BACKDATE).timestamp()),
                    "exp": int((now + _APP_JWT_LIFETIME).timestamp()),
                    "iss": app_id,
                },
                private_key,
                algorithm="RS256",
            )
        except Exception as e:
            raise AntigravityEgressError(
                f"GitHub App private key could not sign a JWT: {e}"
            ) from e

    async def _mint(self) -> tuple[str, datetime | None]:
        installation_id = self._env.get(GITHUB_APP_INSTALLATION_ID_ENV)
        if not installation_id:
            raise AntigravityEgressError(
                "GitHub App egress credential requires "
                f"{GITHUB_APP_INSTALLATION_ID_ENV}"
            )
        app_jwt = self._app_jwt()
        url = f"{self._api_base_url}/app/installations/{installation_id}/access_tokens"
        try:
            response = await self._client().post(
                url,
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as e:
            raise AntigravityEgressError(
                f"GitHub refused an installation token for installation "
                f"{installation_id}: {e.response.status_code} {e.response.text}"
            ) from e
        except httpx.HTTPError as e:
            raise AntigravityEgressError(
                f"Requesting a GitHub installation token failed: {e}"
            ) from e

        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AntigravityEgressError(
                "GitHub installation token response carried no 'token'"
            )
        return token, _parse_expiry(payload.get("expires_at"))

    async def token(self) -> str:
        """Return an installation access token, minting one per run.

        A token is reused only for the moments a single submission takes to
        resolve its rules; anything older is re-minted so the run it is about
        to be frozen into starts with a full lifetime.
        """
        async with self._lock:
            now = self._clock.now()
            if (
                self._cached_token is not None
                and self._cached_minted_at is not None
                and self._cached_expiry is not None
                and now - self._cached_minted_at < _INSTALLATION_TOKEN_REUSE_WINDOW
                and now < self._cached_expiry
            ):
                return self._cached_token

            token, expires_at = await self._mint()
            self._cached_token = token
            self._cached_minted_at = now
            # A response without a usable `expires_at` is treated as the
            # documented hour; the reuse window above means this only bounds
            # the few seconds one submission spans.
            self._cached_expiry = expires_at or (now + timedelta(hours=1))
            logger.info(
                "Minted GitHub App installation token, valid until %s",
                self._cached_expiry.isoformat(),
            )
            return token


def _parse_expiry(raw: object) -> datetime | None:
    """Parse GitHub's ``expires_at`` (RFC 3339, ``Z``-suffixed) if present."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable GitHub token expiry %r; using a 1h default", raw)
        return None


def _render_header_value(scheme: str, token: str) -> str:
    """Render a credential as an ``Authorization`` value in the given scheme."""
    if scheme == "basic":
        encoded = base64.b64encode(
            f"{_GITHUB_GIT_BASIC_USERNAME}:{token}".encode()
        ).decode("ascii")
        return f"Basic {encoded}"
    return f"Bearer {token}"


class AntigravityEgressResolver:
    """Builds ``environment.network`` from a profile's environment config."""

    def __init__(
        self,
        config: AntigravityEnvironmentConfig,
        *,
        github_app_tokens: GitHubAppInstallationTokenSource | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._env = env if env is not None else os.environ
        # Created lazily so a config with no GitHub rule never constructs an
        # HTTP client or reads a key it does not need.
        self._github_app_tokens = github_app_tokens
        self._clock = clock

    def _github_source(self) -> GitHubAppInstallationTokenSource:
        if self._github_app_tokens is None:
            self._github_app_tokens = GitHubAppInstallationTokenSource(
                clock=self._clock, env=self._env
            )
        return self._github_app_tokens

    async def aclose(self) -> None:
        """Release any HTTP client this resolver created."""
        if self._github_app_tokens is not None:
            await self._github_app_tokens.aclose()

    async def _credential_header(
        self, credential: AntigravityEgressCredentialConfig
    ) -> dict[str, str]:
        if credential.type == "github_app":
            token = await self._github_source().token()
        else:
            # `token_env` is required for this type by the config model.
            token_env = credential.token_env or ""
            raw = self._env.get(token_env)
            if not raw:
                raise AntigravityEgressError(
                    f"Antigravity egress credential reads {token_env}, which is "
                    "unset or empty"
                )
            token = raw
        return {credential.header_name: _render_header_value(credential.scheme, token)}

    async def resolve_network(self) -> EgressNetworkPayload | None:
        """Resolve the network block, minting every credential it names."""
        if self._config.network == "default":
            return None
        if self._config.network == "disabled":
            return "disabled"

        entries: list[EgressAllowlistEntry] = []
        for rule in self._config.allowlist:
            transform: dict[str, str] = dict(rule.headers)
            if rule.credential is not None:
                transform.update(await self._credential_header(rule.credential))
            entry: EgressAllowlistEntry = {"domain": rule.domain}
            if transform:
                # The API takes a list of flat single-header objects rather
                # than one object with several keys.
                entry["transform"] = [
                    {name: value} for name, value in transform.items()
                ]
            entries.append(entry)
        return {"allowlist": entries}
