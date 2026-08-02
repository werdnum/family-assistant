"""End-to-end functional test for the user-scoped Google data feature.

This module wires the *real* chain end to end and fakes only the outermost seam:
Google's own HTTP endpoints (token / revoke / userinfo), served by a single
``httpx.MockTransport`` handler that dispatches on the request path and
``grant_type``. Everything between — the OAuth connect router, the encrypted
connection storage, the real :class:`OAuthCredentialResolver` (per-user token
refresh against the stubbed token endpoint using the *stored, encrypted* refresh
token), the real attachment registry with owner enforcement, and the attachment
HTTP routes — is exercised as it runs in production.

The journeys covered mirror the design's Testing Strategy
(``docs/design/user-scoped-google-data-access.md``):

1. Connect two users (alice, bob) through the real OAuth flow.
2. Read as each user through the real resolver — cross-user isolation.
3. Fail-closed: unconnected user, no acting user, and after disconnect.
4. Attachment ownership across the tool + HTTP stack (content + metadata routes).
5. ``needs_reauth`` journey: ``invalid_grant`` on refresh flips status, notifies,
   and reconnecting restores service.
6. Taint: a Gmail read taints the turn ``unknown_external`` (descriptor-level,
   reusing the existing cheap pattern).

Only a new test file is added; no source or existing test file is modified. The
fake Google server and per-user OAuth helpers deliberately re-use the shapes from
``tests/functional/web/api/test_google_integration.py`` and
``tests/functional/tools/test_google_data_tools.py`` without importing them, so
that shared module remains untouched.
"""

from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from family_assistant.config_models import AppConfig, GoogleIntegrationConfig
from family_assistant.security.taint import (
    SourceTrustTier,
    derive_tool_result_taint_source,
)
from family_assistant.services.api_backend import ApiResponse
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.credential_encryption import CredentialEncryption
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.oauth_credentials import OAuthCredentialResolver
from family_assistant.services.oauth_integration_state import (
    evaluate_oauth_integration_state,
)
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS
from family_assistant.tools.google_data import (
    GOOGLE_TOOL_REQUIRED_SCOPES,
    gmail_get_attachment_tool,
    gmail_search_tool,
)
from family_assistant.tools.metadata import build_tool_descriptor
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.web.dependencies import (
    get_current_session_user,
    get_current_user,
)
from family_assistant.web.routers.attachments_api import attachments_api_router
from family_assistant.web.routers.oauth_integration import (
    create_oauth_integration_router,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.api_backend import ApiBackend
    from family_assistant.storage.repositories.oauth_connections import (
        OAuthConnectionModel,
    )

GMAIL_SCOPE = GoogleScope.GMAIL_READONLY.value
DRIVE_SCOPE = GoogleScope.DRIVE_READONLY.value

# Refresh tokens Google hands out per user (round-tripped encrypted through the DB).
REFRESH_ALICE = "1//refresh-alice"
REFRESH_BOB = "1//refresh-bob"
# Fresh refresh token issued when bob reconnects after a needs_reauth flip.
REFRESH_BOB_RECONNECT = "1//refresh-bob-reconnected"

# Access tokens the token endpoint mints for each refresh token.
_ACCESS_BY_REFRESH: dict[str, str] = {
    REFRESH_ALICE: "at-alice",
    REFRESH_BOB: "at-bob",
    REFRESH_BOB_RECONNECT: "at-bob-2",
}


def _b64url(text: str) -> str:
    """Encode text as base64url without padding (Gmail's content encoding)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unsigned_id_token(email: str) -> str:
    """Build an unsigned JWT (header.payload.sig) carrying an ``email`` claim."""

    def _seg(data: dict[str, str]) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = _seg({"alg": "none", "typ": "JWT"})
    payload = _seg({"email": email, "sub": f"sub-{email}"})
    return f"{header}.{payload}.signature"


# --------------------------------------------------------------------------- #
# Fake Google server (token / revoke / userinfo) shared by callback + resolver
# --------------------------------------------------------------------------- #


@dataclass
class _FakeGoogleServer:
    """Stubs Google's token/revoke/userinfo endpoints via an httpx MockTransport.

    A single handler serves both the OAuth *callback* (``authorization_code``
    grant) and the *resolver* (``refresh_token`` grant). The callback path is
    keyed on a per-flow ``code -> (refresh_token, account_email)`` map registered
    just before each callback; the refresh path maps a refresh token to its access
    token via :data:`_ACCESS_BY_REFRESH`. Refresh tokens listed in
    ``invalid_grant_refresh_tokens`` return Google's revoked/expired signal.
    """

    granted_scope: str = f"{GMAIL_SCOPE} {DRIVE_SCOPE} openid email"
    # code -> (refresh_token, account_email) for pending authorization-code grants.
    codes: dict[str, tuple[str, str]] = field(default_factory=dict)
    invalid_grant_refresh_tokens: set[str] = field(default_factory=set)
    revoke_calls: list[str] = field(default_factory=list)

    def register_code(self, code: str, refresh_token: str, account_email: str) -> None:
        """Register the code an upcoming callback will exchange."""
        self.codes[code] = (refresh_token, account_email)

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            code = form.get("code", "")
            if code not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            refresh_token, account_email = self.codes[code]
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS_BY_REFRESH.get(refresh_token, "at-init"),
                    "expires_in": 3599,
                    "scope": self.granted_scope,
                    "token_type": "Bearer",
                    "refresh_token": refresh_token,
                    "id_token": _unsigned_id_token(account_email),
                },
            )
        if grant_type == "refresh_token":
            refresh_token = form.get("refresh_token", "")
            if refresh_token in self.invalid_grant_refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access_token = _ACCESS_BY_REFRESH.get(refresh_token)
            if access_token is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": access_token,
                    "expires_in": 3599,
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path
        if path.endswith("/token"):
            return self._handle_token(request)
        if path.endswith("/revoke"):
            form = {
                k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()
            }
            self.revoke_calls.append(form.get("token", ""))
            return httpx.Response(200, json={})
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"email": "unused@gmail.com"})
        return httpx.Response(404, json={"error": "not_found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))


# --------------------------------------------------------------------------- #
# Recording notifier — satisfies both the callback dispatcher and the resolver's
# Notifier protocol (send_notification with a keyword-only ``metadata``).
# --------------------------------------------------------------------------- #


@dataclass
class _RecordedNotification:
    user_identifier: str
    title: str
    body: str


@dataclass
class _RecordingNotifier:
    """Records notifications for both the connect notice and needs_reauth alerts."""

    sent: list[_RecordedNotification] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return True

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

    def for_user(self, user_id: str) -> list[_RecordedNotification]:
        return [n for n in self.sent if n.user_identifier == user_id]


@dataclass
class _FakeAuthService:
    """Minimal stub satisfying the integration enablement gate's auth check."""

    auth_enabled: bool = True

    async def get_user_from_api_token(self, auth_header: str, request: object) -> None:
        return None


# --------------------------------------------------------------------------- #
# Fake Google data-API backend keyed by access token (mailbox fixtures per user)
# --------------------------------------------------------------------------- #


@dataclass
class _FakeApiBackend:
    """A :class:`ApiBackend` serving canned mailbox payloads by access token.

    ``routes`` maps ``access_token -> {(method, url_suffix): payload}``. A payload
    that is ``bytes`` is returned verbatim (attachment/download bodies); anything
    else is JSON-encoded. Requests for an unknown token or route 404 — so a user
    whose token was never provisioned sees an empty mailbox rather than another
    user's data.
    """

    routes: dict[str, dict[tuple[str, str], object]] = field(default_factory=dict)
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    async def request(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> ApiResponse:
        self.requests.append((method, url, access_token))
        path = url.split("?", 1)[0]
        for (route_method, needle), payload in self.routes.get(
            access_token, {}
        ).items():
            if route_method == method and path.endswith(needle):
                content = (
                    payload
                    if isinstance(payload, bytes)
                    else json.dumps(payload).encode("utf-8")
                )
                return ApiResponse(status_code=200, content=content)
        return ApiResponse(status_code=404, content=b'{"error": "not found"}')


def _gmail_mailbox(message_id: str, subject: str) -> dict[tuple[str, str], object]:
    """A one-message mailbox: a search listing plus that message's metadata."""
    return {
        ("GET", "/users/me/messages"): {"messages": [{"id": message_id}]},
        ("GET", f"/messages/{message_id}"): {
            "id": message_id,
            "threadId": f"thread-{message_id}",
            "snippet": f"snippet for {subject}",
            "payload": {
                "headers": [
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                ]
            },
        },
    }


# --------------------------------------------------------------------------- #
# App / fixtures
# --------------------------------------------------------------------------- #


def _google_integration_config() -> GoogleIntegrationConfig:
    return GoogleIntegrationConfig.model_validate({
        "oauth_client_id": "test-client-id",
        "oauth_client_secret": "test-client-secret",
        "credential_encryption_key": CredentialEncryption.generate_key(),
        "scopes": [GMAIL_SCOPE, DRIVE_SCOPE],
        "require_taint_enforcement": False,
    })


@dataclass
class _E2EApp:
    app: FastAPI
    google_server: _FakeGoogleServer
    notifier: _RecordingNotifier
    integration: GoogleIntegrationConfig
    registry: AttachmentRegistry
    google_client: httpx.AsyncClient

    def set_user(self, user_id: str) -> None:
        """Override the session user for both the OAuth and attachment routes."""
        payload = {"user_identifier": user_id}
        self.app.dependency_overrides[get_current_user] = lambda: payload
        self.app.dependency_overrides[get_current_session_user] = lambda: payload


@pytest_asyncio.fixture
async def e2e(db_engine: AsyncEngine) -> AsyncGenerator[_E2EApp]:
    """Wire a FastAPI app with the Google OAuth + attachment routers, real storage."""
    temp_ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await temp_ctx.init_vector_db()

    integration = _google_integration_config()
    google_server = _FakeGoogleServer()
    notifier = _RecordingNotifier()
    registry = AttachmentRegistry(
        storage_path=tempfile.mkdtemp(), db_engine=db_engine, config=None
    )

    app = FastAPI()
    app.include_router(
        create_oauth_integration_router(GOOGLE_PROVIDER),
        prefix="/api/integrations/google",
    )
    app.include_router(attachments_api_router, prefix="/api/attachments")
    app.state.database_engine = db_engine
    app.state.config = AppConfig.model_validate({
        "database_url": str(db_engine.url),
        "google_integration": integration,
        "users": [
            {"id": "alice", "oidc": {"emails": ["alice@example.com"]}},
            {"id": "bob", "oidc": {"emails": ["bob@example.com"]}},
        ],
    })
    app.state.auth_service = _FakeAuthService(auth_enabled=True)
    state = evaluate_oauth_integration_state(
        GOOGLE_PROVIDER,
        app.state.config,
        auth_enabled=True,
        tool_required_scopes=GOOGLE_TOOL_REQUIRED_SCOPES,
    )
    app.state.oauth_integration_states = {"google": state}
    app.state.notification_dispatcher = notifier
    app.state.attachment_registry = registry

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
    app.dependency_overrides[get_current_user] = lambda: {"user_identifier": "alice"}
    app.dependency_overrides[get_current_session_user] = lambda: {
        "user_identifier": "alice"
    }

    try:
        yield _E2EApp(
            app=app,
            google_server=google_server,
            notifier=notifier,
            integration=integration,
            registry=registry,
            google_client=google_client,
        )
    finally:
        await google_client.aclose()


@pytest_asyncio.fixture
async def http(e2e: _E2EApp) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=e2e.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------- #
# Helpers exercising the REAL connect flow and the REAL resolver
# --------------------------------------------------------------------------- #


async def _connect_user(
    http: AsyncClient,
    e2e: _E2EApp,
    *,
    user_id: str,
    refresh_token: str,
    account_email: str,
) -> None:
    """Drive the full OAuth connect flow for ``user_id`` and assert it landed."""
    e2e.set_user(user_id)
    authorize = await http.get(
        "/api/integrations/google/authorize", follow_redirects=False
    )
    assert authorize.status_code == 302
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]

    code = f"code-for-{user_id}-{refresh_token[-6:]}"
    e2e.google_server.register_code(code, refresh_token, account_email)
    callback = await http.get(
        "/api/integrations/google/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/settings/accounts?google=connected"


async def _status(http: AsyncClient, e2e: _E2EApp, user_id: str) -> dict[str, object]:
    e2e.set_user(user_id)
    response = await http.get("/api/integrations/google")
    assert response.status_code == 200
    return response.json()


async def _fetch_connection(
    db_engine: AsyncEngine, user_id: str
) -> OAuthConnectionModel | None:
    db = Database(engine=db_engine)
    return await db.oauth_connections.get_connection(user_id, "google")


def _real_resolver(e2e: _E2EApp) -> OAuthCredentialResolver:
    """Construct the REAL resolver against the same fake Google token endpoint."""
    return OAuthCredentialResolver(
        provider=GOOGLE_PROVIDER,
        config=e2e.integration,
        encryption=CredentialEncryption(e2e.integration.credential_encryption_key),
        http_client=e2e.google_server.client(),
        notifier=e2e.notifier,
        token_endpoint=e2e.app.state.oauth_url_overrides["google"]["token"],
    )


def _exec_context(
    db: Database,
    *,
    user_id: str | None,
    resolver: OAuthCredentialResolver,
    backend: _FakeApiBackend,
    registry: AttachmentRegistry | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-google-e2e",
        user_name="Test User",
        turn_id="turn-e2e",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=registry,
        camera_backend=None,
        credential_resolvers={"google": resolver},
        api_backend=cast("ApiBackend | None", backend),
        timezone=ZoneInfo("UTC"),
        user_id=user_id,
    )


# --------------------------------------------------------------------------- #
# 1 + 2. Connect two users, then read as each through the real resolver.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_two_users_connect_and_read_only_own_mailbox(
    http: AsyncClient, e2e: _E2EApp, db_engine: AsyncEngine
) -> None:
    # 1. Connect alice and bob through the real OAuth flow.
    await _connect_user(
        http,
        e2e,
        user_id="alice",
        refresh_token=REFRESH_ALICE,
        account_email="alice@gmail.com",
    )
    await _connect_user(
        http,
        e2e,
        user_id="bob",
        refresh_token=REFRESH_BOB,
        account_email="bob@gmail.com",
    )

    alice_status = await _status(http, e2e, "alice")
    bob_status = await _status(http, e2e, "bob")
    assert alice_status["connected"] is True
    assert alice_status["provider_account_email"] == "alice@gmail.com"
    assert bob_status["connected"] is True
    assert bob_status["provider_account_email"] == "bob@gmail.com"

    # The refresh token round-tripped ENCRYPTED through the DB.
    alice_row = await _fetch_connection(db_engine, "alice")
    assert alice_row is not None
    assert alice_row.refresh_token_encrypted != REFRESH_ALICE
    assert (
        CredentialEncryption(e2e.integration.credential_encryption_key).decrypt(
            alice_row.refresh_token_encrypted
        )
        == REFRESH_ALICE
    )

    # 2. Read as each user through the REAL resolver: refresh-alice -> at-alice
    # (which sees alice's mailbox), refresh-bob -> at-bob (bob's mailbox).
    resolver = _real_resolver(e2e)
    backend = _FakeApiBackend(
        routes={
            "at-alice": _gmail_mailbox("msg-alice", "Alice mailbox only"),
            "at-bob": _gmail_mailbox("msg-bob", "Bob mailbox only"),
        }
    )
    db = Database(engine=db_engine)
    alice_ctx = _exec_context(db, user_id="alice", resolver=resolver, backend=backend)
    bob_ctx = _exec_context(db, user_id="bob", resolver=resolver, backend=backend)
    alice_result = await gmail_search_tool(alice_ctx, query="anything")
    bob_result = await gmail_search_tool(bob_ctx, query="anything")

    alice_data = alice_result.get_data()
    bob_data = bob_result.get_data()
    assert isinstance(alice_data, dict)
    assert isinstance(bob_data, dict)
    assert [m["subject"] for m in alice_data["messages"]] == ["Alice mailbox only"]
    assert [m["subject"] for m in bob_data["messages"]] == ["Bob mailbox only"]

    # The resolver actually obtained at-alice/at-bob from the encrypted refresh
    # tokens — proving the DB round-trip + real refresh produced the right token.
    assert any(token == "at-alice" for _, _, token in backend.requests)
    assert any(token == "at-bob" for _, _, token in backend.requests)

    # last_used_at reflects the successful data API use.
    db = Database(engine=db_engine)
    alice_conn = await db.oauth_connections.get_connection("alice", "google")
    assert alice_conn is not None
    assert alice_conn.last_used_at is not None


# --------------------------------------------------------------------------- #
# 3. Fail-closed matrix.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fail_closed_unconnected_no_user_and_after_disconnect(
    http: AsyncClient, e2e: _E2EApp, db_engine: AsyncEngine
) -> None:
    await _connect_user(
        http,
        e2e,
        user_id="alice",
        refresh_token=REFRESH_ALICE,
        account_email="alice@gmail.com",
    )

    resolver = _real_resolver(e2e)
    backend = _FakeApiBackend(
        routes={"at-alice": _gmail_mailbox("msg-alice", "Alice mailbox only")}
    )

    db = Database(engine=db_engine)
    # carol has no connection -> actionable "connect from Settings" error.
    carol_ctx = _exec_context(db, user_id="carol", resolver=resolver, backend=backend)
    carol_result = await gmail_search_tool(carol_ctx, query="anything")
    assert "connect from settings" in carol_result.get_text().lower()

    # No acting user (system/ambient) -> fail closed.
    ambient_ctx = _exec_context(db, user_id=None, resolver=resolver, backend=backend)
    ambient_result = await gmail_search_tool(ambient_ctx, query="anything")
    assert "acting user" in ambient_result.get_text().lower()

    # alice disconnects: the revoke endpoint sees her decrypted refresh token.
    e2e.set_user("alice")
    disconnect = await http.request("DELETE", "/api/integrations/google")
    assert disconnect.status_code == 204
    assert REFRESH_ALICE in e2e.google_server.revoke_calls

    # Her next read now fails closed with the not-connected message.
    db = Database(engine=db_engine)
    alice_ctx = _exec_context(db, user_id="alice", resolver=resolver, backend=backend)
    after = await gmail_search_tool(alice_ctx, query="anything")
    assert "connect from settings" in after.get_text().lower()


# --------------------------------------------------------------------------- #
# 4. Attachment ownership across the tool + HTTP stack.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_attachment_ownership_across_stack(
    http: AsyncClient, e2e: _E2EApp, db_engine: AsyncEngine
) -> None:
    await _connect_user(
        http,
        e2e,
        user_id="alice",
        refresh_token=REFRESH_ALICE,
        account_email="alice@gmail.com",
    )

    attachment_bytes = b"PDF-PERMISSION-FORM-BYTES"
    resolver = _real_resolver(e2e)
    backend = _FakeApiBackend(
        routes={
            "at-alice": {
                ("GET", "/attachments/att-1"): {
                    "data": base64
                    .urlsafe_b64encode(attachment_bytes)
                    .decode("ascii")
                    .rstrip("=")
                },
                ("GET", "/messages/msg-1"): {
                    "id": "msg-1",
                    "payload": {
                        "mimeType": "multipart/mixed",
                        "filename": "",
                        "body": {},
                        "parts": [
                            {
                                "mimeType": "application/pdf",
                                "filename": "form.pdf",
                                "body": {
                                    "attachmentId": "att-1",
                                    "size": len(attachment_bytes),
                                },
                            }
                        ],
                    },
                },
            }
        }
    )

    db = Database(engine=db_engine)
    alice_ctx = _exec_context(
        db,
        user_id="alice",
        resolver=resolver,
        backend=backend,
        registry=e2e.registry,
    )
    result = await gmail_get_attachment_tool(
        alice_ctx, message_id="msg-1", attachment_id="att-1", filename="form.pdf"
    )
    data = result.get_data()
    assert isinstance(data, dict)
    attachment_id = data["attachment_id"]
    assert result.attachments is not None
    assert result.attachments[0].attachment_id == attachment_id

    # HTTP content route as alice (owner): served with private, no-store.
    e2e.set_user("alice")
    owner_response = await http.get(f"/api/attachments/{attachment_id}")
    assert owner_response.status_code == 200
    assert owner_response.content == attachment_bytes
    assert owner_response.headers["Cache-Control"] == "private, no-store"

    # HTTP content route as bob (non-owner): 404.
    e2e.set_user("bob")
    assert (await http.get(f"/api/attachments/{attachment_id}")).status_code == 404

    # Metadata route: alice can read it, bob 404s.
    e2e.set_user("alice")
    alice_meta = await http.get(f"/api/attachments/{attachment_id}/metadata")
    assert alice_meta.status_code == 200
    assert alice_meta.json()["id"] == attachment_id

    e2e.set_user("bob")
    assert (
        await http.get(f"/api/attachments/{attachment_id}/metadata")
    ).status_code == 404


# --------------------------------------------------------------------------- #
# 5. needs_reauth journey: invalid_grant on refresh, then reconnect.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_needs_reauth_then_reconnect(
    http: AsyncClient, e2e: _E2EApp, db_engine: AsyncEngine
) -> None:
    await _connect_user(
        http,
        e2e,
        user_id="bob",
        refresh_token=REFRESH_BOB,
        account_email="bob@gmail.com",
    )

    # Google now rejects bob's refresh token as revoked/expired.
    e2e.google_server.invalid_grant_refresh_tokens.add(REFRESH_BOB)

    resolver = _real_resolver(e2e)
    backend = _FakeApiBackend(
        routes={"at-bob-2": _gmail_mailbox("msg-bob", "Bob mailbox only")}
    )

    db = Database(engine=db_engine)
    bob_ctx = _exec_context(db, user_id="bob", resolver=resolver, backend=backend)
    failed = await gmail_search_tool(bob_ctx, query="anything")
    assert "re-authorized" in failed.get_text().lower()

    # Status endpoint reflects needs_reauth; bob was notified.
    bob_status = await _status(http, e2e, "bob")
    assert bob_status["status"] == "needs_reauth"
    assert any(
        "re-authorization" in n.body.lower() or "reconnect" in n.body.lower()
        for n in e2e.notifier.for_user("bob")
    )

    # bob reconnects with a fresh refresh token -> reads work again.
    await _connect_user(
        http,
        e2e,
        user_id="bob",
        refresh_token=REFRESH_BOB_RECONNECT,
        account_email="bob@gmail.com",
    )
    reconnect_status = await _status(http, e2e, "bob")
    assert reconnect_status["status"] == "active"

    # A brand-new resolver (fresh cache) reads bob's mailbox via at-bob-2.
    resolver_after = _real_resolver(e2e)
    db = Database(engine=db_engine)
    bob_ctx = _exec_context(db, user_id="bob", resolver=resolver_after, backend=backend)
    ok = await gmail_search_tool(bob_ctx, query="anything")
    ok_data = ok.get_data()
    assert isinstance(ok_data, dict)
    assert [m["subject"] for m in ok_data["messages"]] == ["Bob mailbox only"]


# --------------------------------------------------------------------------- #
# 6. Taint: a Gmail read taints the turn unknown_external (descriptor-level).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gmail_read_taints_turn_unknown_external(
    http: AsyncClient, e2e: _E2EApp, db_engine: AsyncEngine
) -> None:
    await _connect_user(
        http,
        e2e,
        user_id="alice",
        refresh_token=REFRESH_ALICE,
        account_email="alice@gmail.com",
    )

    resolver = _real_resolver(e2e)
    backend = _FakeApiBackend(
        routes={"at-alice": _gmail_mailbox("msg-alice", "Alice mailbox only")}
    )
    db = Database(engine=db_engine)
    alice_ctx = _exec_context(db, user_id="alice", resolver=resolver, backend=backend)
    result = await gmail_search_tool(alice_ctx, query="anything")
    # The read succeeded (real chain), and the tool's own result taints the turn.
    assert isinstance(result.get_data(), dict)

    registration = next(r for r in LOCAL_TOOL_REGISTRATIONS if r.name == "gmail_search")
    descriptor = build_tool_descriptor(
        registration.definition, registration.tags, origin="local"
    )
    source = derive_tool_result_taint_source(descriptor=descriptor, call_id=None)
    assert source is not None
    assert source.tier is SourceTrustTier.UNKNOWN_EXTERNAL
