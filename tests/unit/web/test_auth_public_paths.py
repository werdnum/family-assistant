"""Tests that PWA-related paths bypass authentication in AuthMiddleware."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from family_assistant.web.auth import PUBLIC_PATHS, AuthMiddleware, AuthService


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Minimal ASGI app that returns 200 for any request."""
    assert scope["type"] == "http"
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"text/plain")],
    })
    await send({"type": "http.response.body", "body": b"ok"})


@pytest_asyncio.fixture
async def auth_client() -> AsyncGenerator[AsyncClient]:
    """Client wrapping AuthMiddleware with auth enabled and no session user."""
    auth_service = AuthService()
    auth_service.auth_enabled = True
    middleware = AuthMiddleware(_ok_app, auth_service)
    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/manifest.webmanifest",
        "/sw.js",
        "/.well-known/apple-app-site-association",
    ],
)
@pytest.mark.asyncio
async def test_pwa_paths_are_public(auth_client: AsyncClient, path: str) -> None:
    """PWA asset paths must be served without redirecting to login."""
    response = await auth_client.get(path)
    assert response.status_code == 200
    assert response.text == "ok"


def _is_public(path: str) -> bool:
    return any(pattern.match(path) for pattern in PUBLIC_PATHS)


@pytest.mark.parametrize(
    "path",
    [
        "/notes",
        "/manifest.json",
        "/manifest.jsonx",
        "/sw.json",
        "/swxjs",
        "/manifest.webmanifestx",
    ],
)
def test_non_public_paths_are_not_matched(path: str) -> None:
    """Protected paths and near-miss PWA paths must not be treated as public."""
    assert not _is_public(path)
