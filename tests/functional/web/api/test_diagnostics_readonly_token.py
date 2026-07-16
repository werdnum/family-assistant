"""Tests for the read-only diagnostics token on the errors/diagnostics endpoints.

The ``DIAGNOSTICS_READONLY_TOKEN`` env var lets an external monitor read the
error-log and diagnostics-export endpoints without a full user session or API
token, while every other endpoint stays protected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.web.app_creator import app as fastapi_app
from family_assistant.web.dependencies import DIAGNOSTICS_READONLY_TOKEN_ENV_VAR

if TYPE_CHECKING:
    import httpx
    import pytest as _pytest

READONLY_ENDPOINTS = [
    "/api/errors/",
    "/api/diagnostics/export",
    "/api/diagnostics/taint-audit",
]
READONLY_TOKEN = "ro-secret-token-value"


class _FakeAuthService:
    """AuthService stand-in with auth enabled and no valid sessions/tokens."""

    auth_enabled = True
    oauth = None

    async def get_user_from_api_token(
        self,
        auth_header: str,  # noqa: ARG002 - protocol requires this parameter
        request: object,  # noqa: ARG002 - protocol requires this parameter
    ) -> None:
        return None


def _enable_auth() -> object | None:
    original = getattr(fastapi_app.state, "auth_service", None)
    fastapi_app.state.auth_service = _FakeAuthService()
    return original


def _restore_auth(original: object | None) -> None:
    if original is not None:
        fastapi_app.state.auth_service = original
    elif hasattr(fastapi_app.state, "auth_service"):
        del fastapi_app.state.auth_service


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_unauthenticated_when_no_token_configured(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """With auth enabled and no read-only token configured, access is refused."""
    monkeypatch.delenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, raising=False)
    original = _enable_auth()
    try:
        response = await api_client.get(endpoint)
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_allows_readonly_token_bearer(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A matching read-only token via Authorization: Bearer grants read access."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"Authorization": f"Bearer {READONLY_TOKEN}"},
        )
        assert response.status_code == 200
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_allows_readonly_token_x_api_token_header(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A matching read-only token via X-API-Token grants read access."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"X-API-Token": READONLY_TOKEN},
        )
        assert response.status_code == 200
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_wrong_readonly_token(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A non-matching token falls through to normal auth and is refused."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            endpoint,
            headers={"Authorization": "Bearer not-the-right-token"},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", READONLY_ENDPOINTS)
async def test_endpoint_rejects_non_ascii_token_without_500(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """A non-ASCII token falls through to normal auth (401), not a 500.

    secrets.compare_digest raises TypeError on non-ASCII str inputs, so the
    comparison must be done on bytes to avoid leaking a 500.
    """
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        # Send the value as latin-1 bytes (valid on the wire); Starlette decodes
        # headers as latin-1, so the server sees a non-ASCII str token.
        response = await api_client.get(
            endpoint,
            headers={b"Authorization": "Bearer tökén-with-ünïcode".encode("latin-1")},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)


@pytest.mark.asyncio
async def test_readonly_token_does_not_unlock_other_endpoints(
    api_client: httpx.AsyncClient,
    monkeypatch: _pytest.MonkeyPatch,
) -> None:
    """The read-only token only covers diagnostics, not other protected endpoints."""
    monkeypatch.setenv(DIAGNOSTICS_READONLY_TOKEN_ENV_VAR, READONLY_TOKEN)
    original = _enable_auth()
    try:
        response = await api_client.get(
            "/api/debug/profiles",
            headers={"Authorization": f"Bearer {READONLY_TOKEN}"},
        )
        assert response.status_code == 401
    finally:
        _restore_auth(original)
