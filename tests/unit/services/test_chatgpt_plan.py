"""Tests for the ChatGPT plan sign-in and its token lifecycle.

Real repository on the ``db_engine`` fixture; OpenAI's auth server is an
``httpx.MockTransport`` that scripts responses and records requests.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from family_assistant.services.chatgpt_plan import (
    DYNAMIC_CLIENT_ID,
    PLAN_SCOPE,
    REDIRECT_URI,
    SCOPES,
    ChatGPTPlanAuth,
    ChatGPTPlanEndpoints,
    ChatGPTPlanNotSignedInError,
    ChatGPTPlanReauthRequiredError,
    ChatGPTPlanRefreshFailedError,
    ChatGPTPlanSignInError,
)
from family_assistant.services.credential_encryption import (
    CredentialEncryption,
    generate_key,
)
from family_assistant.storage.database import Database
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

ENDPOINTS = ChatGPTPlanEndpoints(
    authorize="https://auth.test/authorize",
    token="https://auth.test/token",
    discovery="https://auth.test/.well-known/openid-configuration",
)
ISSUED_CLIENT_ID = "oaiapp_issued"
SUBJECT = "user-subject"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

type Handler = Callable[[httpx.Request], httpx.Response]


def _id_token(
    *,
    nonce: str,
    sub: str = SUBJECT,
    aud: str = ISSUED_CLIENT_ID,
    iss: str = "https://auth.openai.com",
    exp: datetime = NOW + timedelta(hours=1),
) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    claims = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "exp": int(exp.timestamp()),
        "nonce": nonce,
        "email": "parent@example.com",
    }
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _token_response(
    *,
    access: str = "access-1",
    refresh: str = "refresh-1",
    id_token: str | None = None,
    scope: str = SCOPES,
) -> httpx.Response:
    body: dict[str, object] = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }
    if id_token is not None:
        body["id_token"] = id_token
    return httpx.Response(200, json=body)


class _AuthServer:
    """Answers each request with the next scripted handler, recording requests."""

    def __init__(self) -> None:
        self.handlers: list[Handler] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handlers.pop(0)(request)

    def form(self, index: int) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in parse_qs(self.requests[index].content.decode()).items()
        }


@pytest.fixture
def server() -> _AuthServer:
    return _AuthServer()


@pytest.fixture
def clock() -> MockClock:
    return MockClock(NOW)


@pytest.fixture
def auth(
    db_engine: AsyncEngine, server: _AuthServer, clock: MockClock
) -> ChatGPTPlanAuth:
    return ChatGPTPlanAuth(
        Database(engine=db_engine),
        CredentialEncryption(generate_key()),
        httpx.AsyncClient(transport=httpx.MockTransport(server)),
        agent_name="Family Assistant",
        endpoints=ENDPOINTS,
        clock=clock,
    )


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def _callback(params: dict[str, str]) -> str:
    return f"{REDIRECT_URI}?{urlencode(params)}"


async def _sign_in(
    auth: ChatGPTPlanAuth,
    server: _AuthServer,
    *,
    use_saved_account: bool = False,
    sub: str = SUBJECT,
    access: str = "access-1",
    refresh: str = "refresh-1",
) -> dict[str, str]:
    authorize = _query(
        await auth.start_sign_in("alice", use_saved_account=use_saved_account)
    )
    client_id = (
        ISSUED_CLIENT_ID if authorize["client_id"] == DYNAMIC_CLIENT_ID else None
    )
    server.handlers.append(
        lambda _request: _token_response(
            access=access,
            refresh=refresh,
            id_token=_id_token(nonce=authorize["nonce"], sub=sub),
        )
    )
    callback = {"code": "auth-code", "state": authorize["state"]}
    if client_id:
        callback["client_id"] = client_id
    await auth.complete_sign_in("alice", _callback(callback))
    return authorize


async def test_new_sign_in_registers_with_agent_name_and_loopback_redirect(
    auth: ChatGPTPlanAuth,
) -> None:
    authorize = _query(await auth.start_sign_in("alice", use_saved_account=True))

    assert authorize["client_id"] == DYNAMIC_CLIENT_ID
    assert authorize["agent_name_hint"] == "Family Assistant"
    assert authorize["redirect_uri"] == REDIRECT_URI
    assert authorize["code_challenge_method"] == "S256"
    assert authorize["ext_agent_host_id"].startswith("urn:uuid:")


async def test_pasted_callback_stores_an_active_sign_in(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)

    assert await auth.access_token() == "access-1"


async def test_code_exchange_uses_issued_client_and_verifier(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)

    form = server.form(0)
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == ISSUED_CLIENT_ID
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["resource"] == "https://api.openai.com/v1"
    assert form["code_verifier"]


async def test_callback_for_another_attempt_is_refused(
    auth: ChatGPTPlanAuth,
) -> None:
    await auth.start_sign_in("alice", use_saved_account=False)

    with pytest.raises(ChatGPTPlanSignInError, match="current sign-in attempt"):
        await auth.complete_sign_in(
            "alice",
            _callback({
                "code": "c",
                "state": "someone-elses",
                "client_id": ISSUED_CLIENT_ID,
            }),
        )


async def test_sign_in_without_plan_scope_is_refused(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    authorize = _query(await auth.start_sign_in("alice", use_saved_account=False))
    server.handlers.append(
        lambda _request: _token_response(
            id_token=_id_token(nonce=authorize["nonce"]),
            scope=SCOPES.replace(PLAN_SCOPE, ""),
        )
    )

    with pytest.raises(ChatGPTPlanSignInError, match="plan usage wasn't granted"):
        await auth.complete_sign_in(
            "alice",
            _callback({
                "code": "c",
                "state": authorize["state"],
                "client_id": ISSUED_CLIENT_ID,
            }),
        )


async def test_id_token_with_wrong_nonce_is_refused(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    authorize = _query(await auth.start_sign_in("alice", use_saved_account=False))
    server.handlers.append(
        lambda _request: _token_response(id_token=_id_token(nonce="replayed"))
    )

    with pytest.raises(ChatGPTPlanSignInError, match="doesn't match"):
        await auth.complete_sign_in(
            "alice",
            _callback({
                "code": "c",
                "state": authorize["state"],
                "client_id": ISSUED_CLIENT_ID,
            }),
        )


async def test_new_registration_without_issued_client_is_refused(
    auth: ChatGPTPlanAuth,
) -> None:
    authorize = _query(await auth.start_sign_in("alice", use_saved_account=False))

    with pytest.raises(ChatGPTPlanSignInError, match="registering"):
        await auth.complete_sign_in(
            "alice", _callback({"code": "c", "state": authorize["state"]})
        )


async def test_no_sign_in_raises_not_signed_in(auth: ChatGPTPlanAuth) -> None:
    with pytest.raises(ChatGPTPlanNotSignedInError):
        await auth.access_token()


async def test_expired_access_token_is_refreshed_and_rotation_persisted(
    auth: ChatGPTPlanAuth, server: _AuthServer, clock: MockClock
) -> None:
    await _sign_in(auth, server)
    clock.advance(timedelta(hours=2))
    server.handlers.append(
        lambda _request: _token_response(access="access-2", refresh="refresh-2")
    )
    server.handlers.append(
        lambda _request: _token_response(access="access-3", refresh="refresh-3")
    )
    await auth.access_token()
    clock.advance(timedelta(hours=2))

    assert await auth.access_token() == "access-3"
    assert server.form(2)["refresh_token"] == "refresh-2"


async def test_refresh_request_names_issued_client_and_resource(
    auth: ChatGPTPlanAuth, server: _AuthServer, clock: MockClock
) -> None:
    await _sign_in(auth, server)
    clock.advance(timedelta(hours=2))
    server.handlers.append(lambda _request: _token_response(access="access-2"))

    await auth.access_token()

    assert server.form(1) == {
        "grant_type": "refresh_token",
        "client_id": ISSUED_CLIENT_ID,
        "refresh_token": "refresh-1",
        "resource": "https://api.openai.com/v1",
    }


async def test_unusable_refresh_token_requires_signing_in_again(
    auth: ChatGPTPlanAuth, server: _AuthServer, clock: MockClock
) -> None:
    await _sign_in(auth, server)
    clock.advance(timedelta(hours=2))
    server.handlers.append(
        lambda _request: httpx.Response(400, json={"error": "refresh_token_reused"})
    )
    with pytest.raises(ChatGPTPlanReauthRequiredError):
        await auth.access_token()

    with pytest.raises(ChatGPTPlanReauthRequiredError):
        await auth.access_token()


async def test_transient_refresh_failure_keeps_the_sign_in(
    auth: ChatGPTPlanAuth, server: _AuthServer, clock: MockClock
) -> None:
    await _sign_in(auth, server)
    clock.advance(timedelta(hours=2))
    server.handlers.append(lambda _request: httpx.Response(503))
    with pytest.raises(ChatGPTPlanRefreshFailedError):
        await auth.access_token()
    server.handlers.append(lambda _request: _token_response(access="access-2"))

    assert await auth.access_token() == "access-2"


async def test_rejected_token_is_refreshed_before_its_expiry(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)
    await auth.access_token()
    auth.reject_access_token()
    server.handlers.append(lambda _request: _token_response(access="access-2"))

    assert await auth.access_token() == "access-2"


async def test_sign_out_revokes_the_refresh_token(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)
    server.handlers.append(
        lambda _request: httpx.Response(
            200, json={"revocation_endpoint": "https://auth.test/revoke"}
        )
    )
    server.handlers.append(lambda _request: httpx.Response(200))

    assert await auth.sign_out() is True
    assert server.form(2) == {
        "token": "refresh-1",
        "token_type_hint": "refresh_token",
        "client_id": ISSUED_CLIENT_ID,
    }


async def test_signed_out_deployment_has_no_token(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)
    server.handlers.append(lambda _request: httpx.Response(503))
    await auth.sign_out()

    with pytest.raises(ChatGPTPlanNotSignedInError):
        await auth.access_token()


async def test_returning_sign_in_reuses_the_registration(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    first = await _sign_in(auth, server)

    again = _query(await auth.start_sign_in("alice", use_saved_account=True))

    assert again["client_id"] == ISSUED_CLIENT_ID
    assert again["ext_agent_host_id"] == first["ext_agent_host_id"]
    assert again["login_hint"] == "parent@example.com"
    assert "id_token_hint" in again
    assert "agent_name_hint" not in again


async def test_returning_sign_in_as_another_account_is_refused(
    auth: ChatGPTPlanAuth, server: _AuthServer
) -> None:
    await _sign_in(auth, server)

    with pytest.raises(ChatGPTPlanSignInError, match="different ChatGPT account"):
        await _sign_in(auth, server, use_saved_account=True, sub="someone-else")
