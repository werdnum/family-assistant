"""Tests for the unauthenticated privacy policy page."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient

from family_assistant.paths import TEMPLATES_DIR
from family_assistant.web.auth import PUBLIC_PATHS, AuthMiddleware, AuthService
from family_assistant.web.routers.legal import legal_router


@pytest_asyncio.fixture
async def privacy_client() -> AsyncGenerator[AsyncClient]:
    """Client for an app serving the legal router behind enabled auth."""
    app = FastAPI()
    app.state.templates = Jinja2Templates(directory=TEMPLATES_DIR)
    app.include_router(legal_router)

    auth_service = AuthService()
    auth_service.auth_enabled = True
    transport = ASGITransport(app=AuthMiddleware(app, auth_service))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_privacy_policy_renders_without_authentication(
    privacy_client: AsyncClient,
) -> None:
    """The policy must render for a signed-out visitor, e.g. an app reviewer."""
    response = await privacy_client.get("/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Family Assistant Privacy Policy" in response.text
    assert "Last updated:" in response.text


@pytest.mark.asyncio
async def test_privacy_policy_shows_configured_contact(
    privacy_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator name and contact email come from the environment when set."""
    monkeypatch.setenv("PRIVACY_POLICY_OPERATOR", "The Garrett household")
    monkeypatch.setenv("PRIVACY_POLICY_CONTACT_EMAIL", "privacy@example.test")

    response = await privacy_client.get("/privacy")

    assert response.status_code == 200
    assert "The Garrett household" in response.text
    assert "mailto:privacy@example.test" in response.text


@pytest.mark.asyncio
async def test_privacy_policy_falls_back_without_contact(
    privacy_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without configuration the page still renders, pointing at the operator."""
    monkeypatch.delenv("PRIVACY_POLICY_OPERATOR", raising=False)
    monkeypatch.delenv("PRIVACY_POLICY_CONTACT_EMAIL", raising=False)

    response = await privacy_client.get("/privacy")

    assert response.status_code == 200
    assert "mailto:" not in response.text
    assert "the person who operates this Family Assistant instance" in response.text


def test_privacy_path_is_public() -> None:
    """The policy path must bypass the auth middleware."""
    assert any(pattern.match("/privacy") for pattern in PUBLIC_PATHS)
    assert not any(pattern.match("/privacyx") for pattern in PUBLIC_PATHS)
