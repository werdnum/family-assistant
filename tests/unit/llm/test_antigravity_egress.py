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
from typing import TYPE_CHECKING

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
    AntigravityEgressError,
    AntigravityEgressResolver,
    GitHubAppInstallationTokenSource,
)
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from pathlib import Path

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
