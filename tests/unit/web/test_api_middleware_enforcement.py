"""Fail-closed middleware enforcement for /api routes (design: jwt-edge-auth)."""

import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
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
        ("GET", "/api/debug/profiles/tools"),
        ("GET", "/api/asterisk/live"),
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


@pytest.mark.asyncio
async def test_x_api_token_header_authenticates() -> None:
    """The middleware honours the same credential headers as get_current_user."""

    middleware = AuthMiddleware(_ok_app, _AcceptingAuthService())
    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/api/notes/", headers={"X-API-Token": "x"})
    assert response.status_code == 200

    middleware = AuthMiddleware(_ok_app, _RejectingAuthService())
    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/api/notes/", headers={"X-API-Token": "x"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bearer_session_is_bound_to_token_id() -> None:
    """A bearer-authenticated request persists the token id with the session."""

    async def session_reader_app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        body = json.dumps(dict(request.session)).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    stack = SessionMiddleware(
        AuthMiddleware(session_reader_app, _AcceptingAuthService()),
        secret_key="test-secret",
    )
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        first = await c.get("/api/notes/", headers={"Authorization": "Bearer x"})
        assert first.json().get("api_token_id") == 1

        # Replay only the session cookie (no bearer header): the stored user
        # must carry api_token_id so the validity check can revoke it later.
        cookie_header = first.headers["set-cookie"].split(";")[0]
        second = await c.get("/api/notes/", headers={"Cookie": cookie_header})
    assert second.status_code == 200
    assert second.json()["user"]["token_id"] == 1


class _AcceptingJWTAuthService(_AcceptingAuthService):
    """Accepts any bearer credential as a short-lived JWT identity."""

    async def get_user_from_api_token(self, auth_header: str, request: object) -> dict:
        return {
            "sub": "jwt-user",
            "name": "jwt-user",
            "email": "jwt-user",
            "source": "jwt_access_token",
            "token_id": 2,
        }


@pytest.mark.asyncio
async def test_jwt_bearer_auth_is_not_persisted_into_session() -> None:
    """A one-hour JWT must not mint a long-lived session cookie."""

    async def session_reader_app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        body = json.dumps(dict(request.session)).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    stack = SessionMiddleware(
        AuthMiddleware(session_reader_app, _AcceptingJWTAuthService()),
        secret_key="test-secret",
    )
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/api/notes/", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert response.json() == {}
