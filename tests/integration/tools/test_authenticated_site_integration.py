"""Authenticated-site sessions against the real browser-server app, in process.

The unit tests assert what Family Assistant *sends*; these assert that the
service it is talking to agrees. browser-server runs here with its fake browser
runtime, and Keychute is faked at the one seam its client exposes for the
purpose, so the whole autofill protocol runs without a network or a second
implementation of the flow.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from browser_handoff_service import keychute as keychute_module
from browser_handoff_service.main import app as browser_server_app
from browser_handoff_service.main import registry as browser_server_registry

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools import browser_backend as backend_module
from family_assistant.tools.browser_autofill import (
    browser_autofill_tool,
    browser_report_login_outcome_tool,
)
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionSpec,
    AuthenticatedSessionUnavailableError,
    BrowserBackendError,
    RemoteBrowserBackend,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from browser_handoff_service.runtime import FakeBrowserWorker

    from family_assistant.tools.types import ToolExecutionContext

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
    *,
    alias: str | None = "test-site",
    origins: frozenset[str] | None = None,
    ordinary: bool = False,
    autofill_enabled: bool = False,
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
        autofill_enabled=autofill_enabled,
        authenticated=None
        if ordinary
        else AuthenticatedSessionSpec(
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
    grant_id = "11111111-1111-4111-8111-111111111111"
    payload = secret or {"username": "someone@example.test", "password": "hunter2"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/access-requests":
            return httpx.Response(
                200,
                json={
                    "request_id": "22222222-2222-4222-8222-222222222222",
                    "state": "approved" if approved else "pending",
                    "grant_id": grant_id if approved else None,
                    "server_time": "2026-09-21T00:00:00Z",
                },
            )
        if path.endswith("/wait"):
            return httpx.Response(
                200,
                json={
                    "request_id": "22222222-2222-4222-8222-222222222222",
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
        assert body["idempotency_key"].startswith(f"{session_id}:")
        assert body["mechanism"] == "autofill"
        assert body["constraints"]["origins"] == [{"host": "example.test", "port": 443}]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_resume", [False, True])
async def test_authenticated_otp_handoff_returns_through_server_side_claim(
    allow_resume: bool,
) -> None:
    backend = _backend()
    session_id = await backend.start_authenticated_session()
    await backend.goto(f"{ORIGIN}/login")
    handoff = await backend.request_handoff(
        reason="otp",
        handoff_note="Enter the code",
        expected_origin=ORIGIN,
        allow_resume=allow_resume,
    )
    token = parse_qs(urlsplit(str(handoff["handoff_url"])).query)["token"][0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_server_app), base_url=_SERVICE_URL
    ) as client:
        claim = await client.post(
            f"/v1/sessions/{session_id}/claim", json={"token": token}
        )
        assert claim.status_code == 200
        handback = await client.post(
            f"/v1/sessions/{session_id}/handover",
            json={"token": claim.json()["control_token"]},
        )
        assert handback.status_code == 200
        assert handback.json()["handover_token"] is None
    await backend.claim_handback_server_side(session_id)
    state = await backend.session_state()
    assert state["state"] == "agent_active"
    assert state["confine_origins"] == [ORIGIN]
    await backend.close()


def _credential_tool_context(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
) -> tuple[RemoteBrowserBackend, ToolExecutionContext]:
    backend = _backend(ordinary=True, autofill_enabled=True)
    monkeypatch.setitem(
        backend_module._remote_backends, ("on-demand-conv", True), backend
    )
    context = cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id="on-demand-conv",
            subconversation_id=None,
            processing_profile_id=profile_id,
            processing_service=SimpleNamespace(
                app_config=SimpleNamespace(
                    authenticated_sites={},
                    browser_handoff_config=BrowserHandoffConfig(
                        enabled=True,
                        service_url=_SERVICE_URL,
                        handoff_capable_profiles=[profile_id],
                    ),
                )
            ),
            user_name="andrew",
            timezone=None,
            tool_call_batch=None,
            tool_call_id=None,
        ),
    )
    return backend, context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_id", ["credential_browser_profile", "credential_browser_visual_profile"]
)
async def test_credential_browser_requests_named_secret_and_resumes_approval(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
) -> None:
    pending_requests = _fake_keychute(monkeypatch, approved=False)
    backend, context = _credential_tool_context(monkeypatch, profile_id)
    try:
        await backend.goto(f"{ORIGIN}/login")
        assert backend.session_id is not None
        record = _session_record(backend.session_id)
        assert record.autofill_enabled and not record.authenticated_site
        worker = cast(
            "FakeBrowserWorker", browser_server_registry.workers[record.worker_id]
        )
        worker.autofill_fields = [
            {"ref": "e12", "input_type": "password", "name": "Password"}
        ]
        pending = await browser_autofill_tool(
            context, secret_name="my-password", kind="password"
        )
        pending_data = pending.get_data()
        assert isinstance(pending_data, dict)
        assert pending_data["status"] == "approval_pending", pending.get_text()
        assert not worker.filled
        approved_requests = _fake_keychute(monkeypatch, approved=True)
        filled = await browser_autofill_tool(
            context, secret_name="my-password", kind="password"
        )
        filled_data = filled.get_data()
        assert isinstance(filled_data, dict)
        assert filled_data["status"] == "filled"
        assert worker.filled == [{"ref": "e12", "kind": "password", "value": "hunter2"}]
        assert "hunter2" not in filled.get_text()
        initial = json.loads(pending_requests[0].content)
        resumed = json.loads(approved_requests[0].content)
        assert initial["idempotency_key"] == resumed["idempotency_key"]
        assert resumed["secret_name"] == "my-password"
        assert resumed["constraints"]["origins"] == [
            {"host": "example.test", "port": 443}
        ]
        assert not backend.autofill_step_keys
        await backend.goto("https://another.example.test/")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_rejected_password_can_be_corrected_in_the_same_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_keychute(monkeypatch)
    backend, context = _credential_tool_context(
        monkeypatch, "credential_browser_profile"
    )
    try:
        await backend.goto(f"{ORIGIN}/login")
        failed_session_id = backend.session_id
        assert failed_session_id is not None
        reported = await browser_report_login_outcome_tool(
            context, outcome="bad_password"
        )
        assert "discarded" in reported.get_text()
        assert backend.session_id is None
        assert _session_record(failed_session_id).state == "cancelled"
        await backend.goto(f"{ORIGIN}/login")
        assert (
            backend.session_id is not None and backend.session_id != failed_session_id
        )
        record = _session_record(backend.session_id)
        worker = cast(
            "FakeBrowserWorker", browser_server_registry.workers[record.worker_id]
        )
        worker.autofill_fields = [
            {"ref": "e12", "input_type": "password", "name": "Password"}
        ]
        filled = await browser_autofill_tool(
            context, secret_name="corrected-password", kind="password"
        )
        data = filled.get_data()
        assert isinstance(data, dict)
        assert data["status"] == "filled", filled.get_text()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_profile_switch_isolates_browsers_and_preserves_each_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal tab can execute JS without disabling the separate login browser."""
    monkeypatch.setattr(backend_module, "_remote_backends", {})
    config = BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
        handoff_capable_profiles=[
            "browser_profile",
            "browser_visual_profile",
            "credential_browser_profile",
            "credential_browser_visual_profile",
        ],
    )
    context = cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id="switch-conv",
            subconversation_id=None,
            processing_profile_id="browser_profile",
            timezone=None,
            processing_service=SimpleNamespace(
                app_config=SimpleNamespace(
                    authenticated_sites={},
                    browser_handoff_config=config,
                )
            ),
            user_name="andrew",
            tool_call_batch=None,
            tool_call_id=None,
        ),
    )
    ordinary = await backend_module.get_browser_backend(context)
    assert isinstance(ordinary, RemoteBrowserBackend)
    await ordinary._client.aclose()
    ordinary._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_server_app)
    )
    context.processing_profile_id = "credential_browser_profile"
    protected = await backend_module.get_browser_backend(context)
    assert isinstance(protected, RemoteBrowserBackend)
    await protected._client.aclose()
    protected._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=browser_server_app)
    )
    try:
        await ordinary.goto(f"{ORIGIN}/public")
        assert await ordinary.evaluate("document.title")
        assert "<h1>" in await ordinary.extract_html(selector=None)
        assert protected.session_id is None
        context.processing_profile_id = "browser_visual_profile"
        assert await backend_module.get_browser_backend(context) is ordinary
        with pytest.raises(BrowserBackendError, match="credential_browser_profile"):
            await browser_autofill_tool(context, secret_name="my-password")

        context.processing_profile_id = "credential_browser_visual_profile"
        assert await backend_module.get_browser_backend(context) is protected
        await protected.goto(f"{ORIGIN}/login")
        assert ordinary.session_id != protected.session_id
        assert ordinary.session_id is not None and protected.session_id is not None
        normal_record = _session_record(ordinary.session_id)
        protected_record = _session_record(protected.session_id)
        assert normal_record.worker_id != protected_record.worker_id
        assert not normal_record.autofill_enabled
        assert protected_record.autofill_enabled
        assert not protected_record.authenticated_site
        assert protected_record.jar_id is None
        with pytest.raises(BrowserBackendError, match="denied"):
            await protected.evaluate("document.title")
        with pytest.raises(BrowserBackendError, match="denied"):
            await protected.extract_html(selector=None)
        worker = cast(
            "FakeBrowserWorker",
            browser_server_registry.workers[protected_record.worker_id],
        )
        worker.autofill_fields = [
            {"ref": "e12", "input_type": "password", "name": "Password"}
        ]
        _fake_keychute(monkeypatch, approved=False)
        pending = await browser_autofill_tool(context, secret_name="my-password")
        assert (
            isinstance(pending.data, dict)
            and pending.data["status"] == "approval_pending"
        )
        context.processing_profile_id = "browser_profile"
        assert await backend_module.get_browser_backend(context) is ordinary
        assert await ordinary.evaluate("document.title")
        context.processing_profile_id = "credential_browser_profile"
        assert await backend_module.get_browser_backend(context) is protected
        _fake_keychute(monkeypatch, approved=True)
        filled = await browser_autofill_tool(context, secret_name="my-password")
        assert isinstance(filled.data, dict) and filled.data["status"] == "filled"
    finally:
        await backend_module.close_browser_backend(context)
    assert not backend_module._remote_backends
