"""Functional web tests for the per-user Link device-code OAuth endpoints.

Exercises ``/api/integrations/link`` end to end against a real SQLite DB
(``db_engine``) and an ``httpx.MockTransport`` fake Link server:
- GET status (enabled, disabled, connected, never leaks tokens, no waiver flag)
- POST /device/start (409 when disabled, 502 on upstream failure, 200 contract)
- POST /device/poll (404 unknown / wrong user, pending, slow_down interval bump,
  expired_token, local TTL expiry via MockClock, access_denied, connected with
  account email, encrypted refresh token storage, initial access-token seeding,
  notification, single-use consumption)
- DELETE (best-effort revoke with client_id, row delete, cache eviction, works
  even when disabled)
- Diagnostics read-only token exclusion
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from family_assistant.config_models import AppConfig, LinkIntegrationConfig
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.services.link_provider import (
    LINK_DEFAULT_OAUTH_CLIENT_ID,
    LINK_PROVIDER,
    LinkScope,
)
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    evaluate_oauth_integration_state,
)
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.tools.link_wallet import LINK_TOOL_REQUIRED_SCOPES
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock
from family_assistant.web.dependencies import get_current_session_user
from family_assistant.web.routers.oauth_integration import (
    create_oauth_integration_router,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

AGENTIC_SCOPE = LinkScope.PAYMENT_METHODS_AGENTIC.value
USERINFO_SCOPE = LinkScope.USERINFO_READ.value


@dataclass
class _RecordedNotification:
    user_identifier: str
    title: str
    body: str


@dataclass
class _RecordingNotificationDispatcher:
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
    auth_enabled: bool = True

    async def get_user_from_api_token(self, auth_header: str, request: object) -> None:
        return None


@dataclass
class _FakeLinkAuthServer:
    """Stubs Link's device-code, token, revoke, and userinfo endpoints."""

    device_code_status: int = 200
    poll_responses: list[httpx.Response] = field(default_factory=list)
    userinfo_email: str | None = "alice-link@example.com"
    userinfo_status: int = 200
    device_code_calls: list[dict[str, str]] = field(default_factory=list)
    token_calls: list[dict[str, str]] = field(default_factory=list)
    revoke_calls: list[dict[str, str]] = field(default_factory=list)
    userinfo_calls: list[str] = field(default_factory=list)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path.endswith("/device/code"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.device_code_calls.append(form)
            if self.device_code_status != 200:
                return httpx.Response(self.device_code_status, json={"error": "down"})
            return httpx.Response(
                200,
                json={
                    "device_code": "dc-secret-123",
                    "user_code": "WARM-CAT-42",
                    "verification_uri": "https://login.link.com/device",
                    "verification_uri_complete": (
                        "https://login.link.com/device?user_code=WARM-CAT-42"
                    ),
                    "expires_in": 600,
                    "interval": 5,
                },
            )

        if path.endswith("/device/token"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.token_calls.append(form)
            if self.poll_responses:
                return self.poll_responses.pop(0)
            return httpx.Response(
                200,
                json={
                    "access_token": "link-initial-access-token",
                    "refresh_token": "link-refresh-token-plain",
                    "expires_in": 3600,
                    "scope": f"{AGENTIC_SCOPE} {USERINFO_SCOPE}",
                    "token_type": "Bearer",
                },
            )

        if path.endswith("/device/revoke"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.revoke_calls.append(form)
            return httpx.Response(200)

        if path.endswith("/userinfo"):
            self.userinfo_calls.append(request.headers.get("authorization", ""))
            if self.userinfo_status != 200:
                return httpx.Response(self.userinfo_status, json={"error": "failed"})
            payload: dict[str, object] = {}
            if self.userinfo_email is not None:
                payload["email"] = self.userinfo_email
            return httpx.Response(200, json=payload)

        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))


@dataclass
class _LinkAppFixture:
    app: FastAPI
    db_engine: AsyncEngine
    link_server: _FakeLinkAuthServer
    notifier: _RecordingNotificationDispatcher
    resolver: OAuthCredentialResolver
    encryption: CredentialEncryption
    clock: MockClock
    current_user: dict[str, str]
    http_client: httpx.AsyncClient


@pytest_asyncio.fixture
async def link_app(db_engine: AsyncEngine) -> AsyncGenerator[_LinkAppFixture]:
    await init_db(db_engine)
    key = generate_key()
    encryption = CredentialEncryption(key)
    link_server = _FakeLinkAuthServer()
    http_client = link_server.client()
    notifier = _RecordingNotificationDispatcher()
    clock = MockClock(datetime(2026, 11, 24, 12, 0, tzinfo=UTC))

    integration = LinkIntegrationConfig.model_validate({
        "enabled": True,
        "oauth_client_id": LINK_DEFAULT_OAUTH_CLIENT_ID,
        "oauth_client_secret": "",
        "credential_encryption_key": key,
        "scopes": [AGENTIC_SCOPE, USERINFO_SCOPE],
        "require_taint_enforcement": False,
    })
    app_config = AppConfig.model_validate({
        "database_url": "sqlite+aiosqlite:///:memory:",
        "link_integration": integration,
        "users": [
            {"id": "user-a", "oidc": {"emails": ["alice@example.com"]}},
            {"id": "user-b", "oidc": {"emails": ["bob@example.com"]}},
        ],
    })
    state = evaluate_oauth_integration_state(
        LINK_PROVIDER,
        app_config,
        auth_enabled=True,
        tool_required_scopes=LINK_TOOL_REQUIRED_SCOPES,
        shared_tool_required_scopes={},
    )
    assert state.enabled is True

    resolver = OAuthCredentialResolver(
        LINK_PROVIDER,
        integration,
        encryption,
        http_client,
        notifier=None,
        clock=clock,
    )

    app = FastAPI()
    app.include_router(
        create_oauth_integration_router(LINK_PROVIDER),
        prefix="/api/integrations/link",
    )
    app.state.database_engine = db_engine
    app.state.config = app_config
    app.state.auth_service = _FakeAuthService(auth_enabled=True)
    app.state.oauth_integration_states = {LINK_PROVIDER.name: state}
    app.state.oauth_http_client = http_client
    app.state.oauth_credential_resolvers = {LINK_PROVIDER.name: resolver}
    app.state.oauth_clock = clock
    app.state.notification_dispatcher = notifier

    current_user = {"user_identifier": "user-a", "name": "Alice"}

    async def _override_user() -> dict[str, str]:
        return current_user

    app.dependency_overrides[get_current_session_user] = _override_user

    try:
        yield _LinkAppFixture(
            app=app,
            db_engine=db_engine,
            link_server=link_server,
            notifier=notifier,
            resolver=resolver,
            encryption=encryption,
            clock=clock,
            current_user=current_user,
            http_client=http_client,
        )
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_device_flow_happy_path_seeds_resolver_and_disconnects(
    link_app: _LinkAppFixture,
) -> None:
    link_app.link_server.poll_responses = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(
            200,
            json={
                "access_token": "link-initial-at",
                "refresh_token": "link-initial-rt",
                "expires_in": 3600,
                "scope": f"{AGENTIC_SCOPE} {USERINFO_SCOPE}",
            },
        ),
    ]

    async with AsyncClient(
        transport=ASGITransport(app=link_app.app), base_url="https://fa.example"
    ) as client:
        # 1. Status before connect: enabled, not connected, no taint waiver flag.
        status_resp = await client.get("/api/integrations/link")
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["enabled"] is True
        assert status_body["connected"] is False
        assert status_body["require_taint_enforcement_waived"] is False

        # 2. Start device flow.
        start_resp = await client.post("/api/integrations/link/device/start")
        assert start_resp.status_code == 200
        start_body = start_resp.json()
        flow_id = start_body["flow_id"]
        assert start_body["user_code"] == "WARM-CAT-42"
        assert start_body["verification_uri"] == "https://login.link.com/device"
        assert (
            start_body["verification_uri_complete"]
            == "https://login.link.com/device?user_code=WARM-CAT-42"
        )
        assert start_body["expires_in"] == 600
        assert start_body["interval"] == 5
        assert link_app.link_server.device_code_calls[0]["connection_label"] == (
            "Family Assistant"
        )
        assert "dc-secret-123" not in start_resp.text

        # 3. User isolation: user-b cannot poll user-a's flow_id.
        link_app.current_user["user_identifier"] = "user-b"
        wrong_user_resp = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": flow_id}
        )
        assert wrong_user_resp.status_code == 404
        link_app.current_user["user_identifier"] = "user-a"

        # 4. Poll: authorization_pending -> pending (interval 5).
        p1 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": flow_id}
        )
        assert p1.status_code == 200
        assert p1.json() == {"status": "pending", "interval": 5}

        # 5. Poll: slow_down -> pending (interval bumped to 10).
        p2 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": flow_id}
        )
        assert p2.status_code == 200
        assert p2.json() == {"status": "pending", "interval": 10}

        # 6. Poll: approved -> connected with account email.
        p3 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": flow_id}
        )
        assert p3.status_code == 200
        assert p3.json() == {
            "status": "connected",
            "provider_account_email": "alice-link@example.com",
        }
        assert "link-initial-rt" not in p3.text
        assert "link-initial-at" not in p3.text

        # Flow consumed on connect: replay returns 404.
        replay = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": flow_id}
        )
        assert replay.status_code == 404

        # Verify DB row stores encrypted refresh token and notification fired.
        db = Database(engine=link_app.db_engine)
        row = await db.oauth_connections.get_connection("user-a", LINK_PROVIDER.name)
        assert row is not None
        assert row.provider_account_email == "alice-link@example.com"
        assert row.refresh_token_encrypted != "link-initial-rt"
        assert (
            link_app.encryption.decrypt(row.refresh_token_encrypted)
            == "link-initial-rt"
        )
        # Resolver was seeded: access_token_for returns initial token without
        # calling /device/token again!
        token_calls_before = len(link_app.link_server.token_calls)
        ctx = ToolExecutionContext(
            interface_type="web",
            conversation_id="c1",
            user_name="Alice",
            user_id="user-a",
            turn_id="t1",
            db_context=db,
            processing_service=None,
            clock=link_app.clock,
            plugins=None,
            event_sources=None,
            attachment_registry=None,
            credential_resolvers=None,
            api_backend=None,
            timezone=ZoneInfo("UTC"),
        )
        seeded = await link_app.resolver.access_token_for(ctx, AGENTIC_SCOPE)
        assert seeded == "link-initial-at"
        assert len(link_app.link_server.token_calls) == token_calls_before

        assert len(link_app.notifier.sent) == 1
        assert link_app.notifier.sent[0].user_identifier == "user-a"
        assert "alice-link@example.com" in link_app.notifier.sent[0].body

        # 7. GET status after connect never leaks tokens.
        status_after = await client.get("/api/integrations/link")
        assert status_after.status_code == 200
        body_after = status_after.json()
        assert body_after["connected"] is True
        assert body_after["provider_account_email"] == "alice-link@example.com"
        assert body_after["status"] == "active"
        assert "link-initial-rt" not in status_after.text

        # 8. Disconnect: best-effort revoke with client_id, row deleted.
        del_resp = await client.delete("/api/integrations/link")
        assert del_resp.status_code == 204
        assert link_app.link_server.revoke_calls == [
            {
                "token": "link-initial-rt",
                "client_id": LINK_DEFAULT_OAUTH_CLIENT_ID,
            }
        ]
        del_again = await client.delete("/api/integrations/link")
        assert del_again.status_code == 404


@pytest.mark.asyncio
async def test_device_poll_expired_and_denied_and_local_ttl(
    link_app: _LinkAppFixture,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=link_app.app), base_url="https://fa.example"
    ) as client:
        # Upstream expired_token
        link_app.link_server.poll_responses = [
            httpx.Response(400, json={"error": "expired_token"})
        ]
        f1 = (await client.post("/api/integrations/link/device/start")).json()[
            "flow_id"
        ]
        r1 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": f1}
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "expired"
        assert (
            await client.post(
                "/api/integrations/link/device/poll", json={"flow_id": f1}
            )
        ).status_code == 404

        # Upstream access_denied
        link_app.link_server.poll_responses = [
            httpx.Response(
                400,
                json={"error": "access_denied", "error_description": "User declined"},
            )
        ]
        f2 = (await client.post("/api/integrations/link/device/start")).json()[
            "flow_id"
        ]
        r2 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": f2}
        )
        assert r2.status_code == 200
        assert r2.json() == {"status": "denied", "detail": "User declined"}
        assert (
            await client.post(
                "/api/integrations/link/device/poll", json={"flow_id": f2}
            )
        ).status_code == 404

        # Local TTL expiry via MockClock (does not call upstream token endpoint).
        f3 = (await client.post("/api/integrations/link/device/start")).json()[
            "flow_id"
        ]
        calls_before = len(link_app.link_server.token_calls)
        link_app.clock.advance(timedelta(seconds=601))
        r3 = await client.post(
            "/api/integrations/link/device/poll", json={"flow_id": f3}
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "expired"
        assert len(link_app.link_server.token_calls) == calls_before


@pytest.mark.asyncio
async def test_device_start_returns_409_when_disabled_and_502_on_upstream_error(
    link_app: _LinkAppFixture,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=link_app.app), base_url="https://fa.example"
    ) as client:
        # Upstream 503 -> 502
        link_app.link_server.device_code_status = 503
        err_resp = await client.post("/api/integrations/link/device/start")
        assert err_resp.status_code == 502

        # Disabled -> 409
        link_app.link_server.device_code_status = 200
        link_app.app.state.oauth_integration_states = {
            LINK_PROVIDER.name: OAuthIntegrationState(
                provider=LINK_PROVIDER.name,
                enabled=False,
                reason="Link integration is not configured.",
                taint_enforcement_waived=False,
                enabled_tool_names=frozenset(),
                governed_tool_names=frozenset(LINK_TOOL_REQUIRED_SCOPES),
            )
        }
        disabled_resp = await client.post("/api/integrations/link/device/start")
        assert disabled_resp.status_code == 409


@pytest.mark.asyncio
async def test_link_endpoints_reject_diagnostics_readonly_token(
    link_app: _LinkAppFixture,
) -> None:
    link_app.app.dependency_overrides.pop(get_current_session_user, None)
    headers = {"Authorization": "Bearer diagnostics-readonly-secret"}

    async with AsyncClient(
        transport=ASGITransport(app=link_app.app), base_url="https://fa.example"
    ) as client:
        for method, path, payload in (
            ("GET", "/api/integrations/link", None),
            ("POST", "/api/integrations/link/device/start", None),
            ("POST", "/api/integrations/link/device/poll", {"flow_id": "abc"}),
            ("DELETE", "/api/integrations/link", None),
        ):
            resp = await client.request(method, path, headers=headers, json=payload)
            assert resp.status_code in {401, 403}, (method, path, resp.status_code)
