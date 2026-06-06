"""Unit tests for APNsService using an injected httpx MockTransport."""

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.apns import (
    APNS_HOST_PRODUCTION,
    APNS_HOST_SANDBOX,
    ApnsAuthKeyError,
    APNsService,
    load_apns_auth_key,
)
from family_assistant.services.notifier import NotificationMetadata
from family_assistant.storage.context import DatabaseContext


def _make_p8_key() -> str:
    """Generate a P-256 private key in PEM form, usable as a fake .p8 auth key."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


AUTH_KEY = _make_p8_key()


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) -> AsyncGenerator[DatabaseContext]:
    """Provides an entered DatabaseContext for APNs tests."""
    async with DatabaseContext(engine=db_engine) as db_ctx:
        yield db_ctx


def _service(handler: object, **kwargs: object) -> APNsService:
    """Build an APNsService backed by a MockTransport handler."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return APNsService(
        team_id="TEAM123",
        key_id="KEY456",
        auth_key=AUTH_KEY,
        bundle_id="com.example.app",
        client=client,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_disabled_when_unconfigured() -> None:
    """The service reports disabled when required settings are missing."""
    service = APNsService(team_id=None, key_id=None, auth_key=None, bundle_id=None)
    assert service.enabled is False


@pytest.mark.asyncio
async def test_sends_alert_with_expected_headers_and_payload(
    db_context: DatabaseContext,
) -> None:
    """A configured token receives an alert push with the correct APNs headers."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1",
        device_token="tok-prod",
        environment="production",
    )

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    service = _service(handler)
    await service.send_notification("user-1", "Hi", "There", db_context)

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == f"{APNS_HOST_PRODUCTION}/3/device/tok-prod"
    assert request.headers["apns-topic"] == "com.example.app"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["authorization"].startswith("bearer ")
    body = json.loads(request.content)
    assert body["aps"]["alert"] == {"title": "Hi", "body": "There"}

    # Token is still present (not deleted) after a successful send.
    assert len(await db_context.ios_push_tokens.get_by_user("user-1")) == 1


@pytest.mark.asyncio
async def test_metadata_sets_category_and_custom_fields(
    db_context: DatabaseContext,
) -> None:
    """Notification metadata becomes aps.category plus top-level custom userInfo keys."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    metadata = NotificationMetadata(
        category="FAMILY_ASSISTANT_CONFIRMATION",
        request_id="confirm_abc",
        conversation_id="conv-1",
    )
    await _service(handler).send_notification(
        "user-1", "Hi", "There", db_context, metadata=metadata
    )

    body = json.loads(seen[0].content)
    assert body["aps"]["category"] == "FAMILY_ASSISTANT_CONFIRMATION"
    # Custom fields are top-level userInfo keys, not nested under aps.
    assert body["request_id"] == "confirm_abc"
    assert body["conversation_id"] == "conv-1"
    assert "category" not in body


@pytest.mark.asyncio
async def test_signs_provider_token_with_team_and_key(
    db_context: DatabaseContext,
) -> None:
    """The Authorization bearer is an ES256 JWT carrying the team id and key id."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200)

    await _service(handler).send_notification("user-1", "t", "b", db_context)

    token = captured["auth"].removeprefix("bearer ")
    header = jwt.get_unverified_header(token)
    assert header["kid"] == "KEY456"
    assert header["alg"] == "ES256"
    # Verify the signature with the public key derived from AUTH_KEY.
    private_key = serialization.load_pem_private_key(AUTH_KEY.encode(), password=None)
    assert isinstance(private_key, ec.EllipticCurvePrivateKey)
    decoded = jwt.decode(token, private_key.public_key(), algorithms=["ES256"])
    assert decoded["iss"] == "TEAM123"


@pytest.mark.asyncio
async def test_unregistered_token_is_deleted(db_context: DatabaseContext) -> None:
    """A 410 Unregistered response prunes the device token."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="dead", environment="production"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered"})

    await _service(handler).send_notification("user-1", "t", "b", db_context)

    assert await db_context.ios_push_tokens.get_by_user("user-1") == []


@pytest.mark.asyncio
async def test_bad_device_token_retries_other_environment(
    db_context: DatabaseContext,
) -> None:
    """A BadDeviceToken on production succeeds on sandbox and updates the stored environment."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == httpx.URL(APNS_HOST_SANDBOX).host:
            return httpx.Response(200)
        return httpx.Response(400, json={"reason": "BadDeviceToken"})

    await _service(handler).send_notification("user-1", "t", "b", db_context)

    tokens = await db_context.ios_push_tokens.get_by_user("user-1")
    assert len(tokens) == 1
    assert tokens[0].environment == "sandbox"


@pytest.mark.asyncio
async def test_bad_device_token_in_both_environments_deletes(
    db_context: DatabaseContext,
) -> None:
    """A BadDeviceToken in both environments prunes the token."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"reason": "BadDeviceToken"})

    await _service(handler).send_notification("user-1", "t", "b", db_context)

    assert await db_context.ios_push_tokens.get_by_user("user-1") == []


@pytest.mark.asyncio
async def test_bad_device_token_keeps_token_on_transient_retry_failure(
    db_context: DatabaseContext,
) -> None:
    """A transient failure on the environment retry must not delete a valid token."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Production rejects as BadDeviceToken; the sandbox retry hits a transient 500.
        if request.url.host == httpx.URL(APNS_HOST_SANDBOX).host:
            return httpx.Response(500, json={"reason": "InternalServerError"})
        return httpx.Response(400, json={"reason": "BadDeviceToken"})

    await _service(handler).send_notification("user-1", "t", "b", db_context)

    # Token is preserved (environment unchanged) for the next send attempt.
    tokens = await db_context.ios_push_tokens.get_by_user("user-1")
    assert len(tokens) == 1
    assert tokens[0].environment == "production"


@pytest.mark.asyncio
async def test_expired_provider_token_refreshes_and_retries(
    db_context: DatabaseContext,
) -> None:
    """A 403 ExpiredProviderToken triggers a JWT refresh and a successful retry."""
    await db_context.ios_push_tokens.upsert(
        user_identifier="user-1", device_token="tok", environment="production"
    )

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["authorization"])
        if len(attempts) == 1:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
        return httpx.Response(200)

    # Distinct issue times so the refreshed token differs from the first.
    times = iter([1000.0, 1000.0, 5000.0, 5000.0, 5000.0])
    service = _service(handler, time_fn=lambda: next(times))
    await service.send_notification("user-1", "t", "b", db_context)

    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    # Token retained after a successful retry.
    assert len(await db_context.ios_push_tokens.get_by_user("user-1")) == 1


@pytest.mark.asyncio
async def test_no_tokens_is_noop(db_context: DatabaseContext) -> None:
    """Sending to a user with no tokens does not raise or call the transport."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    await _service(handler).send_notification("nobody", "t", "b", db_context)
    assert called is False


def test_load_apns_auth_key_prefers_inline_value(tmp_path: Path) -> None:
    """An inline auth key is used in preference to a path."""
    key_file = tmp_path / "key.p8"
    key_file.write_text("from-file")
    assert (
        load_apns_auth_key(auth_key="inline", auth_key_path=str(key_file)) == "inline"
    )


def test_load_apns_auth_key_reads_path(tmp_path: Path) -> None:
    """A configured path is read when no inline key is provided."""
    key_file = tmp_path / "key.p8"
    key_file.write_text("from-file")
    assert load_apns_auth_key(auth_key=None, auth_key_path=str(key_file)) == "from-file"


def test_load_apns_auth_key_none_when_unconfigured() -> None:
    """No key and no path yields None (APNs simply stays disabled)."""
    assert load_apns_auth_key(auth_key=None, auth_key_path=None) is None


def test_load_apns_auth_key_raises_on_unreadable_path(tmp_path: Path) -> None:
    """An explicitly configured but unreadable path is a fatal configuration error."""
    missing = tmp_path / "does-not-exist.p8"
    with pytest.raises(ApnsAuthKeyError):
        load_apns_auth_key(auth_key=None, auth_key_path=str(missing))
