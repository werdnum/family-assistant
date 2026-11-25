"""Unit tests for Link OAuth enablement and rotating-refresh credential resolution.

Uses real :class:`OAuthConnectionsRepository` on the ``db_engine`` fixture and
an ``httpx.AsyncBaseTransport`` fake Link token server (no mocking of internals,
no ``asyncio.sleep``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from family_assistant.config_models import (
    AppConfig,
    GoogleIntegrationConfig,
    LinkIntegrationConfig,
    TaintPolicyConfig,
)
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.services.effective_tool_registry import (
    build_effective_local_tool_registrations,
)
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.link_provider import (
    LINK_DEFAULT_OAUTH_CLIENT_ID,
    LINK_PROVIDER,
    LinkScope,
)
from family_assistant.services.oauth_credentials import (
    OAuthCredentialResolver,
    OAuthReauthRequiredError,
    OAuthRefreshFailedError,
)
from family_assistant.services.oauth_integration_state import (
    OAuthIntegrationState,
    evaluate_oauth_integration_state,
    oauth_integration_configured,
)
from family_assistant.storage.database import Database
from family_assistant.tools.link_wallet import LINK_TOOL_REQUIRED_SCOPES
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

USER_ID = "user-link-1"
AGENTIC_SCOPE = LinkScope.PAYMENT_METHODS_AGENTIC.value
USERINFO_SCOPE = LinkScope.USERINFO_READ.value


def _users() -> list[dict[str, object]]:
    return [{"id": USER_ID, "oidc": {"emails": ["alice@example.com"]}}]


def _link_config(**overrides: object) -> LinkIntegrationConfig:
    base: dict[str, object] = {
        "enabled": True,
        "oauth_client_id": LINK_DEFAULT_OAUTH_CLIENT_ID,
        "oauth_client_secret": "",
        "credential_encryption_key": generate_key(),
        "scopes": [AGENTIC_SCOPE, USERINFO_SCOPE],
        "require_taint_enforcement": False,
    }
    base.update(overrides)
    return LinkIntegrationConfig.model_validate(base)


def _app_config(
    link: LinkIntegrationConfig,
    *,
    users: list[dict[str, object]] | None = None,
    taint_mode: str = "observe",
) -> AppConfig:
    return AppConfig.model_validate({
        "database_url": "sqlite+aiosqlite:///:memory:",
        "link_integration": link,
        "taint_policy": TaintPolicyConfig.model_validate({"mode": taint_mode}),
        "users": _users() if users is None else users,
    })


def _eval_link(
    config: AppConfig,
    *,
    auth_enabled: bool = True,
) -> OAuthIntegrationState:
    return evaluate_oauth_integration_state(
        LINK_PROVIDER,
        config,
        auth_enabled=auth_enabled,
        tool_required_scopes=LINK_TOOL_REQUIRED_SCOPES,
        shared_tool_required_scopes={},
    )


# --------------------------------------------------------------------------- #
# OAuthIntegrationState for Link
# --------------------------------------------------------------------------- #


@pytest.mark.no_db
def test_link_disabled_by_default_even_with_key_and_public_client_id() -> None:
    """Default Link config stays quietly disabled even when encryption key is set."""
    cfg = LinkIntegrationConfig(
        credential_encryption_key=SecretStr(generate_key()),
    )
    assert cfg.enabled is False
    assert cfg.oauth_client_id == LINK_DEFAULT_OAUTH_CLIENT_ID
    assert oauth_integration_configured(cfg) is False

    state = _eval_link(_app_config(cfg), auth_enabled=True)
    assert state.enabled is False
    assert state.reason == "Link integration is not configured."
    assert state.taint_enforcement_waived is False
    assert state.enabled_tool_names == frozenset()


@pytest.mark.no_db
def test_link_enabled_with_public_client_and_observe_taint_mode() -> None:
    """Link allows empty client_secret and observe taint mode without waiver banner."""
    cfg = _link_config()
    assert oauth_integration_configured(cfg) is True

    state = _eval_link(_app_config(cfg, taint_mode="observe"), auth_enabled=True)
    assert state.enabled is True
    assert state.reason is None
    assert state.taint_enforcement_waived is False
    assert state.enabled_tool_names == frozenset(LINK_TOOL_REQUIRED_SCOPES)


@pytest.mark.no_db
def test_link_refuses_dev_test_user_mode() -> None:
    state = _eval_link(_app_config(_link_config()), auth_enabled=False)
    assert state.enabled is False
    assert state.reason is not None
    assert "test_user" in state.reason
    assert state.enabled_tool_names == frozenset()


@pytest.mark.no_db
def test_link_refuses_empty_users_block() -> None:
    state = _eval_link(_app_config(_link_config(), users=[]), auth_enabled=True)
    assert state.enabled is False
    assert state.reason is not None
    assert "users" in state.reason


@pytest.mark.no_db
def test_link_refuses_financial_insights_scopes() -> None:
    cfg = _link_config(
        scopes=[AGENTIC_SCOPE, USERINFO_SCOPE, "financial_insights.transactions:read"]
    )
    state = _eval_link(_app_config(cfg), auth_enabled=True)
    assert state.enabled is False
    assert state.reason is not None
    assert "financial_insights.transactions:read" in state.reason
    assert state.enabled_tool_names == frozenset()


@pytest.mark.no_db
def test_link_requires_encryption_key_when_enabled() -> None:
    cfg = _link_config(credential_encryption_key="")
    state = _eval_link(_app_config(cfg), auth_enabled=True)
    assert state.enabled is False
    assert state.reason is not None
    assert "CREDENTIAL_ENCRYPTION_KEY" in state.reason


@pytest.mark.no_db
def test_effective_registry_filters_link_tools_by_state() -> None:
    config = _app_config(_link_config())
    google_disabled = OAuthIntegrationState(
        provider="google",
        enabled=False,
        reason="Google integration is not configured.",
        taint_enforcement_waived=False,
        enabled_tool_names=frozenset(),
        governed_tool_names=frozenset(),
    )

    # Default (omitted link_integration_state) fails closed.
    default_names = {
        reg.name
        for reg in build_effective_local_tool_registrations(config, google_disabled)
    }
    assert default_names.isdisjoint(LINK_TOOL_REQUIRED_SCOPES)

    # Enabled Link state exposes all 5 Link tools.
    link_enabled = _eval_link(config, auth_enabled=True)
    enabled_names = {
        reg.name
        for reg in build_effective_local_tool_registrations(
            config, google_disabled, link_integration_state=link_enabled
        )
    }
    assert frozenset(LINK_TOOL_REQUIRED_SCOPES).issubset(enabled_names)


# --------------------------------------------------------------------------- #
# OAuthCredentialResolver: rotating refresh tokens, seeding, Link 400 error
# --------------------------------------------------------------------------- #


class _RecordingTokenTransport(httpx.AsyncBaseTransport):
    """Records POST form bodies and returns scripted token responses."""

    def __init__(
        self,
        responses: list[httpx.Response],
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._responses = responses
        self._gate = gate
        self.calls: list[dict[str, str]] = []
        self.started = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        index = len(self.calls)
        self.calls.append(form)
        self.started.set()
        if self._gate is not None:
            await self._gate.wait()
        return self._responses[index]


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

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
        self.sent.append((user_identifier, title, body))


@pytest_asyncio.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    yield Database(engine=db_engine)


def _exec_context(db: Database, user_id: str | None = USER_ID) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-link-1",
        user_name="Alice",
        user_id=user_id,
        turn_id="turn-1",
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
    )


async def _seed_link_connection(
    db: Database,
    encryption: CredentialEncryption,
    *,
    refresh_token: str = "rt-initial",
) -> str:
    conn = await db.oauth_connections.upsert_connection(
        user_id=USER_ID,
        provider=LINK_PROVIDER.name,
        provider_account_email="alice@example.com",
        scopes=[AGENTIC_SCOPE, USERINFO_SCOPE],
        refresh_token_encrypted=encryption.encrypt(refresh_token),
    )
    return conn.credential_generation


@pytest.mark.asyncio
async def test_link_refresh_omits_client_secret_and_persists_rotated_refresh_token(
    db_context: Database,
) -> None:
    key = generate_key()
    encryption = CredentialEncryption(key)
    generation = await _seed_link_connection(
        db_context, encryption, refresh_token="rt-1"
    )
    clock = MockClock(datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    transport = _RecordingTokenTransport([
        httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
        httpx.Response(
            200,
            json={
                "access_token": "at-2",
                "refresh_token": "rt-3",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        ),
    ])
    http_client = httpx.AsyncClient(transport=transport)
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER,
        _link_config(credential_encryption_key=key, oauth_client_secret=""),
        encryption,
        http_client,
        notifier=None,
        clock=clock,
    )
    ctx = _exec_context(db_context)

    try:
        token1 = await resolver.access_token_for(ctx, AGENTIC_SCOPE)
        assert token1 == "at-1"
        assert len(transport.calls) == 1
        assert "client_secret" not in transport.calls[0]
        assert transport.calls[0]["refresh_token"] == "rt-1"
        assert transport.calls[0]["client_id"] == LINK_DEFAULT_OAUTH_CLIENT_ID

        # DB row now holds rt-2 and kept the same credential_generation.
        row = await db_context.oauth_connections.get_connection(
            USER_ID, LINK_PROVIDER.name
        )
        assert row is not None
        assert row.credential_generation == generation
        assert encryption.decrypt(row.refresh_token_encrypted) == "rt-2"

        # Advance past expiry: next refresh MUST present the rotated rt-2.
        clock.advance(timedelta(seconds=3600))
        token2 = await resolver.access_token_for(ctx, AGENTIC_SCOPE)
        assert token2 == "at-2"
        assert len(transport.calls) == 2
        assert transport.calls[1]["refresh_token"] == "rt-2"

        row2 = await db_context.oauth_connections.get_connection(
            USER_ID, LINK_PROVIDER.name
        )
        assert row2 is not None
        assert row2.credential_generation == generation
        assert encryption.decrypt(row2.refresh_token_encrypted) == "rt-3"
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_seed_access_token_avoids_immediate_refresh(
    db_context: Database,
) -> None:
    key = generate_key()
    encryption = CredentialEncryption(key)
    generation = await _seed_link_connection(
        db_context, encryption, refresh_token="rt-1"
    )
    clock = MockClock(datetime(2026, 11, 24, 12, 0, tzinfo=UTC))
    transport = _RecordingTokenTransport([
        httpx.Response(
            200,
            json={
                "access_token": "at-refreshed",
                "refresh_token": "rt-2",
                "expires_in": 3600,
            },
        ),
    ])
    http_client = httpx.AsyncClient(transport=transport)
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER,
        _link_config(credential_encryption_key=key),
        encryption,
        http_client,
        notifier=None,
        clock=clock,
    )
    ctx = _exec_context(db_context)

    try:
        resolver.seed_access_token(USER_ID, generation, "at-seeded", expires_in=3600)
        token = await resolver.access_token_for(ctx, AGENTIC_SCOPE)
        assert token == "at-seeded"
        assert transport.calls == []

        # Once within the refresh margin, it refreshes using rt-1.
        clock.advance(timedelta(seconds=3550))
        refreshed = await resolver.access_token_for(ctx, AGENTIC_SCOPE)
        assert refreshed == "at-refreshed"
        assert len(transport.calls) == 1
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_link_redeemed_400_marks_needs_reauth_and_notifies(
    db_context: Database,
) -> None:
    key = generate_key()
    encryption = CredentialEncryption(key)
    generation = await _seed_link_connection(
        db_context, encryption, refresh_token="rt-redeemed"
    )
    transport = _RecordingTokenTransport([
        httpx.Response(
            400,
            json={
                "error": {
                    "message": "refresh_token is not active: status: redeemed",
                }
            },
        ),
    ])
    notifier = _RecordingNotifier()
    http_client = httpx.AsyncClient(transport=transport)
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER,
        _link_config(credential_encryption_key=key),
        encryption,
        http_client,
        notifier=notifier,
    )
    ctx = _exec_context(db_context)

    try:
        with pytest.raises(OAuthReauthRequiredError):
            await resolver.access_token_for(ctx, AGENTIC_SCOPE)

        row = await db_context.oauth_connections.get_connection(
            USER_ID, LINK_PROVIDER.name
        )
        assert row is not None
        assert row.status == "needs_reauth"
        assert row.credential_generation != generation
        assert len(notifier.sent) == 1
        assert notifier.sent[0][0] == USER_ID
        assert "Link" in notifier.sent[0][1]
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_google_does_not_treat_link_error_shape_as_invalid_grant(
    db_context: Database,
) -> None:
    """Google keeps strict RFC 6749 invalid_grant classification."""
    key = generate_key()
    encryption = CredentialEncryption(key)
    await db_context.oauth_connections.upsert_connection(
        user_id=USER_ID,
        provider=GOOGLE_PROVIDER.name,
        provider_account_email="alice@gmail.com",
        scopes=[GoogleScope.GMAIL_READONLY.value],
        refresh_token_encrypted=encryption.encrypt("google-rt"),
    )
    transport = _RecordingTokenTransport([
        httpx.Response(400, json={"error": {"message": "some other error"}}),
    ])
    http_client = httpx.AsyncClient(transport=transport)
    resolver = OAuthCredentialResolver(
        GOOGLE_PROVIDER,
        GoogleIntegrationConfig(
            oauth_client_id="cid",
            oauth_client_secret=SecretStr("csec"),
            credential_encryption_key=SecretStr(key),
        ),
        encryption,
        http_client,
        notifier=None,
    )
    ctx = _exec_context(db_context)

    try:
        with pytest.raises(OAuthRefreshFailedError):
            await resolver.access_token_for(ctx, GoogleScope.GMAIL_READONLY.value)

        row = await db_context.oauth_connections.get_connection(
            USER_ID, GOOGLE_PROVIDER.name
        )
        assert row is not None
        assert row.status == "active"
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_rotated_refresh_does_not_overwrite_concurrent_reconnect(
    db_context: Database,
) -> None:
    key = generate_key()
    encryption = CredentialEncryption(key)
    old_gen = await _seed_link_connection(
        db_context, encryption, refresh_token="rt-old"
    )
    gate = asyncio.Event()
    transport = _RecordingTokenTransport(
        [
            httpx.Response(
                200,
                json={
                    "access_token": "at-from-old",
                    "refresh_token": "rt-rotated-stale",
                    "expires_in": 3600,
                },
            )
        ],
        gate=gate,
    )
    http_client = httpx.AsyncClient(transport=transport)
    resolver = OAuthCredentialResolver(
        LINK_PROVIDER,
        _link_config(credential_encryption_key=key),
        encryption,
        http_client,
        notifier=None,
    )
    ctx = _exec_context(db_context)

    try:
        task = asyncio.create_task(resolver.access_token_for(ctx, AGENTIC_SCOPE))
        await asyncio.wait_for(transport.started.wait(), timeout=5)

        # User reconnects while the old refresh is in flight.
        new_gen = await _seed_link_connection(
            db_context, encryption, refresh_token="rt-new-connect"
        )
        assert new_gen != old_gen
        resolver.seed_access_token(USER_ID, new_gen, "at-new-seeded", expires_in=3600)

        gate.set()
        await asyncio.wait_for(task, timeout=5)

        row = await db_context.oauth_connections.get_connection(
            USER_ID, LINK_PROVIDER.name
        )
        assert row is not None
        assert row.credential_generation == new_gen
        assert encryption.decrypt(row.refresh_token_encrypted) == "rt-new-connect"

        # Seeded token for the replacement connection survived!
        assert await resolver.access_token_for(ctx, AGENTIC_SCOPE) == "at-new-seeded"
    finally:
        gate.set()
        await http_client.aclose()
