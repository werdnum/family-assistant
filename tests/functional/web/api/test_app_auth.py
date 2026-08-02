"""Tests for the iOS app auth token endpoints."""

import hashlib
import time
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from family_assistant.storage import api_tokens as api_tokens_storage

if TYPE_CHECKING:
    from fastapi import FastAPI

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web.routers.app_auth import (
    auth_codes,
    cleanup_expired_codes,
)


@pytest.fixture(autouse=True)
def _clear_auth_codes() -> None:
    """Clear in-memory auth codes before each test."""
    auth_codes.clear()


def _create_pkce_pair() -> tuple[str, str]:
    """Create a PKCE code_verifier and code_challenge pair."""
    code_verifier = "test-code-verifier-that-is-long-enough-for-pkce"
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _seed_auth_code(code_challenge: str) -> str:
    """Seed an auth code in the in-memory store, returning the code."""
    auth_code = "test-auth-code-12345"
    auth_codes[auth_code] = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "user_info": {"sub": "testuser@example.com", "email": "testuser@example.com"},
        "created_at": time.monotonic(),
    }
    return auth_code


class TestCodeExchange:
    @pytest.mark.asyncio
    async def test_exchange_valid_code(self, api_test_client: AsyncClient) -> None:
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 200
        data = response.json()
        assert "api_token" in data
        assert "refresh_token" in data
        assert data["expires_in"] == 30 * 86400
        assert len(data["api_token"]) > 8
        assert len(data["refresh_token"]) > 8

    @pytest.mark.asyncio
    async def test_exchange_invalid_code(self, api_test_client: AsyncClient) -> None:
        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": "nonexistent", "code_verifier": "whatever"},
        )
        assert response.status_code == 400
        assert "Invalid or expired" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_exchange_wrong_verifier(self, api_test_client: AsyncClient) -> None:
        _, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": "wrong-verifier"},
        )
        assert response.status_code == 400
        assert "PKCE verification failed" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_exchange_code_is_single_use(
        self, api_test_client: AsyncClient
    ) -> None:
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        # First use succeeds
        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 200

        # Second use fails
        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_exchange_expired_code(self, api_test_client: AsyncClient) -> None:
        _, code_challenge = _create_pkce_pair()
        expired_code = "expired-code"
        auth_codes[expired_code] = {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "user_info": {"sub": "testuser@example.com"},
            "created_at": time.monotonic() - 120,  # 2 minutes ago
        }

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": expired_code, "code_verifier": "whatever"},
        )
        assert response.status_code == 400
        assert "expired" in response.json()["detail"].lower()


class TestRefreshToken:
    @pytest_asyncio.fixture
    async def token_pair(self, api_test_client: AsyncClient) -> dict:
        """Create a valid token pair via code exchange."""
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 200
        return response.json()

    @pytest.mark.asyncio
    async def test_refresh_valid_token(
        self, api_test_client: AsyncClient, token_pair: dict
    ) -> None:
        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": token_pair["refresh_token"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert "api_token" in data
        assert data["expires_in"] == 30 * 86400
        # New API token should be different from the original
        assert data["api_token"] != token_pair["api_token"]

    @pytest.mark.asyncio
    async def test_refresh_invalid_token(self, api_test_client: AsyncClient) -> None:
        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": "totally-invalid-token-value"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_api_token_fails(
        self, api_test_client: AsyncClient, token_pair: dict
    ) -> None:
        """API tokens cannot be used as refresh tokens."""
        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": token_pair["api_token"]},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_revoked_token(
        self,
        api_test_client: AsyncClient,
        token_pair: dict,
        db_engine: AsyncEngine,
    ) -> None:
        """Revoked refresh tokens should be rejected."""
        refresh_prefix = token_pair["refresh_token"][:8]
        db = Database(db_engine)
        await db.execute(
            update(api_tokens_table)
            .where(api_tokens_table.c.prefix == refresh_prefix)
            .values(is_revoked=True)
        )

        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": token_pair["refresh_token"]},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_revoking_api_token_cascades_to_refresh_token(
        self,
        api_test_client: AsyncClient,
        token_pair: dict,
        db_engine: AsyncEngine,
    ) -> None:
        """Revoking an API token should also revoke its child refresh token."""
        api_prefix = token_pair["api_token"][:8]

        # Find the API token ID and revoke it
        db = Database(db_engine)
        row = await db.fetch_one(
            select(api_tokens_table.c.id).where(api_tokens_table.c.prefix == api_prefix)
        )
        assert row is not None
        api_token_id = row["id"]

        success = await api_tokens_storage.revoke_api_token(
            db, api_token_id, "testuser@example.com"
        )
        assert success

        # Refresh token should now be rejected
        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": token_pair["refresh_token"]},
        )
        assert response.status_code == 401


class TestTokenSession:
    @pytest_asyncio.fixture
    async def session_client(
        self, app_fixture: "FastAPI"
    ) -> AsyncGenerator[AsyncClient]:
        """Client with SessionMiddleware enabled for token-session testing."""
        app_fixture.add_middleware(SessionMiddleware, secret_key="test-secret")
        transport = ASGITransport(app=app_fixture)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client

    @pytest.mark.asyncio
    async def test_token_session_sets_cookie(self, session_client: AsyncClient) -> None:
        response = await session_client.post("/api/auth/token-session")
        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestAppleAppSiteAssociation:
    @pytest.mark.asyncio
    async def test_aasa_endpoint(self, api_test_client: AsyncClient) -> None:
        response = await api_test_client.get("/.well-known/apple-app-site-association")
        assert response.status_code == 200
        data = response.json()
        assert "applinks" in data
        assert "details" in data["applinks"]
        details = data["applinks"]["details"]
        assert len(details) == 1
        assert "/.well-known/app-auth-callback*" in details[0]["paths"]


class TestCleanupExpiredCodes:
    def test_cleanup_removes_expired(self) -> None:
        auth_codes["old"] = {
            "code_challenge": "x",
            "code_challenge_method": "S256",
            "user_info": {},
            "created_at": time.monotonic() - 120,
        }
        auth_codes["fresh"] = {
            "code_challenge": "y",
            "code_challenge_method": "S256",
            "user_info": {},
            "created_at": time.monotonic(),
        }
        cleanup_expired_codes()
        assert "old" not in auth_codes
        assert "fresh" in auth_codes
