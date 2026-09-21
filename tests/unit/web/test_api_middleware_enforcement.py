"""Fail-closed middleware enforcement for /api routes (design: jwt-edge-auth)."""

import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Annotated, cast

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from family_assistant.web.app_creator import AuthMiddlewareWrapper
from family_assistant.web.app_creator import middleware as app_middleware
from family_assistant.web.auth import (
    PUBLIC_PATHS,
    AuthMiddleware,
    AuthService,
    BootstrapBodyLimitMiddleware,
)
from family_assistant.web.dependencies import get_current_user
from family_assistant.web.jwt_tokens import JWTTokenService
from family_assistant.web.route_auth import BOOTSTRAP_BODY_LIMIT_BYTES


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
def auth_service_class(request: pytest.FixtureRequest) -> type[AuthService]:
    return cast("type[AuthService]", request.param)


@pytest_asyncio.fixture
async def client(
    auth_service_class: type[AuthService],
) -> AsyncGenerator[AsyncClient]:
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
async def test_jwt_only_authentication_is_enforced_and_reused_by_dependency() -> None:
    class CountingJWTAuthService(_AcceptingJWTAuthService):
        calls = 0

        async def get_user_from_api_token(
            self, auth_header: str, request: object
        ) -> dict:
            self.calls += 1
            return await super().get_user_from_api_token(auth_header, request)

    auth_service = CountingJWTAuthService()
    auth_service.auth_enabled = False
    auth_service.jwt_tokens = JWTTokenService(
        ec.generate_private_key(ec.SECP256R1()), "test-key"
    )
    app = FastAPI()
    app.state.auth_service = auth_service

    @app.get("/api/notes/")
    async def notes(
        current_user: Annotated[dict, Depends(get_current_user)],
    ) -> dict[str, object]:
        return {"sub": current_user["sub"]}

    app.add_middleware(AuthMiddleware, auth_service=auth_service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        unauthenticated = await c.get("/api/notes/")
        response = await c.get("/api/notes/", headers={"Authorization": "Bearer jwt"})

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json() == {"sub": "jwt-user"}
    assert auth_service.calls == 1


@pytest.mark.asyncio
async def test_jwt_only_authentication_does_not_protect_ui_pages() -> None:
    auth_service = _AcceptingJWTAuthService()
    auth_service.auth_enabled = False
    auth_service.jwt_tokens = JWTTokenService(
        ec.generate_private_key(ec.SECP256R1()), "test-key"
    )
    middleware = AuthMiddleware(_ok_app, auth_service)
    transport = ASGITransport(app=middleware)

    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/settings")

    assert response.status_code == 200
    assert response.text == "ok"


def test_application_always_installs_auth_middleware_wrapper() -> None:
    assert any(item.cls is AuthMiddlewareWrapper for item in app_middleware)


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
async def test_opaque_bearer_authentication_is_request_local() -> None:
    """Ordinary bearer auth must not mint a session cookie implicitly."""

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
        second = await c.get("/api/notes/")

    assert first.status_code == 200
    assert first.json() == {}
    assert second.status_code == 401


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/api/notes/", 401),
        ("/api/diagnostics/export", 200),
    ],
)
async def test_expired_bound_jwt_invalidates_session(
    path: str, expected_status: int
) -> None:
    """A session bound to a JWT is rejected once that JWT's exp passes."""

    async def session_app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        if scope["path"] == "/health-seed":
            request.session.update({
                "user": {"sub": "jwt-user", "source": "app_token_session"},
                "api_token_id": 2,
                "session_jwt_exp": time.time() - 10,
            })
        body = json.dumps(dict(request.session)).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    # The AuthMiddleware sits inside SessionMiddleware so both share the
    # session; /api/notes requires default auth, exercising the bound check.
    class _HealthSeedPublic(AuthMiddleware):
        def __init__(self) -> None:
            super().__init__(session_app, _RejectingAuthService())
            self.public_paths = list(PUBLIC_PATHS) + [re.compile(r"^/health-seed$")]

    stack = SessionMiddleware(
        _HealthSeedPublic(),
        secret_key="test-secret",
    )
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        seed = await c.get("/health-seed")
        assert seed.json().get("session_jwt_exp"), "seed failed"

        response = await c.get(
            path,
            headers={"Cookie": seed.headers["set-cookie"].split(";")[0]},
        )
    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.json() == {}


@pytest.mark.asyncio
async def test_small_bootstrap_bodies_pass_through() -> None:

    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post("/api/auth/exchange", content=b"x" * 100)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_oversized_bootstrap_body_rejected_while_streaming() -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(
            "/api/auth/exchange",
            content=b"x" * (BOOTSTRAP_BODY_LIMIT_BYTES + 1),
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_content_length_header_rejected_before_reading() -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(
            "/api/auth/exchange",
            headers={"Content-Length": str(BOOTSTRAP_BODY_LIMIT_BYTES * 10)},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", ["invalid", "-1"])
async def test_invalid_content_length_header_rejected(content_length: str) -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(
            "/api/auth/exchange",
            headers={"Content-Length": content_length},
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_default_auth_routes_are_not_capped() -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post("/api/notes/", content=b"x" * 200)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_non_api_webhook_routes_are_not_capped() -> None:
    stack = BootstrapBodyLimitMiddleware(_ok_app)
    transport = ASGITransport(app=stack)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.post(
            "/webhook/mail/mime",
            content=b"x" * (BOOTSTRAP_BODY_LIMIT_BYTES + 1),
        )
    assert response.status_code == 200
