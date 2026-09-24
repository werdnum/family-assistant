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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict

import httpx
import jwt

from family_assistant.utils.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from family_assistant.config_models import (
        AntigravityEgressCredentialConfig,
        AntigravityEnvironmentConfig,
    )
    from family_assistant.tools.types import ToolExecutionContext
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

# Where the Interactions API keeps stored credentials. The egress proxy resolves
# a stored id per outbound request rather than reading a header frozen into the
# interaction at submit, which is what lets a token change under a run that is
# already in flight.
GENERATIVE_LANGUAGE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

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

# The sandbox variable bound to the stored git credential. The sandbox sees a
# placeholder; the proxy replaces it with the current value on requests to the
# credential's trusted domains, so git can put it in its own Basic header and
# still authenticate after the token it started with has expired.
GITHUB_GIT_AUTH_ENV = "FA_GITHUB_GIT_AUTH"
# The only host a GitHub git credential may be configured for; see
# ``AntigravityEgressRuleConfig``.
GITHUB_GIT_HOST = "github.com"

EGRESS_CREDENTIAL_ROTATION_TASK_TYPE = "antigravity_egress_credential_rotation"
EGRESS_CREDENTIAL_ROTATION_TASK_ID = "system_antigravity_egress_credential_rotation"
# A third of an installation token's ~1h life, so a single missed tick still
# leaves the stored token valid until the one after it.
EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES = 20


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
    credential: str


class EgressAllowlistPayload(TypedDict):
    """The object form of ``environment.network``."""

    allowlist: list[EgressAllowlistEntry]


# ``environment.network`` is either the allowlist object or the literal string
# "disabled"; ``None`` means send no network block and take the API's default.
EgressNetworkPayload = EgressAllowlistPayload | str


class EgressEnvVar(TypedDict, total=False):
    """One ``environment.env`` entry: a stored credential's id, or a value."""

    credential: str
    value: str


@dataclass(frozen=True)
class EgressResolution:
    """What one run's egress policy adds to its sandbox."""

    network: EgressNetworkPayload | None
    env: dict[str, EgressEnvVar] = field(default_factory=dict)
    # Whether git reaches GitHub through the stored credential in ``env``,
    # which the agent has to be told to use: the proxy only substitutes the
    # placeholder, it does not add the header.
    github_git: bool = False


class EgressResolver(Protocol):
    """Resolves the egress part of the sandbox environment for one agent run."""

    async def resolve(self) -> EgressResolution:
        """Return the network block and any credential-bound variables."""
        ...


def github_git_instruction() -> str:
    """Tell the agent how to point git at the stored credential."""
    command = (
        f"git config --global --replace-all "
        f"'http.https://{GITHUB_GIT_HOST}/.extraHeader' "
        f'"Authorization: Basic ${GITHUB_GIT_AUTH_ENV}"'
    )
    return (
        "Git authentication for GitHub is arranged for you. Before your first "
        f"git command, run:\n\n{command}\n\n${GITHUB_GIT_AUTH_ENV} holds a "
        "placeholder that the network proxy swaps for a credential that stays "
        "valid for the whole task, so after that plain git clone, fetch, pull "
        "and push all work. Do not put credentials in remote URLs or configure "
        "a git credential helper for GitHub."
    )


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

    def stored_credential_id(self) -> str:
        """The store id this installation's token is written under.

        Derived from the installation rather than configured, so that two
        deployments sharing one API project collide only when they are the same
        installation -- in which case they would be writing the same token and
        the collision is harmless. An id chosen by hand could have them quietly
        authenticating as each other instead.
        """
        installation_id = self._env.get(GITHUB_APP_INSTALLATION_ID_ENV)
        if not installation_id:
            raise AntigravityEgressError(
                "GitHub App egress credential requires "
                f"{GITHUB_APP_INSTALLATION_ID_ENV}"
            )
        return f"fa-egress-github-app-{installation_id}"

    def git_credential_id(self) -> str:
        """The store id of the same token, encoded for git's Basic header."""
        return f"{self.stored_credential_id()}-git"

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


class AntigravityCredentialStore:
    """Writes minted tokens into the Interactions API credential store.

    A stored credential is referenced from an allowlist rule by id, and the
    proxy resolves that id on every outbound request. Replacing the stored
    value therefore reaches runs that are already in flight -- observed against
    the live API, not inferred from the documentation -- which is the whole
    reason a minted token goes here rather than into a submit-time header.

    The store renders every credential as ``Authorization: Bearer <token>``.
    ``header_name`` and ``prefix`` are accepted on create and then ignored on
    the wire, so nothing here offers them: a caller that needs another header
    or scheme cannot be served by the store at all and belongs on the
    ``transform`` path.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = GENERATIVE_LANGUAGE_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._owns_client = http_client is None

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=15.0)
        return self._http_client

    async def aclose(self) -> None:
        """Close the HTTP client if this store created it."""
        if self._http_client is not None and self._owns_client:
            await self._http_client.aclose()
            self._http_client = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    async def _write(
        self, credential_id: str, body: dict[str, object]
    ) -> httpx.Response:
        """Update the credential, creating it if absent; return the last response."""
        client = self._client()
        url = f"{self._base_url}/credentials/{credential_id}"
        response = await client.patch(url, headers=self._headers(), json=body)
        if response.status_code != httpx.codes.NOT_FOUND:
            return response
        response = await client.post(
            f"{self._base_url}/credentials",
            headers=self._headers(),
            json={"id": credential_id, **body},
        )
        if response.status_code != httpx.codes.CONFLICT:
            return response
        # Another writer created the id between our PATCH and POST -- the first
        # rotation tick racing the first submit, say. It exists now, so the
        # update path applies.
        return await client.patch(url, headers=self._headers(), json=body)

    async def ensure(self, credential_id: str, token: str) -> None:
        """Store ``token`` as a Bearer credential, creating the id if needed.

        Written as update-then-create rather than create-then-update because
        the steady state is a credential that already exists: every rotation
        tick and every submit after the first takes the single-request path.
        """
        await self._ensure(credential_id, {"type": "bearer_token", "token": token})

    async def ensure_substituted(
        self, credential_id: str, value: str, trusted_domains: Iterable[str]
    ) -> None:
        """Store ``value`` for placeholder substitution in request headers.

        The sandbox variable bound to it holds a placeholder; the proxy swaps in
        ``value`` only on requests to ``trusted_domains`` and refuses requests
        elsewhere that carry it. Measured against the live API, including a
        mid-run update reaching later requests of the same run.
        """
        await self._ensure(
            credential_id,
            {
                "type": "environment_variable",
                "value": value,
                "trusted_domains": list(trusted_domains),
                "injection_location": "header",
            },
        )

    async def _ensure(self, credential_id: str, body: dict[str, object]) -> None:
        try:
            response = await self._write(credential_id, body)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise AntigravityEgressError(
                f"Storing egress credential {credential_id!r} failed: "
                f"{e.response.status_code} {e.response.text}"
            ) from e
        except httpx.HTTPError as e:
            raise AntigravityEgressError(
                f"Storing egress credential {credential_id!r} failed: {e}"
            ) from e
        logger.info("Stored Antigravity egress credential %r", credential_id)

    async def delete(self, credential_id: str) -> None:
        """Remove a stored credential. Absent is success -- the end state holds."""
        try:
            response = await self._client().delete(
                f"{self._base_url}/credentials/{credential_id}",
                headers=self._headers(),
            )
            if response.status_code != httpx.codes.NOT_FOUND:
                response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise AntigravityEgressError(
                f"Deleting egress credential {credential_id!r} failed: "
                f"{e.response.status_code} {e.response.text}"
            ) from e
        except httpx.HTTPError as e:
            raise AntigravityEgressError(
                f"Deleting egress credential {credential_id!r} failed: {e}"
            ) from e


def _store_route(
    credential: AntigravityEgressCredentialConfig,
) -> Literal["rest", "git"] | None:
    """How the store carries this credential, or ``None`` if it should not.

    The store is for values that expire, so they can change while a run is in
    flight. Only a minted kind does; a static token gains nothing there and
    would sit unexpiring, where removing its rule from our config would no
    longer revoke it.

    The scheme then picks the store's mechanism. A ``bearer_token`` credential
    reaches the wire only as ``Authorization: Bearer <token>``, which is the
    REST API's form. Git over HTTPS takes only ``Basic``, so its token is
    stored pre-encoded as an ``environment_variable`` credential instead, and
    git sends the placeholder in its own ``Authorization: Basic`` header for
    the proxy to fill in.
    """
    if (
        credential.type != "github_app"
        or credential.header_name.lower() != "authorization"
    ):
        return None
    return "rest" if credential.scheme == "bearer" else "git"


@dataclass(frozen=True)
class StoredGitHubCredentials:
    """Which forms of the App's token some profile reads from the store."""

    rest: bool
    git: bool


def stored_github_credentials(
    environments: Iterable[AntigravityEnvironmentConfig | None],
) -> StoredGitHubCredentials | None:
    """What the store must hold for these sandbox environments, if anything."""
    routes = {
        _store_route(rule.credential)
        for environment in environments
        if environment is not None and environment.network == "allowlist"
        for rule in environment.allowlist
        if rule.credential is not None
    }
    if not routes & {"rest", "git"}:
        return None
    return StoredGitHubCredentials(rest="rest" in routes, git="git" in routes)


async def store_github_app_credentials(
    source: GitHubAppInstallationTokenSource,
    store: AntigravityCredentialStore,
    needs: StoredGitHubCredentials,
) -> None:
    """Write one freshly minted installation token in every form ``needs``."""
    token = await source.token()
    if needs.rest:
        await store.ensure(source.stored_credential_id(), token)
    if needs.git:
        await store.ensure_substituted(
            source.git_credential_id(), _basic_credential(token), [GITHUB_GIT_HOST]
        )


def make_egress_credential_rotation_handler(
    *,
    needs: StoredGitHubCredentials | None,
    api_key: str | None,
    # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
) -> Callable[[ToolExecutionContext, dict[str, Any]], Awaitable[None]]:
    """Bind the rotation tick to the configuration this process started with.

    ``needs`` is re-checked on every tick rather than only at seeding,
    because a recurring task seeded by an earlier configuration outlives it. A
    tick that ignored the change would keep a live GitHub credential in the
    store for a deployment that no longer uses one; returning instead lets the
    last stored token expire on its own within the hour.
    """

    async def handle_egress_credential_rotation(
        exec_context: ToolExecutionContext,
        # ast-grep-ignore: no-dict-any - task payload has varying keys per task type
        payload: dict[str, Any],
    ) -> None:
        del payload
        if needs is None:
            logger.info(
                "No profile resolves an egress credential through the store; "
                "skipping rotation."
            )
            return
        if not api_key:
            raise AntigravityEgressError(
                "Rotating the stored egress credential needs a Gemini API key "
                "(gemini_api_key / GEMINI_API_KEY)."
            )
        source = GitHubAppInstallationTokenSource(clock=exec_context.clock)
        store = AntigravityCredentialStore(api_key=api_key)
        try:
            await store_github_app_credentials(source, store, needs)
        finally:
            await source.aclose()
            await store.aclose()

    return handle_egress_credential_rotation


def _basic_credential(token: str) -> str:
    """The token as GitHub's git-over-HTTPS ``Basic`` credential, sans scheme."""
    return base64.b64encode(f"{_GITHUB_GIT_BASIC_USERNAME}:{token}".encode()).decode(
        "ascii"
    )


def _render_header_value(scheme: str, token: str) -> str:
    """Render a credential as an ``Authorization`` value in the given scheme."""
    if scheme == "basic":
        return f"Basic {_basic_credential(token)}"
    return f"Bearer {token}"


class AntigravityEgressResolver:
    """Builds ``environment.network`` from a profile's environment config."""

    def __init__(
        self,
        config: AntigravityEnvironmentConfig,
        *,
        github_app_tokens: GitHubAppInstallationTokenSource | None = None,
        credential_store: AntigravityCredentialStore | None = None,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._env = env if env is not None else os.environ
        # Created lazily so a config with no GitHub rule never constructs an
        # HTTP client or reads a key it does not need.
        self._github_app_tokens = github_app_tokens
        self._credential_store = credential_store
        self._clock = clock

    def _github_source(self) -> GitHubAppInstallationTokenSource:
        if self._github_app_tokens is None:
            self._github_app_tokens = GitHubAppInstallationTokenSource(
                clock=self._clock, env=self._env
            )
        return self._github_app_tokens

    async def aclose(self) -> None:
        """Release any HTTP client this resolver created.

        Both collaborators close only a client they built themselves, so this
        is safe whether they were injected or created lazily here.
        """
        if self._github_app_tokens is not None:
            await self._github_app_tokens.aclose()
        if self._credential_store is not None:
            await self._credential_store.aclose()

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

    async def _store_credentials(self) -> StoredGitHubCredentials | None:
        """Mint a token into the store in every form this config reads.

        ``None`` means the caller should build headers instead: a deployment
        that configured no API key for the store has no way to use one. That
        degrades a rule to the submit-time ceiling rather than failing the run,
        because the ceiling is a weaker credential, never a wider one.
        """
        needs = stored_github_credentials([self._config])
        if needs is None:
            return None
        if self._credential_store is None:
            logger.info(
                "No credential store configured; the egress credential rides a "
                "submit-time header and expires with the token it started on."
            )
            return None
        await store_github_app_credentials(
            self._github_source(), self._credential_store, needs
        )
        return needs

    async def resolve(self) -> EgressResolution:
        """Resolve the network block, minting every credential it names."""
        if self._config.network == "default":
            return EgressResolution(network=None)
        if self._config.network == "disabled":
            return EgressResolution(network="disabled")

        stored = await self._store_credentials()
        entries: list[EgressAllowlistEntry] = []
        for rule in self._config.allowlist:
            entry: EgressAllowlistEntry = {"domain": rule.domain}
            transform: dict[str, str] = dict(rule.headers)
            credential = rule.credential
            route = _store_route(credential) if credential is not None else None
            if stored is not None and route == "rest":
                entry["credential"] = self._github_source().stored_credential_id()
            elif credential is not None and (stored is None or route != "git"):
                # A stored git credential is absent here on purpose: it reaches
                # the wire through the variable git puts in its own header.
                transform.update(await self._credential_header(credential))
            if transform:
                # The API takes a list of flat single-header objects rather
                # than one object with several keys.
                entry["transform"] = [
                    {name: value} for name, value in transform.items()
                ]
            entries.append(entry)

        env: dict[str, EgressEnvVar] = {}
        github_git = stored is not None and stored.git
        if github_git:
            env[GITHUB_GIT_AUTH_ENV] = {
                "credential": self._github_source().git_credential_id()
            }
        return EgressResolution(
            network={"allowlist": entries}, env=env, github_git=github_git
        )
