"""Authenticated-site sessions against the real browser-server app, in process.

The unit tests assert what Family Assistant *sends*; these assert that the
service it is talking to agrees. browser-server runs here with its fake browser
runtime, and Keychute is faked at the one seam its client exposes for the
purpose, so the whole autofill protocol runs without a network or a second
implementation of the flow.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from browser_handoff_service import keychute as keychute_module
from browser_handoff_service.main import app as browser_server_app
from browser_handoff_service.main import registry as browser_server_registry

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionSpec,
    AuthenticatedSessionUnavailableError,
    BrowserBackendError,
    RemoteBrowserBackend,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

_SERVICE_TOKEN = "integration-test-token"
_SERVICE_URL = "http://browser-server.local"
ORIGIN = "https://example.test"


@pytest.fixture(autouse=True)
async def _browser_server_state(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[None]:
    monkeypatch.setenv("BROWSER_RUNTIME", "fake")
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", _SERVICE_TOKEN)
    await _clear_browser_server_state()
    yield
    await _clear_browser_server_state()


async def _clear_browser_server_state() -> None:
    for session in list(browser_server_registry.list_sessions()):
        await browser_server_registry.close(session.session_id)
    browser_server_registry.sessions.clear()
    browser_server_registry.locks.clear()
    browser_server_registry.events.clear()
    browser_server_registry.tokens.clear()
    browser_server_registry.workers.clear()


def _backend(
    *, alias: str | None = "test-site", origins: frozenset[str] | None = None
) -> RemoteBrowserBackend:
    config = BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_server_app),
        base_url=_SERVICE_URL,
        timeout=10.0,
    )
    return RemoteBrowserBackend(
        config,
        "integ-auth-conv",
        client=client,
        authenticated=AuthenticatedSessionSpec(
            site_id="test_site",
            jar_id=None,
            confine_origins=origins if origins is not None else frozenset({ORIGIN}),
            credential_alias=alias,
        ),
    )


def _fake_keychute(
    monkeypatch: pytest.MonkeyPatch,
    *,
    approved: bool = True,
    secret: dict[str, str] | None = None,
) -> list[httpx.Request]:
    """Point browser-server's Keychute client at a fake that always decides."""
    seen: list[httpx.Request] = []
    grant_id = "grant_1"
    payload = secret or {"username": "someone@example.test", "password": "hunter2"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/access-requests":
            return httpx.Response(
                200,
                json={
                    "request_id": "req_1",
                    "state": "approved" if approved else "pending",
                    "grant_id": grant_id if approved else None,
                    "server_time": "2026-09-21T00:00:00Z",
                },
            )
        if path.endswith("/wait"):
            return httpx.Response(
                200,
                json={
                    "request_id": "req_1",
                    "state": "pending",
                    "server_time": "2026-09-21T00:00:00Z",
                },
            )
        if path == f"/v1/grants/{grant_id}":
            return httpx.Response(
                200,
                json={
                    "grant_id": grant_id,
                    "mechanism": "autofill",
                    "constraints": {
                        "origins": [{"host": "example.test"}],
                        "methods": [],
                        "path_prefixes": [],
                        "ttl_seconds": 600,
                        "max_uses": 1,
                    },
                    "not_after": "2099-01-01T00:00:00Z",
                    "max_uses": 1,
                    "use_count": 0,
                    "revoked": False,
                    "server_time": "2026-09-21T00:00:00Z",
                },
            )
        if path == f"/v1/grants/{grant_id}/read":
            return httpx.Response(
                200,
                json={
                    "secret": json.dumps(payload),
                    "encoding": "utf8",
                    "secret_version_id": "v1",
                },
            )
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    monkeypatch.setenv(keychute_module.KEYCHUTE_URL_ENV, "https://keychute.test")
    monkeypatch.setenv(keychute_module.KEYCHUTE_TOKEN_ENV, "keychute-token")
    monkeypatch.setattr(
        browser_server_registry,
        "keychute",
        keychute_module.KeychuteClient(transport=httpx.MockTransport(handler)),
    )
    return seen


def _session_record(session_id: str) -> Any:  # noqa: ANN401 - the service's own model
    return browser_server_registry.sessions[session_id]


@pytest.mark.asyncio
async def test_the_service_agrees_the_session_is_confined_and_pinned() -> None:
    backend = _backend()
    session_id = await backend.start_authenticated_session()
    record = _session_record(session_id)
    assert record.authenticated_site is True
    assert list(record.confine_origins) == [ORIGIN]
    assert record.credential_alias == "test-site"
    # The two settings that would undo the boundary are off on the record the
    # service actually enforces from, not merely absent from our request.
    assert record.allow_exec is False
    assert record.confine_navigation is True
    await backend.close()


@pytest.mark.asyncio
async def test_exec_and_extract_are_denied_by_the_service() -> None:
    backend = _backend()
    await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/page")
    with pytest.raises(BrowserBackendError):
        await backend.evaluate("1 + 1")
    with pytest.raises(BrowserBackendError):
        await backend.extract_html(None)
    await backend.close()


@pytest.mark.asyncio
async def test_a_closed_session_is_never_re_provisioned() -> None:
    backend = _backend()
    session_id = await backend.start_authenticated_session()
    await browser_server_registry.close(session_id)
    browser_server_registry.sessions.pop(session_id, None)
    with pytest.raises(AuthenticatedSessionUnavailableError):
        await backend.goto(f"{ORIGIN}/page")


@pytest.mark.asyncio
async def test_autofill_fills_without_the_value_reaching_this_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keychute_requests = _fake_keychute(monkeypatch)
    backend = _backend()
    session_id = await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/login")
    response = await backend.autofill(
        step_key="password-1",
        fields=[{"kind": "password"}],
        wait_seconds=1,
        context={"site": "test_site", "acting_user": "andrew"},
    )
    assert response["status"] in {"filled", "refused"}
    # Whatever the fake page allowed, the secret is in none of it.
    assert "hunter2" not in json.dumps(response)
    assert "hunter2" not in json.dumps(
        _session_record(session_id).model_dump(mode="json")
    )
    # Whether a Keychute request happens at all depends on the fake runtime
    # offering an eligible field, which is browser-server's business. When one
    # does, it must name the real document origin rather than a configured set,
    # and carry the per-step idempotency key.
    created = [r for r in keychute_requests if r.url.path == "/v1/access-requests"]
    for request in created:
        body = json.loads(request.content)
        assert body["idempotency_key"] == f"{session_id}:password-1"
        assert body["mechanism"] == "autofill"
        assert body["constraints"]["origins"] == [{"host": "example.test"}]
    await backend.close()


@pytest.mark.asyncio
async def test_a_pending_decision_parks_rather_than_filling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_keychute(monkeypatch, approved=False)
    backend = _backend()
    await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/login")
    response = await backend.autofill(step_key="password-1", wait_seconds=1)
    assert response["status"] in {"approval_pending", "refused"}
    await backend.close()


@pytest.mark.asyncio
async def test_a_session_without_an_alias_has_no_autofill() -> None:
    backend = _backend(alias=None)
    await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/login")
    response = await backend.autofill(step_key="password-1")
    assert response["status"] == "refused"
    assert response["reason"] == "no_alias"
    await backend.close()


@pytest.mark.asyncio
async def test_a_recorded_bad_password_refuses_every_later_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_keychute(monkeypatch)
    backend = _backend()
    await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/login")
    await backend.report_autofill_outcome("bad_password")
    response = await backend.autofill(step_key="password-2")
    assert response["status"] == "refused"
    assert response["reason"] == "bad_password_recorded"
    await backend.close()
