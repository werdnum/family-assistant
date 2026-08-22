"""Fail-closed middleware enforcement for /api routes (design: jwt-edge-auth)."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from family_assistant.web.auth import AuthMiddleware, AuthService


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Minimal ASGI app that records it was reached and returns 200."""
    assert scope["type"] == "http"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"ok"})


class _RejectingAuthService(AuthService):
    """Auth enabled, but no session and no valid API token."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_enabled = True

    async def get_user_from_api_token(self, auth_header: str, request: object) -> None:
        return None


class _AcceptingAuthService(_RejectingAuthService):
    """Accepts any bearer credential as a fixed user."""

    async def get_user_from_api_token(self, auth_header: str, request: object) -> dict:
        return {
            "sub": "token-user",
            "name": "token-user",
            "email": "token-user",
            "source": "api_token",
            "token_id": 1,
        }


@pytest.fixture(params=[_RejectingAuthService, _AcceptingAuthService])
def auth_service_class(request: pytest.FixtureRequest) -> type:
    return request.param  # type: ignore[no-any-return]


@pytest_asyncio.fixture
async def client(auth_service_class: type) -> AsyncGenerator[AsyncClient]:
    middleware = AuthMiddleware(_ok_app, auth_service_class())
    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/notes/"),
        ("POST", "/api/notes/"),
        ("DELETE", "/api/notes/title"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/v1/chat/conversations"),
    ],
)
async def test_unauthenticated_default_auth_api_request_rejected(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401
    assert response.json()["detail"].startswith("Not authenticated")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/exchange"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/token"),
        ("GET", "/api/auth/browser-token"),
        ("POST", "/api/errors/"),
        ("GET", "/api/errors/telemetry"),
        ("GET", "/api/diagnostics/export"),
        ("GET", "/api/debug/profiles"),
    ],
)
async def test_classified_routes_reach_the_app(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_valid_bearer_token_passes_default_auth() -> None:
    middleware = AuthMiddleware(_ok_app, _AcceptingAuthService())
    transport = ASGITransport(app=middleware)

    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/api/notes/", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
