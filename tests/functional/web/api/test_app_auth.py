"""Tests for the iOS app auth token endpoints."""

import hashlib
import json
import time
from base64 import b64encode, urlsafe_b64encode
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.sessions import SessionMiddleware

from family_assistant.storage import api_tokens as api_tokens_storage
from family_assistant.storage.base import api_tokens_table
from family_assistant.storage.database import Database
from family_assistant.web import jwt_tokens as jwt_tokens_module
from family_assistant.web.auth import AuthService
from family_assistant.web.dependencies import get_current_user
from family_assistant.web.route_auth import api_route_classification
from family_assistant.web.routers.app_auth import (
    api_auth_router,
    auth_codes,
    cleanup_expired_codes,
)


@pytest.fixture(autouse=True)
def _clear_auth_codes() -> None:
    """Clear in-memory auth codes before each test."""
    auth_codes.clear()


def _oidc_session_cookie() -> str:
    payload = b64encode(
        json.dumps({
            "user": {
                "sub": "browser-user@example.com",
                "user_identifier": "browser-user@example.com",
                "email": "browser-user@example.com",
            }
        }).encode("utf-8")
    )
    return TimestampSigner("test-secret").sign(payload).decode("utf-8")


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
        """Client with SessionMiddleware and an auth-enabled OIDC session."""
        app_fixture.add_middleware(SessionMiddleware, secret_key="test-secret")
        original_overrides = dict(app_fixture.dependency_overrides)
        app_fixture.state.auth_service = _SessionAuthService()
        transport = ASGITransport(app=app_fixture)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"session": _oidc_session_cookie()},
        ) as client:
            yield client
        app_fixture.dependency_overrides.clear()
        app_fixture.dependency_overrides.update(original_overrides)

    @pytest_asyncio.fixture
    async def jwt_enabled(
        self, app_fixture: "FastAPI", monkeypatch: pytest.MonkeyPatch
    ) -> AsyncGenerator[jwt_tokens_module.JWTTokenService]:
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        monkeypatch.setenv("JWT_SIGNING_KEY", pem)
        service = jwt_tokens_module.JWTTokenService.from_environment()
        # app_fixture is the module-level app, shared with every other test on
        # this worker. monkeypatch restores the env, but a service built while
        # it was set would otherwise stay attached and keep API authentication
        # switched on, 401-ing unrelated endpoints later in the session.
        previous_token_service = getattr(app_fixture.state, "jwt_token_service", None)
        auth_service = app_fixture.state.auth_service
        previous_auth_tokens = (
            auth_service.jwt_tokens if isinstance(auth_service, AuthService) else None
        )
        app_fixture.state.jwt_token_service = service
        if isinstance(auth_service, AuthService):
            auth_service.jwt_tokens = service
        try:
            yield service
        finally:
            app_fixture.state.jwt_token_service = previous_token_service
            if isinstance(auth_service, AuthService) and previous_auth_tokens:
                auth_service.jwt_tokens = previous_auth_tokens

    @pytest.mark.asyncio
    async def test_exchange_returns_signed_jwt(
        self,
        api_test_client: AsyncClient,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
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

        claims = jwt_enabled.verify_access_token(data["api_token"])
        assert claims is not None
        assert claims["sub"] == "testuser@example.com"

    @pytest.mark.asyncio
    async def test_exchange_row_outlives_long_configured_jwt(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A configured JWT lifetime over 30 days extends its backing row."""
        monkeypatch.setenv("JWT_ACCESS_TOKEN_TTL_SECONDS", str(40 * 86400))
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)

        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )

        assert response.status_code == 200
        claims = jwt_enabled.verify_access_token(response.json()["api_token"])
        assert claims is not None
        row = await Database(db_engine).fetch_one(
            select(api_tokens_table.c.expires_at).where(
                api_tokens_table.c.id == claims["tid"]
            )
        )
        assert row is not None
        row_expiry = row["expires_at"]
        assert row_expiry is not None
        if row_expiry.tzinfo is None:
            row_expiry = row_expiry.replace(tzinfo=UTC)
        assert row_expiry.timestamp() >= claims["exp"]

    @pytest.mark.asyncio
    async def test_successful_jwt_authentication_updates_last_used(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)
        response = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        token = response.json()["api_token"]
        claims = jwt_enabled.verify_access_token(token)
        assert claims is not None

        auth_service = AuthService(db_engine, jwt_enabled)
        user = await auth_service.get_user_from_api_token(
            f"Bearer {token}", MagicMock(spec=Request)
        )

        assert user is not None
        row = await Database(db_engine).fetch_one(
            select(api_tokens_table.c.last_used_at).where(
                api_tokens_table.c.id == claims["tid"]
            )
        )
        assert row is not None
        assert row["last_used_at"] is not None

    @pytest.mark.asyncio
    async def test_refresh_returns_signed_jwt(
        self,
        api_test_client: AsyncClient,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
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
        self,
        app_fixture: "FastAPI",
        api_test_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        service = jwt_tokens_module.JWTTokenService.from_environment()
        app_fixture.state.jwt_token_service = service
        if isinstance(app_fixture.state.auth_service, AuthService):
            app_fixture.state.auth_service.jwt_tokens = service

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
        self,
        api_test_client: AsyncClient,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
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
        jwt_enabled: jwt_tokens_module.JWTTokenService,
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

        claims = jwt_enabled.verify_access_token(data["api_token"])
        assert claims is not None
        assert claims["tid"] == token_id

    @pytest.mark.asyncio
    async def test_browser_token_sets_cookie(
        self,
        session_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
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
        self,
        session_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """Repeated bridge calls reuse one revocation row instead of growing it."""
        for _ in range(3):
            response = await session_client.get("/api/auth/browser-token")
            assert response.status_code == 200

        query = (
            select(func.count().label("count"))
            .select_from(api_tokens_table)
            .where(
                api_tokens_table.c.name == "browser-session",
                api_tokens_table.c.token_type == "browser",
            )
        )
        db = Database(db_engine)
        row = await db.fetch_one(query)
        assert row is not None and row["count"] == 1

    @pytest.mark.asyncio
    async def test_browser_token_reports_disabled_without_signing_key(
        self,
        app_fixture: "FastAPI",
        session_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        service = jwt_tokens_module.JWTTokenService.from_environment()
        app_fixture.state.jwt_token_service = service
        response = await session_client.get("/api/auth/browser-token")
        assert response.status_code == 200
        assert response.json() == {"enabled": False}
        assert "fa_access_token" not in response.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_browser_token_reports_disabled_without_session_auth(
        self,
        app_fixture: "FastAPI",
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """Without session authentication the bridge must not mint credentials."""
        original_overrides = dict(app_fixture.dependency_overrides)
        app_fixture.state.auth_service = _NoAuthService()
        app_fixture.dependency_overrides[get_current_user] = lambda: {
            "sub": "anonymous",
            "user_identifier": "anonymous",
        }
        try:
            transport = ASGITransport(app=app_fixture)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                response = await client.get("/api/auth/browser-token")
        finally:
            app_fixture.dependency_overrides.clear()
            app_fixture.dependency_overrides.update(original_overrides)
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
        self,
        api_test_client: AsyncClient,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        response = await api_test_client.get("/.well-known/jwks.json")
        assert response.status_code == 200
        (key,) = response.json()["keys"]
        assert key["kty"] == "EC"
        assert key["kid"]

    @pytest.mark.asyncio
    async def test_jwks_endpoint_absent_without_signing_key(
        self,
        app_fixture: "FastAPI",
        api_test_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("JWT_SIGNING_KEY", raising=False)
        service = jwt_tokens_module.JWTTokenService.from_environment()
        app_fixture.state.jwt_token_service = service
        response = await api_test_client.get("/.well-known/jwks.json")
        assert response.status_code == 404


class _SessionAuthService:
    """Auth-enabled stand-in representing a deployment with OIDC sessions."""

    auth_enabled = True
    oauth = None
    database_engine = None

    async def get_user_from_api_token(self, auth_header: str, request: object) -> None:
        return None


class _NoAuthService:
    """Auth-enabled=False stand-in (API-token-only deployment)."""

    auth_enabled = False
    oauth = None


class TestBrowserSessionRows:
    @pytest_asyncio.fixture
    async def session_client(
        self, app_fixture: "FastAPI"
    ) -> AsyncGenerator[AsyncClient]:
        """Client with SessionMiddleware and an auth-enabled OIDC session."""
        app_fixture.add_middleware(SessionMiddleware, secret_key="test-secret")
        original_overrides = dict(app_fixture.dependency_overrides)
        app_fixture.state.auth_service = _SessionAuthService()
        transport = ASGITransport(app=app_fixture)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies={"session": _oidc_session_cookie()},
        ) as client:
            yield client
        app_fixture.dependency_overrides.clear()
        app_fixture.dependency_overrides.update(original_overrides)

    @pytest_asyncio.fixture
    async def jwt_enabled(
        self, app_fixture: "FastAPI", monkeypatch: pytest.MonkeyPatch
    ) -> AsyncGenerator[jwt_tokens_module.JWTTokenService]:
        pem = (
            ec
            .generate_private_key(ec.SECP256R1())
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode("ascii")
        )
        monkeypatch.setenv("JWT_SIGNING_KEY", pem)
        service = jwt_tokens_module.JWTTokenService.from_environment()
        app_fixture.state.jwt_token_service = service
        if isinstance(app_fixture.state.auth_service, AuthService):
            app_fixture.state.auth_service.jwt_tokens = service
        yield service

    @pytest.mark.asyncio
    async def test_refreshed_jwt_rebinds_app_session_without_cookie_renewal(
        self,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retained app session is rebound to the bearer sent on this launch."""
        auth_service = AuthService(db_engine, jwt_enabled)
        auth_service.auth_enabled = True
        app = FastAPI()
        app.state.auth_service = auth_service
        app.state.jwt_token_service = jwt_enabled
        app.state.database_engine = db_engine
        app.include_router(api_auth_router, prefix="/api")

        @app.get("/test/session-state")
        async def session_state(request: Request) -> dict:
            return dict(request.session)

        app.add_middleware(SessionMiddleware, secret_key="test-secret")

        minted = api_tokens_storage.mint_api_token()
        db = Database(db_engine)
        token_id = await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="ios-user@example.com",
            name="iOS App",
            hashed_token=minted.hashed_secret,
            prefix=minted.prefix,
            created_at=minted.created_at,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            token_type="api",
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first_token = jwt_enabled.mint_access_token(
                "ios-user@example.com", token_id
            )
            first = await client.post(
                "/api/auth/token-session",
                headers={"Authorization": f"Bearer {first_token}"},
            )
            assert first.status_code == 200
            first_state = (await client.get("/test/session-state")).json()

            monkeypatch.setenv("JWT_ACCESS_TOKEN_TTL_SECONDS", "7200")
            refreshed_token = jwt_enabled.mint_access_token(
                "ios-user@example.com", token_id
            )
            rebound = await client.post(
                "/api/auth/token-session",
                headers={"Authorization": f"Bearer {refreshed_token}"},
            )
            assert rebound.status_code == 200
            rebound_state = (await client.get("/test/session-state")).json()

            previous_expiry = rebound_state["session_jwt_exp"]
            bridge = await client.get("/api/auth/browser-token")
            bridged_state = (await client.get("/test/session-state")).json()

        assert rebound_state["api_token_id"] == token_id
        assert rebound_state["session_jwt_exp"] > first_state["session_jwt_exp"]
        assert bridged_state["session_jwt_exp"] == previous_expiry
        assert bridge.status_code == 200
        assert bridge.json() == {"enabled": False}
        assert "fa_access_token" not in bridge.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_browser_token_prunes_expired_rows(
        self,
        session_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """An expired internal row is pruned when its replacement is minted."""
        minted = api_tokens_storage.mint_api_token()
        db = Database(db_engine)
        await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="browser-user@example.com",
            name="browser-session",
            hashed_token=minted.hashed_secret,
            prefix=minted.prefix,
            created_at=minted.created_at,
            expires_at=datetime.now(UTC) - timedelta(days=1),
            token_type="browser",
        )

        response = await session_client.get("/api/auth/browser-token")
        assert response.status_code == 200

        query = (
            select(func.count().label("count"))
            .select_from(api_tokens_table)
            .where(
                api_tokens_table.c.user_identifier == "browser-user@example.com",
                api_tokens_table.c.name == "browser-session",
                api_tokens_table.c.token_type == "browser",
            )
        )
        row = await db.fetch_one(query)
        assert row is not None and row["count"] == 1

    @pytest.mark.asyncio
    async def test_browser_token_does_not_reuse_user_named_token(
        self,
        session_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """An API token named browser-session remains a user credential."""
        minted = api_tokens_storage.mint_api_token()
        db = Database(db_engine)
        user_token_id = await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="browser-user@example.com",
            name="browser-session",
            hashed_token=minted.hashed_secret,
            prefix=minted.prefix,
            created_at=minted.created_at,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            token_type="api",
        )

        response = await session_client.get("/api/auth/browser-token")
        assert response.status_code == 200

        query = select(api_tokens_table.c.id, api_tokens_table.c.token_type).where(
            api_tokens_table.c.user_identifier == "browser-user@example.com",
            api_tokens_table.c.name == "browser-session",
        )
        rows = await db.fetch_all(query)
        assert len(rows) == 2
        assert any(
            row["id"] == user_token_id and row["token_type"] == "api" for row in rows
        )
        assert any(row["token_type"] == "browser" for row in rows)

    @pytest.mark.asyncio
    async def test_refresh_reuses_backing_row(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """JWT-mode refreshes rebind to the parent row instead of inserting new ones."""
        code_verifier, code_challenge = _create_pkce_pair()
        auth_code = _seed_auth_code(code_challenge)
        exchange = await api_test_client.post(
            "/api/auth/exchange",
            json={"code": auth_code, "code_verifier": code_verifier},
        )
        first_claims = jwt_enabled.verify_access_token(exchange.json()["api_token"])
        assert first_claims is not None
        db = Database(db_engine)
        expiry_query = select(api_tokens_table.c.expires_at).where(
            api_tokens_table.c.id == first_claims["tid"]
        )
        before_refresh = await db.fetch_one(expiry_query)
        assert before_refresh is not None

        refresh_response = await api_test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": exchange.json()["refresh_token"]},
        )
        assert refresh_response.status_code == 200
        second_claims = jwt_enabled.verify_access_token(
            refresh_response.json()["api_token"]
        )
        assert second_claims is not None
        assert second_claims["tid"] == first_claims["tid"]
        after_refresh = await db.fetch_one(expiry_query)
        assert after_refresh is not None
        assert after_refresh["expires_at"] == before_refresh["expires_at"]

        query = (
            select(func.count().label("count"))
            .select_from(api_tokens_table)
            .where(
                api_tokens_table.c.user_identifier == "testuser@example.com",
                api_tokens_table.c.name == "iOS App",
            )
        )
        row = await db.fetch_one(query)
        assert row is not None and row["count"] == 1

    @pytest.mark.asyncio
    async def test_opaque_upgrade_preserves_non_expiring_row(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """A never-expiring operator token keeps NULL expiry after upgrade."""
        minted = api_tokens_storage.mint_api_token()
        db = Database(db_engine)
        token_id = await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="operator@example.com",
            name="operator",
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

        query = select(api_tokens_table.c.expires_at).where(
            api_tokens_table.c.id == token_id
        )
        row = await db.fetch_one(query)
        assert row is not None and row["expires_at"] is None

    @pytest.mark.asyncio
    async def test_opaque_upgrade_is_capped_by_row_expiry(
        self,
        api_test_client: AsyncClient,
        db_engine: AsyncEngine,
        jwt_enabled: jwt_tokens_module.JWTTokenService,
    ) -> None:
        """Exchanging an opaque token neither extends nor outlives its row."""
        minted = api_tokens_storage.mint_api_token()
        original_expiry = datetime.now(UTC) + timedelta(minutes=30)
        db = Database(db_engine)
        token_id = await api_tokens_storage.add_api_token(
            db_context=db,
            user_identifier="short-lived@example.com",
            name="short-lived",
            hashed_token=minted.hashed_secret,
            prefix=minted.prefix,
            created_at=minted.created_at,
            expires_at=original_expiry,
            token_type="api",
        )

        response = await api_test_client.post(
            "/api/auth/token", json={"token": minted.full_token}
        )
        assert response.status_code == 200
        body = response.json()
        assert 1 <= body["expires_in"] <= 30 * 60
        claims = jwt_enabled.verify_access_token(body["api_token"])
        assert claims is not None
        assert claims["exp"] - claims["iat"] == body["expires_in"]

        query = select(api_tokens_table.c.expires_at).where(
            api_tokens_table.c.id == token_id
        )
        row = await db.fetch_one(query)
        assert row is not None
        stored_expiry = row["expires_at"]
        if stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=UTC)
        assert stored_expiry == original_expiry
