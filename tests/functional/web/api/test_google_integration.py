"""Functional web tests for the per-user Google OAuth connect flow.

Exercises ``src/family_assistant/web/routers/oauth_integration.py`` end to end
against a stubbed Google token/userinfo/revoke server (``httpx.MockTransport``),
covering the happy path, state single-use, replay/unknown/expired state, user
mismatch, partial grants, missing refresh token, disconnect, the disabled-
integration gate, diagnostics-token exclusion, and status redaction.
"""

import base64
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, GoogleIntegrationConfig
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    evaluate_oauth_integration_state,
)
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.oauth_connections import (
    OAuthConnectionModel,
)
from family_assistant.tools.google_data import GOOGLE_TOOL_REQUIRED_SCOPES
from family_assistant.web.dependencies import get_current_session_user
from family_assistant.web.routers.oauth_integration import (
    create_oauth_integration_router,
)

PLAINTEXT_REFRESH_TOKEN = "1//refresh-token-plaintext-value"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@dataclass
class _RecordedNotification:
    user_identifier: str
    title: str
    body: str


@dataclass
class _RecordingNotificationDispatcher:
    """Records notifications so tests can assert the connect notice names the account."""

    sent: list[_RecordedNotification] = field(default_factory=list)

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Database,
        *,
        metadata: object | None = None,
    ) -> None:
        self.sent.append(
            _RecordedNotification(
                user_identifier=user_identifier, title=title, body=body
            )
        )


@dataclass
class _FakeAuthService:
    """Minimal stub satisfying the enablement gate's auth check.

    ``get_user_from_api_token`` always rejects, so a bearer token that is not a
    valid API token (e.g. the diagnostics read-only token) is refused — proving
    these routes go through ``get_current_user`` and not the diagnostics reader.
    """

    auth_enabled: bool = True

    async def get_user_from_api_token(self, auth_header: str, request: object) -> None:
        return None


def _unsigned_id_token(email: str) -> str:
    """Build an unsigned JWT (header.payload.sig) carrying an ``email`` claim."""

    def _b64(data: dict[str, str]) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64({"email": email, "sub": "google-sub-123"})
    return f"{header}.{payload}.signature"


@dataclass
class _FakeGoogleServer:
    """Stubs Google's token/revoke/userinfo endpoints via an httpx MockTransport."""

    account_email: str = "connected-user@gmail.com"
    granted_scope: str = f"{GMAIL_SCOPE} {DRIVE_SCOPE} openid email"
    include_refresh_token: bool = True
    include_id_token: bool = True
    token_status: int = 200
    revoke_calls: list[dict[str, str]] = field(default_factory=list)
    token_calls: list[dict[str, str]] = field(default_factory=list)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path.endswith("/token"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.token_calls.append(form)
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status, json={"error": "invalid_grant"}
                )
            body: dict[str, object] = {
                "access_token": "ya29.access-token",
                "expires_in": 3599,
                "scope": self.granted_scope,
                "token_type": "Bearer",
            }
            if self.include_refresh_token:
                body["refresh_token"] = PLAINTEXT_REFRESH_TOKEN
            if self.include_id_token:
                body["id_token"] = _unsigned_id_token(self.account_email)
            return httpx.Response(200, json=body)
        if path.endswith("/revoke"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.revoke_calls.append(form)
            return httpx.Response(200, json={})
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"email": self.account_email})
        return httpx.Response(404, json={"error": "not_found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))


def _google_integration_config(**overrides: object) -> GoogleIntegrationConfig:
    base: dict[str, object] = {
        "oauth_client_id": "test-client-id",
        "oauth_client_secret": "test-client-secret",
        "credential_encryption_key": CredentialEncryption.generate_key(),
        "scopes": [GMAIL_SCOPE, DRIVE_SCOPE],
        "require_taint_enforcement": False,
    }
    base.update(overrides)
    return GoogleIntegrationConfig.model_validate(base)


def _app_config(integration: GoogleIntegrationConfig, database_url: str) -> AppConfig:
    """Build an AppConfig with a populated users block for canonical resolution."""
    return AppConfig.model_validate({
        "database_url": database_url,
        "google_integration": integration,
        "users": [{"id": "alice", "oidc": {"emails": ["alice@example.com"]}}],
    })


def _install_integration_state(app: FastAPI) -> None:
    """Recompute and install the startup Google integration state on the app.

    The router is the sole authority on ``app.state.oauth_integration_states``
    (it fails closed when absent), so tests that build or mutate config must
    install a real state the way startup does.
    """
    config = app.state.config
    auth_service = getattr(app.state, "auth_service", None)
    auth_enabled = bool(getattr(auth_service, "auth_enabled", False))
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        config,
        auth_enabled=auth_enabled,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    app.state.oauth_integration_states = {"google": state}


@dataclass
class _GoogleTestApp:
    app: FastAPI
    google_server: _FakeGoogleServer
    dispatcher: _RecordingNotificationDispatcher
    integration: GoogleIntegrationConfig

    def set_user(self, user_id: str) -> None:
        self.app.dependency_overrides[get_current_session_user] = lambda: {
            "user_identifier": user_id
        }


@pytest_asyncio.fixture
async def google_app(
    db_engine: AsyncEngine,
) -> AsyncGenerator[_GoogleTestApp]:
    """Build a minimal FastAPI app wired for the Google integration router."""
    temp_ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await temp_ctx.init_vector_db()

    integration = _google_integration_config()
    google_server = _FakeGoogleServer()
    dispatcher = _RecordingNotificationDispatcher()

    app = FastAPI()
    app.include_router(
        create_oauth_integration_router(GOOGLE_PROVIDER),
        prefix="/api/integrations/google",
    )
    app.state.database_engine = db_engine
    app.state.config = _app_config(integration, str(db_engine.url))
    app.state.auth_service = _FakeAuthService(auth_enabled=True)
    _install_integration_state(app)
    app.state.notification_dispatcher = dispatcher
    google_client = google_server.client()
    app.state.oauth_http_client = google_client
    app.state.oauth_url_overrides = {
        "google": {
            "authorize": "https://accounts.google.test/o/oauth2/v2/auth",
            "token": "https://oauth2.googleapis.test/token",
            "revoke": "https://oauth2.googleapis.test/revoke",
            "userinfo": "https://www.googleapis.test/oauth2/v3/userinfo",
        }
    }
    app.dependency_overrides[get_current_session_user] = lambda: {
        "user_identifier": "alice"
    }

    try:
        yield _GoogleTestApp(
            app=app,
            google_server=google_server,
            dispatcher=dispatcher,
            integration=integration,
        )
    finally:
        await google_client.aclose()


@pytest_asyncio.fixture
async def google_client(
    google_app: _GoogleTestApp,
) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=google_app.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _authorize_and_extract_state(
    client: AsyncClient,
) -> tuple[str, dict[str, str]]:
    """Call /authorize, assert the redirect params, and return (state, params)."""
    response = await client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert response.status_code == 302
    location = response.headers["location"]
    query = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
    return query["state"], query


async def _fetch_connection(
    db_engine: AsyncEngine, user_id: str
) -> OAuthConnectionModel | None:
    db = Database(engine=db_engine)
    return await db.oauth_connections.get_connection(user_id, "google")


@pytest.mark.asyncio
async def test_authorize_redirect_contains_expected_params(
    google_client: AsyncClient,
) -> None:
    _, params = await _authorize_and_extract_state(google_client)

    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params.get("code_challenge")
    scope = params["scope"]
    assert GMAIL_SCOPE in scope
    assert DRIVE_SCOPE in scope
    assert "openid" in scope.split()
    assert "email" in scope.split()
    assert params["redirect_uri"].endswith("/api/integrations/google/callback")


@pytest.mark.asyncio
async def test_authorize_redirect_uri_honors_forwarded_proto(
    google_client: AsyncClient,
) -> None:
    response = await google_client.get(
        "/api/integrations/google/authorize",
        follow_redirects=False,
        headers={"x-forwarded-proto": "https"},
    )
    assert response.status_code == 302
    params = {
        k: v[0]
        for k, v in parse_qs(urlparse(response.headers["location"]).query).items()
    }
    assert params["redirect_uri"].startswith("https://")


@pytest.mark.asyncio
async def test_full_connect_happy_path(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    state, _ = await _authorize_and_extract_state(google_client)

    callback = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "auth-code-xyz", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/settings/accounts?google=connected"

    # The token exchange included the PKCE code_verifier.
    assert google_app.google_server.token_calls
    assert google_app.google_server.token_calls[0]["code_verifier"]
    assert google_app.google_server.token_calls[0]["grant_type"] == "authorization_code"

    status_response = await google_client.get("/api/integrations/google")
    body = status_response.json()
    assert body["connected"] is True
    assert body["provider_account_email"] == "connected-user@gmail.com"
    assert body["status"] == "active"
    assert set(body["granted_scopes"]) >= {GMAIL_SCOPE, DRIVE_SCOPE}
    assert body["missing_configured_scopes"] == []

    # Notification names the connected account.
    assert len(google_app.dispatcher.sent) == 1
    notice = google_app.dispatcher.sent[0]
    assert notice.user_identifier == "alice"
    assert "connected-user@gmail.com" in notice.body

    # Refresh token stored ENCRYPTED and round-trips.
    connection = await _fetch_connection(db_engine, "alice")
    assert connection is not None
    assert connection.refresh_token_encrypted != PLAINTEXT_REFRESH_TOKEN
    decrypted = CredentialEncryption(
        google_app.integration.credential_encryption_key
    ).decrypt(connection.refresh_token_encrypted)
    assert decrypted == PLAINTEXT_REFRESH_TOKEN


@pytest.mark.asyncio
async def test_state_is_single_use(google_client: AsyncClient) -> None:
    state, _ = await _authorize_and_extract_state(google_client)

    first = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code-1", "state": state},
        follow_redirects=False,
    )
    assert first.status_code == 302

    second = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code-2", "state": state},
        follow_redirects=False,
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_unknown_state_rejected(google_client: AsyncClient) -> None:
    response = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": "never-issued-state"},
        follow_redirects=False,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_user_mismatch_rejected(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    # Authorize as alice.
    google_app.set_user("alice")
    state, _ = await _authorize_and_extract_state(google_client)

    # Callback arrives with bob's session.
    google_app.set_user("bob")
    response = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert await _fetch_connection(db_engine, "bob") is None
    assert await _fetch_connection(db_engine, "alice") is None


@pytest.mark.asyncio
async def test_partial_grant_reports_missing_scopes(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # Google grants only Gmail, not Drive.
    google_app.google_server.granted_scope = f"{GMAIL_SCOPE} openid email"

    state, _ = await _authorize_and_extract_state(google_client)
    await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )

    body = (await google_client.get("/api/integrations/google")).json()
    assert body["connected"] is True
    assert DRIVE_SCOPE in body["missing_configured_scopes"]
    assert GMAIL_SCOPE not in body["missing_configured_scopes"]


@pytest.mark.asyncio
async def test_missing_refresh_token_does_not_connect(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    google_app.google_server.include_refresh_token = False

    state, _ = await _authorize_and_extract_state(google_client)
    response = await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "google=error" in response.headers["location"]
    assert "no_refresh_token" in response.headers["location"]
    assert await _fetch_connection(db_engine, "alice") is None


@pytest.mark.asyncio
async def test_disconnect_revokes_and_deletes(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    state, _ = await _authorize_and_extract_state(google_client)
    await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert await _fetch_connection(db_engine, "alice") is not None

    disconnect = await google_client.request("DELETE", "/api/integrations/google")
    assert disconnect.status_code == 204

    # Revoke endpoint was called with the decrypted refresh token.
    assert google_app.google_server.revoke_calls
    assert google_app.google_server.revoke_calls[0]["token"] == PLAINTEXT_REFRESH_TOKEN
    assert await _fetch_connection(db_engine, "alice") is None

    status_body = (await google_client.get("/api/integrations/google")).json()
    assert status_body["connected"] is False


@pytest.mark.asyncio
async def test_disconnect_without_connection_returns_404(
    google_client: AsyncClient,
) -> None:
    response = await google_client.request("DELETE", "/api/integrations/google")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_disabled_integration_status_and_authorize(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # Disable by clearing the encryption key.
    google_app.app.state.config.google_integration = _google_integration_config(
        credential_encryption_key=""
    )
    _install_integration_state(google_app.app)

    status_response = await google_client.get("/api/integrations/google")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["enabled"] is False
    assert body["reason"]
    assert body["connected"] is False

    authorize = await google_client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 409


@pytest.mark.asyncio
async def test_disabled_but_connected_status_shows_connection(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # Connect while enabled, then disable the integration (lost encryption key).
    state, _ = await _authorize_and_extract_state(google_client)
    await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    google_app.app.state.config.google_integration = _google_integration_config(
        credential_encryption_key=""
    )
    _install_integration_state(google_app.app)

    body = (await google_client.get("/api/integrations/google")).json()
    assert body["enabled"] is False
    assert body["reason"]
    # The stored connection is still reported so the user can see and remove it.
    assert body["connected"] is True
    assert body["provider_account_email"] == "connected-user@gmail.com"
    assert body["status"] == "active"
    assert set(body["granted_scopes"]) >= {GMAIL_SCOPE, DRIVE_SCOPE}


@pytest.mark.asyncio
async def test_disconnect_while_disabled_deletes_without_revoke(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    # Connect while enabled, then disable by clearing the encryption key.
    state, _ = await _authorize_and_extract_state(google_client)
    await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )
    assert await _fetch_connection(db_engine, "alice") is not None
    google_app.app.state.config.google_integration = _google_integration_config(
        credential_encryption_key=""
    )
    _install_integration_state(google_app.app)

    # Disconnect must still work even though the integration is disabled.
    disconnect = await google_client.request("DELETE", "/api/integrations/google")
    assert disconnect.status_code == 204
    assert await _fetch_connection(db_engine, "alice") is None
    # No revocation is attempted without a well-formed encryption key.
    assert google_app.google_server.revoke_calls == []


@pytest.mark.asyncio
async def test_disabled_when_auth_not_enabled(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # Even fully configured, dev test_user mode (auth disabled) must refuse.
    google_app.app.state.auth_service = _FakeAuthService(auth_enabled=False)
    _install_integration_state(google_app.app)

    body = (await google_client.get("/api/integrations/google")).json()
    assert body["enabled"] is False

    authorize = await google_client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 409


@pytest.mark.asyncio
async def test_status_reads_shared_state_when_installed(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # When the app installs the startup-computed state, the router reports its
    # enabled/reason/waived rather than re-deriving from config+auth. Here the
    # shared state disables the integration for a floor reason even though the
    # config is fully valid — proving the router defers to the shared state.
    google_app.app.state.oauth_integration_states = {
        "google": OAuthIntegrationState(
            provider="google",
            enabled=False,
            reason="Google integration is disabled: taint floor not met.",
            taint_enforcement_waived=True,
            enabled_tool_names=frozenset(),
            governed_tool_names=frozenset(GOOGLE_TOOL_REQUIRED_SCOPES),
        )
    }

    body = (await google_client.get("/api/integrations/google")).json()
    assert body["enabled"] is False
    assert body["reason"] == "Google integration is disabled: taint floor not met."
    assert body["require_taint_enforcement_waived"] is True

    authorize = await google_client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 409


@pytest.mark.asyncio
async def test_enabled_shared_state_allows_authorize(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    google_app.app.state.oauth_integration_states = {
        "google": OAuthIntegrationState(
            provider="google",
            enabled=True,
            reason=None,
            taint_enforcement_waived=False,
            enabled_tool_names=frozenset({"gmail_search"}),
            governed_tool_names=frozenset(GOOGLE_TOOL_REQUIRED_SCOPES),
        )
    }

    body = (await google_client.get("/api/integrations/google")).json()
    assert body["enabled"] is True
    assert body["require_taint_enforcement_waived"] is False

    authorize = await google_client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 302


@pytest.mark.asyncio
async def test_missing_shared_state_fails_closed(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
) -> None:
    # With no startup-computed state installed the router must fail closed rather
    # than re-deriving a reduced local enablement check.
    del google_app.app.state.oauth_integration_states

    status_response = await google_client.get("/api/integrations/google")
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["enabled"] is False
    assert body["reason"] == "Google integration state unavailable"

    authorize = await google_client.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 409


@pytest.mark.asyncio
async def test_api_token_auth_refused_for_session_only_routes(
    db_engine: AsyncEngine,
) -> None:
    """An API-token-sourced caller (no browser session) must be refused (403).

    The OAuth connect/disconnect routes are session-only; a valid API-token
    identity is rejected by ``get_current_session_user`` even though it would
    otherwise authenticate.
    """
    temp_ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await temp_ctx.init_vector_db()

    integration = _google_integration_config()
    app = FastAPI()
    app.include_router(
        create_oauth_integration_router(GOOGLE_PROVIDER),
        prefix="/api/integrations/google",
    )
    app.state.database_engine = db_engine
    app.state.config = _app_config(integration, str(db_engine.url))

    @dataclass
    class _ApiTokenAuthService:
        auth_enabled: bool = True

        async def get_user_from_api_token(
            self, auth_header: str, request: object
        ) -> dict[str, object]:
            return {"sub": "alice", "source": "api_token"}

    app.state.auth_service = _ApiTokenAuthService()
    _install_integration_state(app)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        status_response = await client.get(
            "/api/integrations/google",
            headers={"Authorization": "Bearer some-api-token"},
        )
        authorize = await client.get(
            "/api/integrations/google/authorize",
            headers={"Authorization": "Bearer some-api-token"},
            follow_redirects=False,
        )

    assert status_response.status_code == 403
    assert authorize.status_code == 403


@pytest.mark.asyncio
async def test_status_never_leaks_token_material(
    google_client: AsyncClient,
    google_app: _GoogleTestApp,
    db_engine: AsyncEngine,
) -> None:
    state, _ = await _authorize_and_extract_state(google_client)
    await google_client.get(
        "/api/integrations/google/callback",
        params={"code": "code", "state": state},
        follow_redirects=False,
    )

    connection = await _fetch_connection(db_engine, "alice")
    assert connection is not None
    ciphertext = connection.refresh_token_encrypted

    status_response = await google_client.get("/api/integrations/google")
    text = status_response.text
    assert PLAINTEXT_REFRESH_TOKEN not in text
    assert ciphertext not in text


@pytest.mark.asyncio
async def test_uses_get_current_user_not_diagnostics_reader(
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostics read-only token must NOT unlock these routes.

    Builds the app WITHOUT the ``get_current_user`` override so real auth runs;
    with ``auth_service.auth_enabled`` True and no session/token, the request is
    rejected even when a diagnostics token is presented as a bearer header.
    """
    temp_ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await temp_ctx.init_vector_db()

    monkeypatch.setenv("DIAGNOSTICS_READONLY_TOKEN", "diag-secret-token")

    app = FastAPI()
    app.include_router(
        create_oauth_integration_router(GOOGLE_PROVIDER),
        prefix="/api/integrations/google",
    )
    app.state.database_engine = db_engine
    app.state.config = AppConfig(
        database_url=str(db_engine.url),
        google_integration=_google_integration_config(),
    )
    app.state.auth_service = _FakeAuthService(auth_enabled=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/api/integrations/google",
            headers={"Authorization": "Bearer diag-secret-token"},
        )
    # The diagnostics token is not accepted; get_current_user rejects the request.
    assert response.status_code == 401
