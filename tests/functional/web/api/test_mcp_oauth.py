"""The MCP adapter's OAuth authorization server (docs/design/mcp-adapter.md).

Walks the flow a claude.ai connector runs over HTTP: discover the authorization
server from the protected-resource metadata, register, authorize with PKCE,
consent, exchange, use the token at ``/api/mcp``, refresh and revoke.
"""

import hashlib
import re
import secrets
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from family_assistant.config_models import AppConfig, MCPAdapterConfig
from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web.mcp_adapter import install_mcp_adapter

ISSUER = "http://localhost:8000"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource/api/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
# The SDK's revocation request requires the field even for a public client.
PUBLIC_CLIENT = {"client_secret": ""}


def _configure(app: FastAPI, db_engine: AsyncEngine, enabled: bool) -> None:
    app.state.config = AppConfig(
        database_url=str(db_engine.url),
        server_url=ISSUER,
        mcp_adapter=MCPAdapterConfig(enabled=enabled),
    )


@pytest_asyncio.fixture
async def client(
    app_fixture: FastAPI, db_engine: AsyncEngine
) -> AsyncGenerator[AsyncClient]:
    """A client over the test app with the adapter enabled.

    The ``/api/mcp`` mount copied from ``actual_app`` points at an adapter whose
    session manager belongs to the live server's loop, so it is replaced by one
    installed here; a test that reaches the endpoint runs that session manager
    with ``mcp_running`` because the anyio task group inside must open and
    close in one task. A session is added so the consent page's CSRF nonce is
    exercised.
    """
    _configure(app_fixture, db_engine, enabled=True)
    app_fixture.add_middleware(SessionMiddleware, secret_key="test-secret")
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
    """The installed adapter's session manager, for tests that call ``/api/mcp``."""
    return app.state.mcp_adapter.run()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _register(client: AsyncClient) -> dict:
    response = await client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "client_name": "Claude",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _authorize(
    client: AsyncClient, client_id: str, challenge: str, state: str = "xyz"
) -> str:
    """Start authorization; returns the consent page URL the user is sent to."""
    response = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "assistant",
            "resource": f"{ISSUER}/api/mcp",
        },
    )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("/mcp/consent?request_id=")
    return location


async def _decide(client: AsyncClient, consent_url: str, decision: str) -> Response:
    page = await client.get(consent_url)
    assert page.status_code == 200, page.text
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text)
    assert nonce is not None
    request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
    return await client.post(
        "/mcp/consent",
        data={"request_id": request_id, "decision": decision, "nonce": nonce.group(1)},
    )


def _code_from(redirect: Response) -> str:
    assert redirect.status_code == 302, redirect.text
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    return query["code"][0]


async def _exchange(
    client: AsyncClient, client_id: str, code: str, verifier: str
) -> Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )


async def _grant(client: AsyncClient) -> tuple[str, dict]:
    """Register, authorize and approve; returns the client id and the tokens."""
    registration = await _register(client)
    verifier, challenge = _pkce_pair()
    consent_url = await _authorize(client, registration["client_id"], challenge)
    code = _code_from(await _decide(client, consent_url, "approve"))
    tokens = await _exchange(client, registration["client_id"], code, verifier)
    assert tokens.status_code == 200, tokens.text
    return registration["client_id"], tokens.json()


async def _refresh(client: AsyncClient, client_id: str, refresh_token: str) -> Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **MCP_HEADERS}


async def _token_rows(db_engine: AsyncEngine) -> list[dict]:
    return await Database(db_engine).fetch_all(
        select(api_tokens_table).order_by(api_tokens_table.c.id)
    )


@pytest.mark.asyncio
async def test_protected_resource_metadata_names_issuer(client: AsyncClient) -> None:
    response = await client.get(RESOURCE_METADATA_PATH)

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["resource"] == f"{ISSUER}/api/mcp"
    assert metadata["authorization_servers"] == [f"{ISSUER}/"]
    assert metadata["scopes_supported"] == ["assistant"]
    assert metadata["bearer_methods_supported"] == ["header"]


@pytest.mark.asyncio
async def test_authorization_server_metadata_advertises_the_flow(
    client: AsyncClient,
) -> None:
    resource = (await client.get(RESOURCE_METADATA_PATH)).json()
    issuer = resource["authorization_servers"][0]
    assert issuer == f"{ISSUER}/"

    response = await client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == issuer
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert metadata["registration_endpoint"] == f"{ISSUER}/register"
    assert metadata["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert metadata["token_endpoint"] == f"{ISSUER}/token"
    assert metadata["revocation_endpoint"] == f"{ISSUER}/revoke"
    assert "refresh_token" in metadata["grant_types_supported"]


@pytest.mark.asyncio
async def test_consent_page_names_the_client(client: AsyncClient) -> None:
    registration = await _register(client)
    _, challenge = _pkce_pair()
    consent_url = await _authorize(client, registration["client_id"], challenge)

    page = await client.get(consent_url)

    assert page.status_code == 200
    assert "Claude" in page.text
    assert "claude.ai" in page.text
    assert 'value="approve"' in page.text
    assert 'value="deny"' in page.text


@pytest.mark.asyncio
async def test_approval_redirects_to_client_with_code_and_state(
    client: AsyncClient,
) -> None:
    registration = await _register(client)
    _, challenge = _pkce_pair()
    consent_url = await _authorize(
        client, registration["client_id"], challenge, state="s-42"
    )

    redirect = await _decide(client, consent_url, "approve")

    assert redirect.status_code == 302
    location = urlparse(redirect.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT_URI
    query = parse_qs(location.query)
    assert query["state"] == ["s-42"]
    assert len(query["code"][0]) >= 32


@pytest.mark.asyncio
async def test_denial_redirects_to_client_with_access_denied(
    client: AsyncClient,
) -> None:
    registration = await _register(client)
    _, challenge = _pkce_pair()
    consent_url = await _authorize(
        client, registration["client_id"], challenge, state="s-7"
    )

    redirect = await _decide(client, consent_url, "deny")

    assert redirect.status_code == 302
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["s-7"]
    assert "code" not in query


@pytest.mark.asyncio
async def test_exchange_issues_an_mcp_token_pair(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    client_id, tokens = await _grant(client)

    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 24 * 3600
    assert tokens["scope"] == "assistant"
    rows = await _token_rows(db_engine)
    access, refresh = rows[-2], rows[-1]
    assert access["token_type"] == "mcp"
    assert access["oauth_client_id"] == client_id
    assert access["user_identifier"] == "test_user"
    assert access["name"] == "Claude (MCP connector)"
    assert access["prefix"] == tokens["access_token"][:8]
    assert refresh["token_type"] == "refresh"
    assert refresh["parent_token_id"] == access["id"]
    assert refresh["prefix"] == tokens["refresh_token"][:8]


@pytest.mark.asyncio
async def test_access_token_reaches_the_mcp_endpoint(
    client: AsyncClient, app_fixture: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tokens = await _grant(client)
    monkeypatch.setattr(app_fixture.state.auth_service, "auth_enabled", True)

    async with mcp_running(app_fixture):
        response = await client.post(
            "/api/mcp", json=TOOLS_LIST, headers=_bearer(tokens["access_token"])
        )

    assert response.status_code == 200, response.text
    assert "tools" in response.json()["result"]


@pytest.mark.asyncio
async def test_access_token_is_rejected_outside_the_mcp_endpoint(
    client: AsyncClient, app_fixture: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, tokens = await _grant(client)
    monkeypatch.setattr(app_fixture.state.auth_service, "auth_enabled", True)

    response = await client.get(
        "/api/me/tokens", headers=_bearer(tokens["access_token"])
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_both_tokens(
    client: AsyncClient, app_fixture: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id, tokens = await _grant(client)

    rotated = await _refresh(client, client_id, tokens["refresh_token"])

    assert rotated.status_code == 200, rotated.text
    new_tokens = rotated.json()
    assert new_tokens["access_token"] != tokens["access_token"]
    assert new_tokens["refresh_token"] != tokens["refresh_token"]
    monkeypatch.setattr(app_fixture.state.auth_service, "auth_enabled", True)
    async with mcp_running(app_fixture):
        old = await client.post(
            "/api/mcp", json=TOOLS_LIST, headers=_bearer(tokens["access_token"])
        )
        assert old.status_code == 401
        new = await client.post(
            "/api/mcp", json=TOOLS_LIST, headers=_bearer(new_tokens["access_token"])
        )
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_refresh_token_is_single_use(client: AsyncClient) -> None:
    client_id, tokens = await _grant(client)
    assert (
        await _refresh(client, client_id, tokens["refresh_token"])
    ).status_code == 200

    replay = await _refresh(client, client_id, tokens["refresh_token"])

    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_token_is_bound_to_its_client(client: AsyncClient) -> None:
    _, tokens = await _grant(client)
    other = await _register(client)

    response = await _refresh(client, other["client_id"], tokens["refresh_token"])

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_revoking_the_access_token_revokes_its_refresh_token(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    client_id, tokens = await _grant(client)

    response = await client.post(
        "/revoke",
        data={"token": tokens["access_token"], "client_id": client_id, **PUBLIC_CLIENT},
    )

    assert response.status_code == 200, response.text
    access, refresh = (await _token_rows(db_engine))[-2:]
    assert access["is_revoked"] is True
    assert refresh["is_revoked"] is True
    refreshed = await _refresh(client, client_id, tokens["refresh_token"])
    assert refreshed.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_revoking_the_refresh_token_revokes_the_access_token(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    client_id, tokens = await _grant(client)

    response = await client.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": client_id,
            **PUBLIC_CLIENT,
        },
    )

    assert response.status_code == 200
    access, refresh = (await _token_rows(db_engine))[-2:]
    assert access["is_revoked"] is True
    assert refresh["is_revoked"] is True


@pytest.mark.asyncio
async def test_wrong_code_verifier_is_rejected(client: AsyncClient) -> None:
    registration = await _register(client)
    _, challenge = _pkce_pair()
    consent_url = await _authorize(client, registration["client_id"], challenge)
    code = _code_from(await _decide(client, consent_url, "approve"))

    response = await _exchange(
        client, registration["client_id"], code, "not-the-verifier"
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_authorization_code_is_single_use(client: AsyncClient) -> None:
    registration = await _register(client)
    verifier, challenge = _pkce_pair()
    consent_url = await _authorize(client, registration["client_id"], challenge)
    code = _code_from(await _decide(client, consent_url, "approve"))
    assert (
        await _exchange(client, registration["client_id"], code, verifier)
    ).status_code == 200

    replay = await _exchange(client, registration["client_id"], code, verifier)

    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_unknown_consent_request_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/mcp/consent", params={"request_id": "nope"})

    assert response.status_code == 400
    assert "expired" in response.text


@pytest.mark.asyncio
async def test_consent_decision_needs_the_page_nonce(client: AsyncClient) -> None:
    registration = await _register(client)
    _, challenge = _pkce_pair()
    consent_url = await _authorize(client, registration["client_id"], challenge)
    request_id = parse_qs(urlparse(consent_url).query)["request_id"][0]
    assert (await client.get(consent_url)).status_code == 200

    forged = await client.post(
        "/mcp/consent", data={"request_id": request_id, "decision": "approve"}
    )

    assert forged.status_code == 400
    # The request is still pending: the forgery consumed nothing.
    assert (await client.get(consent_url)).status_code == 200


@pytest.mark.asyncio
async def test_mcp_tokens_are_listed_with_the_users_api_tokens(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    client_id, _ = await _grant(client)

    listed = await api_tokens_storage.get_api_tokens_for_user(
        Database(db_engine), "test_user"
    )

    assert [token["token_type"] for token in listed] == ["mcp"]
    assert listed[0]["name"] == "Claude (MCP connector)"
    assert client_id


@pytest.mark.asyncio
async def test_unauthenticated_mcp_request_points_at_resource_metadata(
    client: AsyncClient, app_fixture: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_fixture.state.auth_service, "auth_enabled", True)

    response = await client.post("/api/mcp", json=TOOLS_LIST, headers=MCP_HEADERS)

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert f'resource_metadata="{ISSUER}{RESOURCE_METADATA_PATH}"' in challenge


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", RESOURCE_METADATA_PATH),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/authorize"),
        ("POST", "/token"),
        ("POST", "/register"),
        ("POST", "/revoke"),
        ("GET", "/mcp/consent?request_id=x"),
    ],
)
async def test_everything_is_404_when_disabled(
    app_fixture: FastAPI, db_engine: AsyncEngine, method: str, path: str
) -> None:
    _configure(app_fixture, db_engine, enabled=False)
    install_mcp_adapter(app_fixture)
    async with AsyncClient(
        transport=ASGITransport(app=app_fixture), base_url="http://testserver"
    ) as http:
        response = await http.request(method, path)

    assert response.status_code == 404
