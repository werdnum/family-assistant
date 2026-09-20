"""AuthMiddleware behaviour specific to the MCP adapter (docs/design/mcp-adapter.md).

An OAuth access token issued to an MCP client is an ``api_tokens`` row of type
``mcp``. It authenticates the MCP endpoint and nothing else, and the endpoint's
401 tells a client where to find the OAuth protected-resource metadata.
"""

from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.types import ASGIApp, Receive, Scope, Send

from family_assistant.web.auth import (
    MCP_ENDPOINT_PATH,
    PUBLIC_PATHS,
    AuthMiddleware,
    AuthService,
    BootstrapBodyLimitMiddleware,
    is_mcp_endpoint_path,
    mcp_resource_metadata_url,
)
from family_assistant.web.route_auth import BOOTSTRAP_BODY_LIMIT_BYTES


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    assert scope["type"] == "http"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"ok"})


class _TypedTokenAuthService(AuthService):
    """Auth enabled; the bearer value names the token type it resolves to."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_enabled = True

    async def get_user_from_api_token(
        self, auth_header: str, request: object
    ) -> dict | None:
        token_type = auth_header.removeprefix("Bearer ")
        if token_type not in {"api", "mcp"}:
            return None
        return {
            "sub": "token-user",
            "name": "token-user",
            "email": "token-user",
            "source": "api_token",
            "token_id": 7,
            "token_type": token_type,
        }


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    middleware = AuthMiddleware(_ok_app, _TypedTokenAuthService())
    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [MCP_ENDPOINT_PATH, MCP_ENDPOINT_PATH + "/"])
async def test_mcp_token_authenticates_the_mcp_endpoint(
    client: AsyncClient, path: str
) -> None:
    response = await client.post(path, headers={"Authorization": "Bearer mcp"})
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/notes/", "/api/v1/chat/send_message", "/api/me/tokens"]
)
async def test_mcp_token_is_rejected_elsewhere(client: AsyncClient, path: str) -> None:
    response = await client.post(path, headers={"Authorization": "Bearer mcp"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_token_still_reaches_the_mcp_endpoint(client: AsyncClient) -> None:
    response = await client.post(
        MCP_ENDPOINT_PATH, headers={"Authorization": "Bearer api"}
    )
    assert response.status_code == 200


def _with_app_state(middleware: AuthMiddleware, *, enabled: bool) -> ASGIApp:
    async def app_with_config(scope: Scope, receive: Receive, send: Send) -> None:
        scope["app"] = SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    server_url="https://fa.example.com/",
                    mcp_adapter=SimpleNamespace(enabled=enabled),
                )
            )
        )
        await middleware(scope, receive, send)

    return app_with_config


@pytest.mark.asyncio
async def test_unauthenticated_mcp_request_advertises_resource_metadata() -> None:
    app_with_config = _with_app_state(
        AuthMiddleware(_ok_app, _TypedTokenAuthService()), enabled=True
    )

    transport = ASGITransport(app=app_with_config)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(MCP_ENDPOINT_PATH)
    assert response.status_code == 401
    assert (
        'resource_metadata="https://fa.example.com/.well-known/oauth-protected-resource/api/mcp"'
        in response.headers["WWW-Authenticate"]
    )


@pytest.mark.asyncio
async def test_disabled_adapter_reaches_its_own_gate_unauthenticated() -> None:
    """Off means 404 from the endpoint gate, not a challenge for an absent server."""
    app_with_config = _with_app_state(
        AuthMiddleware(_ok_app, _TypedTokenAuthService()), enabled=False
    )
    transport = ASGITransport(app=app_with_config)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(MCP_ENDPOINT_PATH)
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/authorize", "/token", "/register", "/revoke"])
async def test_oauth_protocol_endpoints_cap_request_bodies(path: str) -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        small = await c.post(path, content=b"x" * 200)
        oversized = await c.post(path, content=b"x" * (BOOTSTRAP_BODY_LIMIT_BYTES + 1))
    assert small.status_code == 200
    assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_unauthenticated_non_mcp_request_keeps_plain_challenge(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/notes/")
    assert response.status_code == 401
    assert "resource_metadata" not in response.headers["WWW-Authenticate"]


def test_mcp_endpoint_path_matching() -> None:
    assert is_mcp_endpoint_path("/api/mcp")
    assert is_mcp_endpoint_path("/api/mcp/")
    assert not is_mcp_endpoint_path("/api/mcpx")
    assert not is_mcp_endpoint_path("/api/mc")


def test_resource_metadata_url_is_path_specific() -> None:
    assert (
        mcp_resource_metadata_url("https://fa.example.com")
        == "https://fa.example.com/.well-known/oauth-protected-resource/api/mcp"
    )


@pytest.mark.parametrize("path", ["/authorize", "/token", "/register", "/revoke"])
def test_oauth_protocol_endpoints_are_public(path: str) -> None:
    assert any(pattern.match(path) for pattern in PUBLIC_PATHS)


@pytest.mark.parametrize("path", ["/mcp/consent", "/authorize/x", "/tokens"])
def test_consent_page_and_near_misses_are_not_public(path: str) -> None:
    assert not any(pattern.match(path) for pattern in PUBLIC_PATHS)
