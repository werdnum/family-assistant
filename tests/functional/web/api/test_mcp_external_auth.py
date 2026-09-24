"""The MCP adapter signing clients in with an external authorization server.

With ``mcp_adapter.authorization_server`` set, the resource metadata names that
issuer, the built-in OAuth endpoints are off, and ``/api/mcp`` accepts the
issuer's signed access tokens. The issuer here is a key pair and a JWKS the test
controls, standing in for Keycloak.
"""

import time
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from typing import Any

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import (
    AppConfig,
    MCPAdapterConfig,
    MCPExternalAuthorizationServer,
)
from family_assistant.web import auth, mcp_external_tokens
from family_assistant.web.mcp_adapter import install_mcp_adapter
from family_assistant.web.route_auth import MCP_CONSENT_PATH

SERVER_URL = "http://localhost:8000"
ISSUER = "https://id.example.com/realms/household"
AUDIENCE = "family-assistant-mcp"
KEY_ID = "test-key"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/api/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
SERVER = MCPExternalAuthorizationServer(
    issuer=ISSUER,
    jwks_uri="https://id.example.com/realms/household/certs",
    audience=AUDIENCE,
)


class _StaticJWKClient(jwt.PyJWKClient):
    """The issuer's key set, served from memory instead of over HTTP.

    Caches what it serves the way the SDK's own fetch does.
    """

    def __init__(self, jwks: dict[str, list[dict[str, str]]]) -> None:
        super().__init__(SERVER.jwks_uri)
        self._jwks = jwks
        self.fetches = 0

    def fetch_data(self) -> Any:  # noqa: ANN401 - overrides the SDK's untyped method
        self.fetches += 1
        jwk_set: Any = self._jwks
        if self.jwk_set_cache is not None:
            self.jwk_set_cache.put(jwk_set)
        return jwk_set


@pytest.fixture(scope="module")
def issuer_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(key: RSAPrivateKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "kc-user-1",
        "email": "andrew@example.com",
        "azp": "family-assistant-claude-code",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KEY_ID})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **MCP_HEADERS}


@pytest_asyncio.fixture
async def client(
    app_fixture: FastAPI,
    db_engine: AsyncEngine,
    issuer_key: RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """The test app with the adapter signing clients in through ``SERVER``."""
    app_fixture.state.config = AppConfig(
        database_url=str(db_engine.url),
        server_url=SERVER_URL,
        mcp_adapter=MCPAdapterConfig(enabled=True, authorization_server=SERVER),
    )
    jwk = RSAAlgorithm.to_jwk(issuer_key.public_key(), as_dict=True)
    app_fixture.state.mcp_external_token_verifier = (
        mcp_external_tokens.ExternalTokenVerifier(
            server=SERVER,
            jwks=_StaticJWKClient({"keys": [{**jwk, "kid": KEY_ID, "use": "sig"}]}),
        )
    )
    monkeypatch.setattr(app_fixture.state.auth_service, "auth_enabled", True)
    app_fixture.router.routes[:] = [
        route
        for route in app_fixture.router.routes
        if getattr(route, "name", None) != "mcp_adapter"
    ]
    install_mcp_adapter(app_fixture)
    async with AsyncClient(
        transport=ASGITransport(app=app_fixture), base_url="http://testserver"
    ) as http:
        yield http


def mcp_running(app: FastAPI) -> AbstractAsyncContextManager[None]:
    return app.state.mcp_adapter.run()


@pytest.mark.asyncio
async def test_resource_metadata_names_the_external_issuer(
    client: AsyncClient,
) -> None:
    response = await client.get(RESOURCE_METADATA_PATH)

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{SERVER_URL}/api/mcp"
    assert metadata["authorization_servers"] == [ISSUER]
    assert metadata["scopes_supported"] == SERVER.scopes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", f"{MCP_CONSENT_PATH}?request_id=anything"),
        ("POST", MCP_CONSENT_PATH),
        ("GET", "/authorize"),
        ("POST", "/token"),
        ("POST", "/register"),
        ("POST", "/revoke"),
    ],
)
async def test_builtin_authorization_server_is_off(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, follow_redirects=False)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_issuer_token_reaches_the_mcp_endpoint(
    client: AsyncClient, app_fixture: FastAPI, issuer_key: RSAPrivateKey
) -> None:
    async with mcp_running(app_fixture):
        response = await client.post(
            "/api/mcp", json=TOOLS_LIST, headers=_bearer(_mint(issuer_key))
        )

    assert response.status_code == 200, response.text
    assert "tools" in response.json()["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "some-other-service"},
        {"exp": int(time.time()) - 60},
    ],
    ids=["wrong-audience", "expired"],
)
async def test_invalid_issuer_token_is_challenged(
    client: AsyncClient, issuer_key: RSAPrivateKey, overrides: dict[str, object]
) -> None:
    response = await client.post(
        "/api/mcp", json=TOOLS_LIST, headers=_bearer(_mint(issuer_key, **overrides))
    )

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert f'resource_metadata="{SERVER_URL}{RESOURCE_METADATA_PATH}"' in challenge


@pytest.mark.asyncio
async def test_token_signed_by_another_key_is_rejected(client: AsyncClient) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    response = await client.post(
        "/api/mcp", json=TOOLS_LIST, headers=_bearer(_mint(other_key))
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_key_ids_do_not_refetch_the_key_set_each_time(
    client: AsyncClient, app_fixture: FastAPI, issuer_key: RSAPrivateKey
) -> None:
    jwks = app_fixture.state.mcp_external_token_verifier.jwks
    tokens = [
        jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": int(time.time()) + 60},
            issuer_key,
            algorithm="RS256",
            headers={"kid": f"made-up-{index}"},
        )
        for index in range(5)
    ]

    for token in tokens:
        await client.post("/api/mcp", json=TOOLS_LIST, headers=_bearer(token))

    # The first fetch fills the cache and the first miss refreshes it once.
    assert jwks.fetches == 2


@pytest.mark.asyncio
async def test_issuer_token_is_rejected_outside_the_mcp_endpoint(
    client: AsyncClient, issuer_key: RSAPrivateKey
) -> None:
    response = await client.get("/api/me/tokens", headers=_bearer(_mint(issuer_key)))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_email_outside_the_allowlist_is_rejected(
    client: AsyncClient, issuer_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "ALLOWED_OIDC_EMAILS", "someone-else@example.com")

    response = await client.post(
        "/api/mcp", json=TOOLS_LIST, headers=_bearer(_mint(issuer_key))
    )

    assert response.status_code == 401
