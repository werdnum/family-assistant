"""Egress policy and credential injection for the Antigravity sandbox.

The point of the proxy transform is that the token reaches Google but never the
sandbox, so what matters here is the shape of the payload we build and the fact
that a credential which cannot be resolved raises instead of quietly producing
an unauthenticated run.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from family_assistant.config_models import (
    AntigravityConfig,
    AntigravityEgressCredentialConfig,
    AntigravityEnvironmentConfig,
)
from family_assistant.llm.antigravity_egress import (
    EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES,
    AntigravityCredentialStore,
    AntigravityEgressError,
    AntigravityEgressResolver,
    GitHubAppInstallationTokenSource,
    make_egress_credential_rotation_handler,
    store_github_app_credential,
    uses_stored_credential,
)
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from pathlib import Path

    from family_assistant.tools.types import ToolExecutionContext

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def rsa_private_key_pem() -> str:
    """A real RSA key, so the JWT is genuinely signed rather than stubbed."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


class _GitHubStub:
    """Stands in for GitHub's installation-token endpoint."""

    def __init__(
        self,
        *,
        token: str = "ghs_installation_token",
        expires_in: timedelta = timedelta(hours=1),
        status_code: int = 201,
    ) -> None:
        self.token = token
        self.expires_in = expires_in
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 201:
            return httpx.Response(self.status_code, json={"message": "Bad credentials"})
        return httpx.Response(
            201,
            json={
                "token": self.token,
                "expires_at": (_NOW + self.expires_in)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _github_env(private_key_pem: str) -> dict[str, str]:
    return {
        "GITHUB_APP_ID": "2376485",
        "GITHUB_APP_INSTALLATION_ID": "97135764",
        "GITHUB_APP_PRIVATE_KEY": private_key_pem,
    }


def _token_source(
    stub: _GitHubStub, env: dict[str, str], clock: MockClock
) -> GitHubAppInstallationTokenSource:
    return GitHubAppInstallationTokenSource(
        clock=clock,
        env=env,
        http_client=stub.client(),
        api_base_url="https://api.github.com",
    )


async def test_default_network_sends_no_block() -> None:
    """The shipped shape: no environment.network, so the API's own policy applies."""
    resolver = AntigravityEgressResolver(AntigravityEnvironmentConfig())
    assert await resolver.resolve_network() is None


async def test_disabled_network_is_the_literal_string() -> None:
    """The API spells 'no outbound traffic' as a string, not an empty allowlist."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig(network="disabled")
    )
    assert await resolver.resolve_network() == "disabled"


async def test_allowlist_without_credentials_omits_transform() -> None:
    """A domain with nothing to inject carries no transform key at all."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [{"domain": "pypi.org"}, {"domain": "*.pythonhosted.org"}],
        })
    )
    assert await resolver.resolve_network() == {
        "allowlist": [{"domain": "pypi.org"}, {"domain": "*.pythonhosted.org"}]
    }


async def test_static_headers_become_flat_single_header_objects() -> None:
    """The API takes a list of one-key objects, not one object with many keys."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "api.example.com",
                    "headers": {"X-Api-Version": "3", "X-Client": "fa"},
                }
            ],
        })
    )
    network = await resolver.resolve_network()
    assert network == {
        "allowlist": [
            {
                "domain": "api.example.com",
                "transform": [{"X-Api-Version": "3"}, {"X-Client": "fa"}],
            }
        ]
    }


async def test_bearer_credential_reads_its_env_var() -> None:
    """A static token is named by env var, never written into config."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "api.example.com",
                    "credential": {"type": "bearer", "token_env": "EXAMPLE_TOKEN"},
                }
            ],
        }),
        env={"EXAMPLE_TOKEN": "tok_abc"},
    )
    assert await resolver.resolve_network() == {
        "allowlist": [
            {
                "domain": "api.example.com",
                "transform": [{"Authorization": "Bearer tok_abc"}],
            }
        ]
    }


async def test_bearer_credential_with_unset_env_var_raises() -> None:
    """An unset token would otherwise produce a silently unauthenticated run."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "api.example.com",
                    "credential": {"type": "bearer", "token_env": "EXAMPLE_TOKEN"},
                }
            ],
        }),
        env={},
    )
    with pytest.raises(AntigravityEgressError, match="EXAMPLE_TOKEN"):
        await resolver.resolve_network()


async def test_custom_header_name_is_honoured() -> None:
    """Not every API authenticates via Authorization."""
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "api.example.com",
                    "credential": {
                        "type": "bearer",
                        "token_env": "EXAMPLE_TOKEN",
                        "header_name": "X-Api-Key",
                    },
                }
            ],
        }),
        env={"EXAMPLE_TOKEN": "tok_abc"},
    )
    network = await resolver.resolve_network()
    assert network == {
        "allowlist": [
            {
                "domain": "api.example.com",
                "transform": [{"X-Api-Key": "Bearer tok_abc"}],
            }
        ]
    }


async def test_github_app_credential_injects_a_minted_token(
    rsa_private_key_pem: str,
) -> None:
    """The App key stays here; only the installation token reaches the proxy."""
    stub = _GitHubStub()
    env = _github_env(rsa_private_key_pem)
    clock = MockClock(_NOW)
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {"domain": "*"},
                {
                    "domain": "api.github.com",
                    "credential": {"type": "github_app"},
                },
            ],
        }),
        github_app_tokens=_token_source(stub, env, clock),
    )

    network = await resolver.resolve_network()

    assert network == {
        "allowlist": [
            {"domain": "*"},
            {
                "domain": "api.github.com",
                "transform": [{"Authorization": "Bearer ghs_installation_token"}],
            },
        ]
    }
    assert len(stub.requests) == 1
    assert stub.requests[0].url.path == ("/app/installations/97135764/access_tokens")
    # The exchange itself is authenticated with the signed App JWT, not the key.
    authorization = stub.requests[0].headers["Authorization"]
    assert authorization.startswith("Bearer ey")
    assert rsa_private_key_pem not in authorization


async def test_github_app_basic_scheme_encodes_for_git_over_https(
    rsa_private_key_pem: str,
) -> None:
    """git authenticates as HTTP Basic with the x-access-token username."""
    stub = _GitHubStub()
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "github.com",
                    "credential": {"type": "github_app", "scheme": "basic"},
                }
            ],
        }),
        github_app_tokens=_token_source(
            stub, _github_env(rsa_private_key_pem), MockClock(_NOW)
        ),
    )

    network = await resolver.resolve_network()

    expected = base64.b64encode(b"x-access-token:ghs_installation_token").decode(
        "ascii"
    )
    assert network == {
        "allowlist": [
            {
                "domain": "github.com",
                "transform": [{"Authorization": f"Basic {expected}"}],
            }
        ]
    }


async def test_one_submissions_rules_share_a_single_token(
    rsa_private_key_pem: str,
) -> None:
    """A config naming github_app on several domains must not split tokens."""
    stub = _GitHubStub()
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [
                {
                    "domain": "github.com",
                    "credential": {"type": "github_app", "scheme": "basic"},
                },
                {"domain": "api.github.com", "credential": {"type": "github_app"}},
            ],
        }),
        github_app_tokens=_token_source(
            stub, _github_env(rsa_private_key_pem), MockClock(_NOW)
        ),
    )

    await resolver.resolve_network()

    assert len(stub.requests) == 1


async def test_each_run_mints_a_full_lifetime_token(
    rsa_private_key_pem: str,
) -> None:
    """The proxy freezes one header for a whole run, so a stale token is a trap.

    Reusing by "not yet expired" would let a long run start on a token with
    minutes left and lose GitHub partway through -- including on a final push,
    after all the work. A later run therefore re-mints rather than inheriting
    the remaining life of an earlier one's token.
    """
    stub = _GitHubStub(expires_in=timedelta(hours=1))
    clock = MockClock(_NOW)
    source = _token_source(stub, _github_env(rsa_private_key_pem), clock)

    assert await source.token() == "ghs_installation_token"

    stub.token = "ghs_second_token"
    clock.advance(timedelta(minutes=50))
    assert await source.token() == "ghs_second_token"
    assert len(stub.requests) == 2


async def test_missing_app_id_raises(rsa_private_key_pem: str) -> None:
    """A half-configured App fails at submit, not as a 404 inside the agent."""
    stub = _GitHubStub()
    env = _github_env(rsa_private_key_pem)
    del env["GITHUB_APP_ID"]
    source = _token_source(stub, env, MockClock(_NOW))

    with pytest.raises(AntigravityEgressError, match="GITHUB_APP_ID"):
        await source.token()
    assert stub.requests == []


async def test_missing_private_key_raises() -> None:
    """Neither the inline PEM nor a path naming one was set."""
    stub = _GitHubStub()
    source = _token_source(
        stub,
        {"GITHUB_APP_ID": "2376485", "GITHUB_APP_INSTALLATION_ID": "97135764"},
        MockClock(_NOW),
    )

    with pytest.raises(AntigravityEgressError, match="GITHUB_APP_PRIVATE_KEY"):
        await source.token()


async def test_private_key_path_is_read_from_disk(
    rsa_private_key_pem: str, tmp_path: Path
) -> None:
    """The deployed shape mounts the key as a file, as k8s secrets do."""
    key_file = tmp_path / "private-key.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")
    stub = _GitHubStub()
    source = _token_source(
        stub,
        {
            "GITHUB_APP_ID": "2376485",
            "GITHUB_APP_INSTALLATION_ID": "97135764",
            "GITHUB_APP_PRIVATE_KEY_PATH": str(key_file),
        },
        MockClock(_NOW),
    )

    assert await source.token() == "ghs_installation_token"


async def test_github_rejection_raises_rather_than_running_unauthenticated(
    rsa_private_key_pem: str,
) -> None:
    """A revoked installation is a credential error, not a quiet degradation."""
    stub = _GitHubStub(status_code=401)
    source = _token_source(stub, _github_env(rsa_private_key_pem), MockClock(_NOW))

    with pytest.raises(AntigravityEgressError, match="401"):
        await source.token()


async def test_app_jwt_claims_are_backdated_and_short_lived(
    rsa_private_key_pem: str,
) -> None:
    """GitHub rejects an iat in its own future and caps the JWT at 10 minutes."""
    stub = _GitHubStub()
    source = _token_source(stub, _github_env(rsa_private_key_pem), MockClock(_NOW))
    await source.token()

    encoded = stub.requests[0].headers["Authorization"].removeprefix("Bearer ")
    payload_segment = encoded.split(".")[1]
    padding = "=" * (-len(payload_segment) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload_segment + padding))

    now_ts = int(_NOW.timestamp())
    assert claims["iss"] == "2376485"
    assert claims["iat"] < now_ts
    assert 0 < claims["exp"] - now_ts <= 600


def test_bearer_credential_requires_a_token_env() -> None:
    """Otherwise the rule builds and injects nothing, which is not an error later."""
    with pytest.raises(ValidationError, match="requires 'token_env'"):
        AntigravityEgressCredentialConfig.model_validate({"type": "bearer"})


def test_github_app_credential_rejects_a_token_env() -> None:
    """It mints its own token, so a token_env here would be silently ignored."""
    with pytest.raises(ValidationError, match="does not read 'token_env'"):
        AntigravityEgressCredentialConfig.model_validate({
            "type": "github_app",
            "token_env": "SOME_TOKEN",
        })


def test_allowlist_mode_requires_entries() -> None:
    """An empty allowlist reads as 'reach nothing', which 'disabled' says plainly."""
    with pytest.raises(ValidationError, match="empty allowlist"):
        AntigravityEnvironmentConfig.model_validate({"network": "allowlist"})


@pytest.mark.parametrize("network", ["default", "disabled"])
def test_allowlist_outside_allowlist_mode_is_rejected(network: str) -> None:
    """A discarded allowlist looks like a configured one until a run misbehaves."""
    with pytest.raises(ValidationError, match="allowlist would be discarded"):
        AntigravityEnvironmentConfig.model_validate({
            "network": network,
            "allowlist": [{"domain": "github.com"}],
        })


def test_environment_is_optional_on_antigravity_config() -> None:
    """The shipped profile configures no environment at all."""
    assert AntigravityConfig().environment is None


class _CredentialStoreStub:
    """Stands in for the Interactions API credential store."""

    def __init__(self, *, existing: set[str] | None = None) -> None:
        self.existing = existing if existing is not None else set()
        self.requests: list[httpx.Request] = []
        self.failure: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            return httpx.Response(self.failure, json={"error": {"message": "nope"}})
        credential_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PATCH":
            if credential_id not in self.existing:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(200, json={"id": credential_id, "status": "active"})
        if request.method == "POST":
            body = json.loads(request.content)
            self.existing.add(body["id"])
            return httpx.Response(200, json={"id": body["id"], "status": "active"})
        if request.method == "DELETE":
            if credential_id not in self.existing:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            self.existing.discard(credential_id)
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected method {request.method}")

    def store(self) -> AntigravityCredentialStore:
        return AntigravityCredentialStore(
            api_key="test-api-key",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
        )


async def test_storing_an_absent_credential_creates_it() -> None:
    """First run for a deployment: the PATCH 404s and the POST establishes it."""
    stub = _CredentialStoreStub()
    await stub.store().ensure("fa-coder-github", "ghs_token")

    assert [r.method for r in stub.requests] == ["PATCH", "POST"]
    assert json.loads(stub.requests[1].content) == {
        "id": "fa-coder-github",
        "type": "bearer_token",
        "token": "ghs_token",
    }


async def test_storing_an_existing_credential_takes_one_request() -> None:
    """The steady state -- every rotation tick after the first -- is a lone PATCH."""
    stub = _CredentialStoreStub(existing={"fa-coder-github"})
    await stub.store().ensure("fa-coder-github", "ghs_rotated")

    assert [r.method for r in stub.requests] == ["PATCH"]
    assert json.loads(stub.requests[0].content) == {
        "type": "bearer_token",
        "token": "ghs_rotated",
    }


async def test_stored_credential_carries_no_header_name_or_prefix() -> None:
    """The proxy ignores both, so offering them would advertise a lie.

    Measured against the live API: a credential created with
    ``header_name: "X-Custom-Hdr"`` and ``prefix: "Basic"`` still arrived as
    ``authorization: "Bearer <token>"``.
    """
    stub = _CredentialStoreStub(existing={"fa-coder-github"})
    await stub.store().ensure("fa-coder-github", "ghs_token")

    body = json.loads(stub.requests[0].content)
    assert "header_name" not in body
    assert "prefix" not in body


async def test_the_api_key_travels_as_a_header_not_a_query_parameter() -> None:
    """A key in the URL lands in logs and proxy access records; a header does not."""
    stub = _CredentialStoreStub(existing={"fa-coder-github"})
    await stub.store().ensure("fa-coder-github", "ghs_token")

    request = stub.requests[0]
    assert request.headers["x-goog-api-key"] == "test-api-key"
    assert "test-api-key" not in str(request.url)


async def test_a_refused_store_write_raises() -> None:
    """A run that reaches a private repo unauthenticated fails deep inside the
    agent as a 404, which reads as confusion rather than a credential problem."""
    stub = _CredentialStoreStub(existing={"fa-coder-github"})
    stub.failure = 403
    with pytest.raises(AntigravityEgressError, match="403"):
        await stub.store().ensure("fa-coder-github", "ghs_token")


async def test_deleting_an_absent_credential_succeeds() -> None:
    """Delete states an end state; a credential already gone satisfies it."""
    stub = _CredentialStoreStub()
    await stub.store().delete("fa-coder-github")

    assert [r.method for r in stub.requests] == ["DELETE"]


def _github_rule(scheme: str, domain: str) -> dict[str, object]:
    return {
        "domain": domain,
        "credential": {"type": "github_app", "scheme": scheme},
    }


async def _resolve_with_store(
    rules: list[dict[str, object]],
    stub: _CredentialStoreStub,
    github: _GitHubStub,
    env: dict[str, str],
    clock: MockClock,
) -> object:
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": rules,
        }),
        github_app_tokens=_token_source(github, env, clock),
        credential_store=stub.store(),
        env=env,
    )
    return await resolver.resolve_network()


async def test_a_bearer_github_rule_carries_a_stored_credential_id(
    rsa_private_key_pem: str,
) -> None:
    """The REST rule: the store can render it, so it gains mid-run refresh."""
    env = _github_env(rsa_private_key_pem)
    stub = _CredentialStoreStub()
    github = _GitHubStub()

    payload = await _resolve_with_store(
        [_github_rule("bearer", "api.github.com")], stub, github, env, MockClock(_NOW)
    )

    assert payload == {
        "allowlist": [
            {
                "domain": "api.github.com",
                "credential": "fa-egress-github-app-97135764",
            }
        ]
    }
    assert json.loads(stub.requests[-1].content)["token"] == "ghs_installation_token"


async def test_a_basic_github_rule_keeps_its_header(
    rsa_private_key_pem: str,
) -> None:
    """Git-over-HTTPS needs Basic, which the store cannot emit at any encoding.

    So the git rule stays on the transform path and keeps that path's ceiling.
    """
    env = _github_env(rsa_private_key_pem)
    stub = _CredentialStoreStub()
    github = _GitHubStub()

    payload = await _resolve_with_store(
        [_github_rule("basic", "github.com")], stub, github, env, MockClock(_NOW)
    )

    expected = base64.b64encode(b"x-access-token:ghs_installation_token").decode()
    assert payload == {
        "allowlist": [
            {
                "domain": "github.com",
                "transform": [{"Authorization": f"Basic {expected}"}],
            }
        ]
    }
    assert stub.requests == []


async def test_both_schemes_together_split_by_mechanism(
    rsa_private_key_pem: str,
) -> None:
    """The shipped shape for a credentialed deployment: REST stored, git not."""
    env = _github_env(rsa_private_key_pem)
    stub = _CredentialStoreStub()
    github = _GitHubStub()

    payload = await _resolve_with_store(
        [
            _github_rule("bearer", "api.github.com"),
            _github_rule("basic", "github.com"),
            {"domain": "*"},
        ],
        stub,
        github,
        env,
        MockClock(_NOW),
    )

    assert isinstance(payload, dict)
    entries = payload["allowlist"]
    assert "credential" in entries[0]
    assert "transform" not in entries[0]
    assert "transform" in entries[1]
    assert "credential" not in entries[1]
    assert entries[2] == {"domain": "*"}


async def test_a_static_bearer_never_reaches_the_store(
    rsa_private_key_pem: str,
) -> None:
    """A static token would sit in the store unexpiring, where removing its
    rule from our config would no longer revoke it."""
    env = {**_github_env(rsa_private_key_pem), "SOME_TOKEN": "static-value"}
    stub = _CredentialStoreStub()
    github = _GitHubStub()

    payload = await _resolve_with_store(
        [
            {
                "domain": "api.example.com",
                "credential": {"type": "bearer", "token_env": "SOME_TOKEN"},
            }
        ],
        stub,
        github,
        env,
        MockClock(_NOW),
    )

    assert payload == {
        "allowlist": [
            {
                "domain": "api.example.com",
                "transform": [{"Authorization": "Bearer static-value"}],
            }
        ]
    }
    assert stub.requests == []


async def test_without_a_store_a_bearer_rule_falls_back_to_a_header(
    rsa_private_key_pem: str,
) -> None:
    """A deployment with no store keeps working, at the submit-time ceiling.

    The ceiling is a weaker credential, never a wider one, so degrading here
    costs availability rather than safety.
    """
    env = _github_env(rsa_private_key_pem)
    resolver = AntigravityEgressResolver(
        AntigravityEnvironmentConfig.model_validate({
            "network": "allowlist",
            "allowlist": [_github_rule("bearer", "api.github.com")],
        }),
        github_app_tokens=_token_source(_GitHubStub(), env, MockClock(_NOW)),
        env=env,
    )

    assert await resolver.resolve_network() == {
        "allowlist": [
            {
                "domain": "api.github.com",
                "transform": [{"Authorization": "Bearer ghs_installation_token"}],
            }
        ]
    }


async def test_the_stored_id_names_the_installation_it_authenticates_as(
    rsa_private_key_pem: str,
) -> None:
    """Two deployments on one API project collide only when they are the same
    installation -- where they would be writing the same token anyway."""
    clock = MockClock(_NOW)
    first = _token_source(_GitHubStub(), _github_env(rsa_private_key_pem), clock)
    other_env = {**_github_env(rsa_private_key_pem), "GITHUB_APP_INSTALLATION_ID": "42"}
    second = _token_source(_GitHubStub(), other_env, clock)

    assert first.stored_credential_id() == "fa-egress-github-app-97135764"
    assert second.stored_credential_id() == "fa-egress-github-app-42"


def _environment(rules: list[dict[str, object]]) -> AntigravityEnvironmentConfig:
    return AntigravityEnvironmentConfig.model_validate({
        "network": "allowlist",
        "allowlist": rules,
    })


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (None, False),
        (AntigravityEnvironmentConfig(), False),
        (_environment([{"domain": "pypi.org"}]), False),
        (_environment([_github_rule("basic", "github.com")]), False),
        (
            _environment([
                {
                    "domain": "api.example.com",
                    "credential": {"type": "bearer", "token_env": "SOME_TOKEN"},
                }
            ]),
            False,
        ),
        (
            _environment([
                _github_rule("basic", "github.com"),
                _github_rule("bearer", "api.github.com"),
            ]),
            True,
        ),
    ],
)
def test_rotation_is_needed_only_where_a_rule_is_stored(
    environment: AntigravityEnvironmentConfig | None, expected: bool
) -> None:
    """The same predicate that routes a rule to the store decides whether to
    rotate, so a deployment is never rotating a credential no rule reads."""
    assert uses_stored_credential(environment) is expected


class _MintingGitHubStub(_GitHubStub):
    """Hands out a different token on every mint, as GitHub does."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.token = f"ghs_minted_{len(self.requests) + 1}"
        return super().handler(request)


async def test_each_rotation_tick_replaces_the_stored_token(
    rsa_private_key_pem: str,
) -> None:
    """Ticks a rotation interval apart each mint afresh and overwrite the id.

    The stored value is what a running interaction's proxy reads per request,
    so every tick is what keeps a run longer than one token's life on GitHub.
    """
    clock = MockClock(_NOW)
    github = _MintingGitHubStub()
    source = _token_source(github, _github_env(rsa_private_key_pem), clock)
    stub = _CredentialStoreStub()
    store = stub.store()

    for _ in range(3):
        await store_github_app_credential(source, store)
        clock.advance(timedelta(minutes=EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES))

    # The first tick's PATCH finds nothing and the POST creates the id; every
    # later tick is a lone PATCH carrying a token no earlier tick wrote.
    assert [(r.method, json.loads(r.content)["token"]) for r in stub.requests] == [
        ("PATCH", "ghs_minted_1"),
        ("POST", "ghs_minted_1"),
        ("PATCH", "ghs_minted_2"),
        ("PATCH", "ghs_minted_3"),
    ]
    assert stub.existing == {"fa-egress-github-app-97135764"}


def test_the_interval_leaves_room_for_a_missed_tick() -> None:
    """One missed tick must not let the stored token expire before the next."""
    assert 2 * EGRESS_CREDENTIAL_ROTATION_INTERVAL_MINUTES < 60


async def test_rotation_does_nothing_when_no_profile_stores_a_credential() -> None:
    """A seeded task outlives the configuration that seeded it; the tick must
    not keep a live GitHub token in the store for a deployment that dropped it."""
    handler = make_egress_credential_rotation_handler(
        rotation_needed=False, api_key="test-api-key"
    )
    # The handler returns before touching the context, the network or GitHub.
    await handler(cast("ToolExecutionContext", object()), {})


async def test_rotation_without_an_api_key_fails_visibly() -> None:
    handler = make_egress_credential_rotation_handler(
        rotation_needed=True, api_key=None
    )
    with pytest.raises(AntigravityEgressError, match="API key"):
        await handler(cast("ToolExecutionContext", object()), {})
