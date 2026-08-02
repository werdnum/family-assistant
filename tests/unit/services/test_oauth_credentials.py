"""Unit tests for :class:`OAuthCredentialResolver`.

Uses the real ``OAuthConnectionsRepository`` against the ``db_engine`` fixture
and a custom ``httpx.AsyncBaseTransport`` for the token endpoint (no mocking of
internals, no ``asyncio.sleep``; concurrency is gated with ``asyncio.Event``).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from family_assistant.config_models import GoogleIntegrationConfig
from family_assistant.services.credential_encryption import (
    CredentialDecryptionError,
    CredentialEncryption,
    generate_key,
)
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope
from family_assistant.services.oauth_credentials import (
    OAuthCredentialResolver,
    OAuthNoActingUserError,
    OAuthNotConnectedError,
    OAuthReauthRequiredError,
    OAuthRefreshFailedError,
    OAuthScopeNotGrantedError,
)
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.notifier import NotificationMetadata

GMAIL = GoogleScope.GMAIL_READONLY.value
DRIVE = GoogleScope.DRIVE_READONLY.value
USER_ID = "user-a"
PROVIDER = "google"


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Async transport that counts calls and returns scripted token responses.

    A response is either an ``httpx.Response`` or a zero-arg callable returning
    one; callables are consumed in order and a default backs any overflow. When
    ``gate`` is set, each handler awaits it before responding, so concurrency can
    be gated deterministically with an :class:`asyncio.Event`.
    """

    def __init__(
        self,
        responses: list[httpx.Response],
        *,
        gate: asyncio.Event | None = None,
        default: httpx.Response | None = None,
    ) -> None:
        self._responses = responses
        self._gate = gate
        self._default = default or _ok_token("default-token")
        self.calls = 0
        self.started = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        index = self.calls
        self.calls += 1
        self.started.set()
        if self._gate is not None:
            await self._gate.wait()
        if index < len(self._responses):
            return self._responses[index]
        return self._default


def _ok_token(token: str, expires_in: int = 3600) -> httpx.Response:
    """A 200 token response with the given access token and lifetime."""
    return httpx.Response(
        200,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
    )


def _invalid_grant() -> httpx.Response:
    """A 400 ``invalid_grant`` token response (revoked/expired)."""
    return httpx.Response(400, json={"error": "invalid_grant"})


class _RecordingNotifier:
    """A fake notifier that records the notifications it is asked to send."""

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
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.sent.append((user_identifier, title, body))


@pytest_asyncio.fixture
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[Database]:
    """An entered Database backed by the test database."""
    ctx = Database(engine=db_engine)
    yield ctx


def _exec_context(db_context: Database, user_id: str | None) -> ToolExecutionContext:
    """A minimal ToolExecutionContext carrying only user_id and db_context."""
    return ToolExecutionContext(
        interface_type="web",
        conversation_id="conv-1",
        user_name="Tester",
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=__import__("zoneinfo").ZoneInfo("UTC"),
        user_id=user_id,
        credential_resolvers=None,
        api_backend=None,
    )


def _resolver(
    encryption: CredentialEncryption,
    transport: _ScriptedTransport | None = None,
    notifier: _RecordingNotifier | None = None,
) -> OAuthCredentialResolver:
    """Build a resolver over a scripted transport (default: one OK token)."""
    transport = transport or _ScriptedTransport([_ok_token("access-1")])
    client = httpx.AsyncClient(transport=transport)
    config = GoogleIntegrationConfig(
        oauth_client_id="client-id", oauth_client_secret="client-secret"
    )
    return OAuthCredentialResolver(
        provider=GOOGLE_PROVIDER,
        config=config,
        encryption=encryption,
        http_client=client,
        notifier=notifier,
    )


async def _seed_connection(
    db_context: Database,
    encryption: CredentialEncryption,
    *,
    user_id: str = USER_ID,
    scopes: list[str] | None = None,
    refresh_token: str = "refresh-token-1",
) -> str:
    """Seed a connection row and return its credential_generation."""
    connection = await db_context.oauth_connections.upsert_connection(
        user_id=user_id,
        provider=PROVIDER,
        provider_account_email="user@example.com",
        scopes=scopes if scopes is not None else [GMAIL, DRIVE],
        refresh_token_encrypted=encryption.encrypt(refresh_token),
    )
    return connection.credential_generation


# --------------------------------------------------------------------------- #
# Fail-closed matrix
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_acting_user_fails_closed(db_context: Database) -> None:
    resolver = _resolver(CredentialEncryption(generate_key()))
    exec_context = _exec_context(db_context, user_id=None)

    with pytest.raises(OAuthNoActingUserError):
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)


@pytest.mark.asyncio
async def test_no_connection_fails_closed(db_context: Database) -> None:
    resolver = _resolver(CredentialEncryption(generate_key()))
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(OAuthNotConnectedError) as exc_info:
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    assert "connect from Settings" in str(exc_info.value)


@pytest.mark.asyncio
async def test_needs_reauth_status_fails_closed(db_context: Database) -> None:
    encryption = CredentialEncryption(generate_key())
    generation = await _seed_connection(db_context, encryption)
    await db_context.oauth_connections.mark_needs_reauth(
        USER_ID, PROVIDER, expected_generation=generation
    )
    resolver = _resolver(encryption)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(OAuthReauthRequiredError) as exc_info:
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    assert "re-authorized" in str(exc_info.value)


@pytest.mark.asyncio
async def test_scope_not_granted_fails_closed(db_context: Database) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption, scopes=[GMAIL])
    resolver = _resolver(encryption)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(OAuthScopeNotGrantedError) as exc_info:
        await resolver.access_token_for(exec_context, GoogleScope.DRIVE_READONLY)

    assert DRIVE in str(exc_info.value)
    assert "reconnect from Settings" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Happy path + caching
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_happy_path_refresh_caches_token(
    db_context: Database,
) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption, refresh_token="refresh-abc")
    transport = _ScriptedTransport([_ok_token("access-xyz")])
    resolver = _resolver(encryption, transport=transport)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    token = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    assert token == "access-xyz"

    second = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    assert second == "access-xyz"
    assert transport.calls == 1  # cache hit, no second POST

    connection = await db_context.oauth_connections.get_connection(USER_ID, PROVIDER)
    assert connection is not None
    # last_used_at reflects successful API use, not token refreshes; the
    # tools' request helper records it after a 2xx data response.
    assert connection.last_used_at is None


@pytest.mark.asyncio
async def test_refresh_uses_decrypted_refresh_token(
    db_context: Database,
) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption, refresh_token="secret-refresh")

    captured: list[str] = []

    class _CapturingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8")
            captured.append(body)
            return _ok_token("access-1")

    client = httpx.AsyncClient(transport=_CapturingTransport())
    config = GoogleIntegrationConfig(
        oauth_client_id="cid", oauth_client_secret="csecret"
    )
    resolver = OAuthCredentialResolver(
        provider=GOOGLE_PROVIDER,
        config=config,
        encryption=encryption,
        http_client=client,
        notifier=None,
    )
    exec_context = _exec_context(db_context, user_id=USER_ID)

    await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    assert "grant_type=refresh_token" in captured[0]
    assert "refresh_token=secret-refresh" in captured[0]


# --------------------------------------------------------------------------- #
# Single-flight
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrent_calls_single_flight(db_context: Database) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption)
    gate = asyncio.Event()
    transport = _ScriptedTransport([_ok_token("access-single")], gate=gate)
    resolver = _resolver(encryption, transport=transport)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    tasks = [
        asyncio.create_task(
            resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
        )
        for _ in range(8)
    ]
    # Let the first refresh reach the transport, then release everyone.
    await transport.started.wait()
    gate.set()
    tokens = await asyncio.gather(*tasks)

    assert all(token == "access-single" for token in tokens)
    assert transport.calls == 1


# --------------------------------------------------------------------------- #
# invalid_grant → needs_reauth + notification
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_grant_marks_needs_reauth_and_notifies(
    db_context: Database,
) -> None:
    encryption = CredentialEncryption(generate_key())
    generation = await _seed_connection(db_context, encryption)
    transport = _ScriptedTransport([_invalid_grant()])
    notifier = _RecordingNotifier()
    resolver = _resolver(encryption, transport=transport, notifier=notifier)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(OAuthReauthRequiredError):
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    connection = await db_context.oauth_connections.get_connection(USER_ID, PROVIDER)
    assert connection is not None
    assert connection.status == "needs_reauth"
    assert connection.credential_generation != generation  # rotated
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == USER_ID

    # A subsequent call fails on the needs_reauth status without another POST.
    with pytest.raises(OAuthReauthRequiredError):
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    assert transport.calls == 1


# --------------------------------------------------------------------------- #
# Stale-generation protection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_generation_does_not_flip_replacement(
    db_context: Database,
) -> None:
    encryption = CredentialEncryption(generate_key())
    old_generation = await _seed_connection(db_context, encryption)

    gate = asyncio.Event()
    transport = _ScriptedTransport([_invalid_grant()], gate=gate)
    notifier = _RecordingNotifier()
    resolver = _resolver(encryption, transport=transport, notifier=notifier)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    # Start a refresh that reads the OLD generation, then reconnect before it
    # completes (simulated by holding the transport at the gate).
    task = asyncio.create_task(
        resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    )
    await transport.started.wait()
    new_generation = await _seed_connection(db_context, encryption)
    assert new_generation != old_generation
    gate.set()

    with pytest.raises(OAuthReauthRequiredError):
        await task

    connection = await db_context.oauth_connections.get_connection(USER_ID, PROVIDER)
    assert connection is not None
    assert connection.status == "active"  # replacement survives
    assert connection.credential_generation == new_generation
    assert notifier.sent == []  # no notification for a stale-generation failure


# --------------------------------------------------------------------------- #
# Reconnect invalidates cache
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconnect_invalidates_cache(db_context: Database) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption)
    transport = _ScriptedTransport([_ok_token("token-gen1"), _ok_token("token-gen2")])
    resolver = _resolver(encryption, transport=transport)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    first = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    assert first == "token-gen1"

    # Reconnect rotates the generation → cached gen1 token is unreachable.
    await _seed_connection(db_context, encryption)
    second = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    assert second == "token-gen2"
    assert transport.calls == 2


# --------------------------------------------------------------------------- #
# evict_cached_token
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_evict_cached_token_forces_refresh(db_context: Database) -> None:
    encryption = CredentialEncryption(generate_key())
    await _seed_connection(db_context, encryption)
    transport = _ScriptedTransport([_ok_token("token-1"), _ok_token("token-2")])
    resolver = _resolver(encryption, transport=transport)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    first = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)
    assert first == "token-1"

    resolver.evict_cached_token(USER_ID)
    second = await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    assert second == "token-2"
    assert transport.calls == 2


# --------------------------------------------------------------------------- #
# Transient refresh failure leaves the row untouched
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transient_refresh_failure_does_not_mutate_row(
    db_context: Database,
) -> None:
    encryption = CredentialEncryption(generate_key())
    generation = await _seed_connection(db_context, encryption)
    transport = _ScriptedTransport([httpx.Response(500, json={"error": "backend"})])
    notifier = _RecordingNotifier()
    resolver = _resolver(encryption, transport=transport, notifier=notifier)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(OAuthRefreshFailedError):
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    connection = await db_context.oauth_connections.get_connection(USER_ID, PROVIDER)
    assert connection is not None
    assert connection.status == "active"
    assert connection.credential_generation == generation
    assert notifier.sent == []


# --------------------------------------------------------------------------- #
# Decryption failure is a configuration error; row untouched
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decryption_failure_propagates_without_mutation(
    db_context: Database,
) -> None:
    writing_encryption = CredentialEncryption(generate_key())
    generation = await _seed_connection(db_context, writing_encryption)

    wrong_key_encryption = CredentialEncryption(generate_key())
    transport = _ScriptedTransport([_ok_token("never-used")])
    resolver = _resolver(wrong_key_encryption, transport=transport)
    exec_context = _exec_context(db_context, user_id=USER_ID)

    with pytest.raises(CredentialDecryptionError):
        await resolver.access_token_for(exec_context, GoogleScope.GMAIL_READONLY)

    connection = await db_context.oauth_connections.get_connection(USER_ID, PROVIDER)
    assert connection is not None
    assert connection.status == "active"
    assert connection.credential_generation == generation
    assert transport.calls == 0  # never reached the token endpoint


# --------------------------------------------------------------------------- #
# invalid_grant response shape assertion (module contract sanity)
# --------------------------------------------------------------------------- #


def test_invalid_grant_helper_shape() -> None:
    # Guard the scripted invalid_grant response shape the resolver keys on.
    response = _invalid_grant()
    assert json.loads(response.content)["error"] == "invalid_grant"


def test_user_operation_locks_are_stable_and_scoped() -> None:
    resolver = _resolver(
        CredentialEncryption(generate_key()), transport=_ScriptedTransport([])
    )

    drive_lock = resolver.user_operation_lock(USER_ID, "drive_write")

    assert resolver.user_operation_lock(USER_ID, "drive_write") is drive_lock
    assert resolver.user_operation_lock("user-b", "drive_write") is not drive_lock
    assert resolver.user_operation_lock(USER_ID, "other-operation") is not drive_lock
