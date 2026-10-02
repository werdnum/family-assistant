"""Embedded pages use the API authentication boundary, including on the LAN."""

import os
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from playwright.async_api import Page, Route, expect
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.database import Database
from family_assistant.web import app_creator, jwt_tokens
from family_assistant.web.auth import AuthService
from family_assistant.web.routers import embedded_pages


@pytest.fixture
def jwt_service(monkeypatch: pytest.MonkeyPatch) -> jwt_tokens.JWTTokenService:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("JWT_SIGNING_KEY", pem)
    return jwt_tokens.JWTTokenService.from_environment()


@pytest_asyncio.fixture
async def embedded_app(
    db_engine: AsyncEngine,
    jwt_service: jwt_tokens.JWTTokenService,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    assets = tmp_path / "dist" / "embedded" / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text("window.embeddedLoaded = true;")
    (assets.parent / "embedded.html").write_text(
        '<html><script src="/api/app/assets/app.js"></script></html>'
    )
    if request.node.get_closest_marker("playwright") is None:
        monkeypatch.setattr(app_creator, "static_dir", tmp_path)
        monkeypatch.setattr(embedded_pages, "STATIC_DIST_DIR", tmp_path / "dist")
    app = app_creator.create_app()
    app.state.auth_service = AuthService(db_engine, jwt_service)
    app.state.auth_service.auth_enabled = True

    @app.get("/login", name="login")
    async def login_page() -> str:
        return "Sign in"

    app.state.jwt_token_service = jwt_service
    app.state.database_engine = db_engine
    app.add_middleware(SessionMiddleware, secret_key="embedded-test-secret")
    return app


@pytest_asyncio.fixture
async def embedded_client(embedded_app: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=embedded_app), base_url="https://testserver"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def access_token(
    db_engine: AsyncEngine, jwt_service: jwt_tokens.JWTTokenService
) -> str:
    minted = api_tokens_storage.mint_api_token()
    token_id = await api_tokens_storage.add_api_token(
        db_context=Database(db_engine),
        user_identifier="ios-user@example.com",
        name="iOS App",
        hashed_token=minted.hashed_secret,
        prefix=minted.prefix,
        created_at=minted.created_at,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        token_type="api",
    )
    return jwt_service.mint_access_token("ios-user@example.com", token_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/app/pages/documents/", "/api/app/assets/app.js"]
)
async def test_embedded_pages_and_assets_require_auth(
    embedded_client: AsyncClient, path: str
) -> None:
    response = await embedded_client.get(path)
    assert response.status_code == 401
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_token_session_sets_bounded_native_jwt_cookie(
    embedded_client: AsyncClient,
    access_token: str,
    jwt_service: jwt_tokens.JWTTokenService,
) -> None:
    response = await embedded_client.post(
        "/api/auth/token-session", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    jwt_cookie = next(
        cookie for cookie in cookies if cookie.startswith("fa_access_token=")
    )
    assert f"fa_access_token={access_token}" in jwt_cookie
    assert "HttpOnly" in jwt_cookie
    assert "Secure" in jwt_cookie
    assert "SameSite=lax" in jwt_cookie
    assert "Path=/api" in jwt_cookie
    claims = jwt_service.verify_access_token(access_token)
    assert claims is not None
    remaining = int(claims["exp"]) - int(time.time())
    assert (
        f"Max-Age={remaining}" in jwt_cookie or f"Max-Age={remaining + 1}" in jwt_cookie
    )
    assert any(cookie.startswith("session=") for cookie in cookies)
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/app/pages/documents/", "/api/app/assets/app.js", "/api/auth/me"]
)
async def test_jwt_cookie_alone_authenticates_pages_assets_and_api(
    embedded_client: AsyncClient, access_token: str, path: str
) -> None:
    embedded_client.cookies.set("fa_access_token", access_token)
    response = await embedded_client.get(path)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/app/pages/documents/", "/api/app/assets/app.js", "/api/auth/me"]
)
async def test_revoked_jwt_cookie_fails_on_pages_assets_and_api(
    embedded_client: AsyncClient,
    access_token: str,
    jwt_service: jwt_tokens.JWTTokenService,
    db_engine: AsyncEngine,
    path: str,
) -> None:
    claims = jwt_service.verify_access_token(access_token)
    assert claims is not None
    await api_tokens_storage.revoke_api_token(
        Database(db_engine), int(claims["tid"]), "ios-user@example.com"
    )
    embedded_client.cookies.set("fa_access_token", access_token)
    response = await embedded_client.get(path)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_jwt_cookie_cannot_load_embedded_page(
    embedded_client: AsyncClient,
    access_token: str,
    jwt_service: jwt_tokens.JWTTokenService,
) -> None:
    claims = jwt_service.verify_access_token(access_token)
    assert claims is not None
    expired_claims = dict(claims)
    expired_claims["exp"] = int(time.time()) - 120
    expired = jwt.encode(
        expired_claims,
        os.environ["JWT_SIGNING_KEY"],
        algorithm="ES256",
        headers=jwt.get_unverified_header(access_token),
    )
    embedded_client.cookies.set("fa_access_token", expired)
    response = await embedded_client.get("/api/app/pages/documents/")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_header_does_not_fall_back_to_valid_cookie(
    embedded_client: AsyncClient, access_token: str
) -> None:
    embedded_client.cookies.set("fa_access_token", access_token)
    response = await embedded_client.get(
        "/api/app/pages/documents/", headers={"Authorization": "Bearer invalid"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_api_cookie_does_not_authenticate_normal_website(
    embedded_client: AsyncClient, access_token: str
) -> None:
    embedded_client.cookies.set("fa_access_token", access_token)
    response = await embedded_client.get("/documents/")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/login")


@pytest.mark.playwright
@pytest.mark.asyncio
async def test_embedded_browser_loads_chunks_and_api_without_access_session(
    embedded_client: AsyncClient, access_token: str, page: Page
) -> None:
    """Drive the production React bundle through the real authenticated ASGI app."""
    bridge = await embedded_client.post(
        "/api/auth/token-session", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert bridge.status_code == 200
    await page.context.add_cookies([
        {
            "name": "fa_access_token",
            "value": access_token,
            "domain": "testserver",
            "path": "/api",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }
    ])
    # Leave no HTTPX cookies: each request must authenticate with the browser's
    # actual Cookie header rather than credentials retained by the ASGI client.
    embedded_client.cookies.clear()
    requested_paths: list[str] = []
    failures: list[str] = []

    async def serve(route: Route) -> None:
        request = route.request
        response = await embedded_client.request(
            request.method,
            request.url,
            headers=await request.all_headers(),
            content=request.post_data_buffer,
        )
        requested_paths.append(response.url.path)
        if response.status_code >= 400:
            failures.append(f"{response.status_code} {response.url.path}")
        await route.fulfill(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )

    await page.route("https://testserver/**", serve)
    await page.goto("https://testserver/api/app/pages/about")
    await expect(page.get_by_text("Application Version", exact=True)).to_be_visible()
    assert not failures
    assert "/api/version" in requested_paths
    assert any(
        path.startswith("/api/app/assets/") and path.endswith(".js")
        for path in requested_paths
    )
    assert all(path.startswith("/api/") for path in requested_paths)
    assert "/api/auth/browser-token" not in requested_paths
