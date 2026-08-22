"""Tests for the iOS app auth token endpoints."""

import hashlib
import time
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from family_assistant.storage import api_tokens as api_tokens_storage

if TYPE_CHECKING:
    from fastapi import FastAPI

from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web import jwt_tokens as jwt_tokens_module
from family_assistant.web.route_auth import api_route_classification
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
    async def test_aasa_endpoint(
        self, api_test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("APPLE_TEAM_ID", raising=False)
        monkeypatch.delenv("APPLE_BUNDLE_ID", raising=False)

        response = await api_test_client.get("/.well-known/apple-app-site-association")
        assert response.status_code == 200
        data = response.json()
        assert "applinks" in data
        assert "details" in data["applinks"]
        details = data["applinks"]["details"]
        assert len(details) == 1
        assert details[0]["appID"] == "H7NBC2S52X.dev.andrewgarrett.assistant"
        assert "/.well-known/app-auth-callback*" in details[0]["paths"]
        assert "/shared/conversations/*" in details[0]["paths"]


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


class TestJWTTokens:
    """Signed-JWT issuance when JWT_SIGNING_KEY is configured."""

    @pytest_asyncio.fixture
    async def session_client(
        self, app_fixture: "FastAPI"
    ) -> AsyncGenerator[AsyncClient]:
        """Client with SessionMiddleware enabled for browser-session testing."""
        app_fixture.add_middleware(SessionMiddleware, secret_key="test-secret")
        transport = ASGITransport(app=app_fixture)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client

    @pytest_asyncio.fixture
    async def jwt_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncGenerator[None]:
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        monkeypatch.setenv("JWT_SIGNING_KEY", pem)
        jwt_tokens_module.init_jwt_signing()
        yield  # type: ignore[misc]
        jwt_tokens_module.reset_jwt_signing_for_tests()

    @pytest.mark.asyncio
    async def test_exchange_returns_signed_jwt(
        self, api_test_client: AsyncClient, jwt_enabled: None
    ) -> None:
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["api_token"].count(".") == 2
        assert data["expires_in"] == 3600

        claims = jwt_tokens_module.verify_access_token(data["api_token"])
        assert claims is not None
        assert claims["sub"] == "testuser@example.com"

    @pytest.mark.asyncio
    async def test_refresh_returns_signed_jwt(
        self, api_test_client: AsyncClient, jwt_enabled: None
    ) -> None:
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)
        exchange = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        refresh_token = exchange.json()["refresh_token"]

        response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["api_token"].count(".") == 2
        assert data["expires_in"] == 3600

    @pytest.mark.asyncio
    async def test_exchange_stays_opaque_without_signing_key(
        self, api_test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        jwt_tokens_module.reset_jwt_signing_for_tests()

        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)
        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["expires_in"] == 30 * 86400
        assert not jwt_tokens_module.looks_like_jwt(data["api_token"])

    @pytest.mark.asyncio
    async def test_opaque_upgrade_requires_valid_token(
        self, api_test_client: AsyncClient, jwt_enabled: None
    ) -> None:
        response = await api_test_client.post(
            "/api/auth/token", json={"token": "not-a-real-token"}
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_opaque_upgrade_returns_jwt_bound_to_row(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: None,
    ) -> None:
        minted = api_tokens_storage.mint_api_token()
        db = Database(db_engine)
        token_id = await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="script-user@example.com",
            name="script",
            hashed_token=minted.hashed_secret,
            prefix=minted.prefix,
            created_at=minted.created_at,
            expires_at=None,
            token_type="api",
        )

        response = await api_test_client.post(
            "/api/auth/token", json={"token": minted.full_token}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["expires_in"] == 3600

        claims = jwt_tokens_module.verify_access_token(data["api_token"])
        assert claims is not None
        assert claims["tid"] == token_id

    @pytest.mark.asyncio
    async def test_browser_token_sets_cookie(
        self, session_client: AsyncClient, db_engine: AsyncEngine, jwt_enabled: None
    ) -> None:
        """The bridge sets the JWT cookie scoped to /api with Lax SameSite."""
        response = await session_client.get("/api/auth/browser-token")
        assert response.status_code == 200
        body = response.json()
        assert body["expires_in"] == 3600
        assert "token" not in body

        set_cookie = response.headers["set-cookie"]
        assert "fa_access_token=" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()
        assert "path=/api" in set_cookie.lower()

    @pytest.mark.asyncio
    async def test_browser_token_reuses_single_row(
        self, session_client: AsyncClient, db_engine: AsyncEngine, jwt_enabled: None
    ) -> None:
        """Repeated bridge calls reuse one revocation row instead of growing it."""
        for _ in range(3):
            response = await session_client.get("/api/auth/browser-token")
            assert response.status_code == 200

        query = (
            select(func.count().label("count"))
            .select_from(api_tokens_table)
            .where(api_tokens_table.c.name == "browser-session")
        )
        db = Database(db_engine)
        row = await db.fetch_one(query)
        assert row is not None and row["count"] == 1

    @pytest.mark.asyncio
    async def test_browser_token_reports_disabled_without_signing_key(
        self, session_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        jwt_tokens_module.reset_jwt_signing_for_tests()
        response = await session_client.get("/api/auth/browser-token")
        assert response.status_code == 200
        assert response.json() == {"enabled": False}
        assert "fa_access_token" not in response.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_route_classification_published(
        self, api_test_client: AsyncClient
    ) -> None:
        response = await api_test_client.get("/.well-known/auth-route-classification")
        assert response.status_code == 200
        assert response.json() == api_route_classification()

    @pytest.mark.asyncio
    async def test_jwks_endpoint_serves_public_key(
        self, api_test_client: AsyncClient, jwt_enabled: None
    ) -> None:
        response = await api_test_client.get("/.well-known/jwks.json")
        assert response.status_code == 200
        (key,) = response.json()["keys"]
        assert key["kty"] == "EC"
        assert key["kid"]

    @pytest.mark.asyncio
    async def test_jwks_endpoint_absent_without_signing_key(
        self, api_test_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        jwt_tokens_module.reset_jwt_signing_for_tests()
        response = await api_test_client.get("/.well-known/jwks.json")
        assert response.status_code == 404
