"""Integration tests for RemoteBrowserBackend against browser-server.

The browser-server package is an explicit dev dependency. These tests run its
real FastAPI app in-process with the fake browser runtime, so no Chromium or
network service is needed while still exercising the browser-server REST API.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from browser_handoff_service.main import app as browser_server_app
from browser_handoff_service.main import registry as browser_server_registry
from browser_handoff_service.models import TERMINAL_STATES, SessionState, now_utc

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools.browser_backend import (
    BrowserBackendError,
    RemoteBrowserBackend,
    StaleRefError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SERVICE_TOKEN = "integration-test-token"
_SERVICE_URL = "http://browser-server.local"


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


def _make_backend(*, conversation_id: str = "integ-conv-1") -> RemoteBrowserBackend:
    """Return a RemoteBrowserBackend wired to the real browser-server app via ASGITransport."""
    transport = httpx.ASGITransport(app=browser_server_app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url=_SERVICE_URL,
        headers={"Authorization": f"Bearer {_SERVICE_TOKEN}"},
    )
    cfg = BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )
    return RemoteBrowserBackend(
        config=cfg,
        conversation_id=conversation_id,
        client=client,
    )


def _refs(snapshot: object) -> list[str]:
    """Every ref in a snapshot tree, in document order."""
    refs: list[str] = []

    def walk(nodes: object) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ref = node.get("ref")
            if isinstance(ref, str):
                refs.append(ref)
            walk(node.get("children"))

    if isinstance(snapshot, dict):
        walk(snapshot.get("roots"))
    return refs


def _session_id_for_conversation(conversation_id: str) -> str:
    matches = [
        session.session_id
        for session in browser_server_registry.list_sessions()
        if session.conversation_id == conversation_id
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.integration
async def test_goto_and_raw_snapshot_return_full_accessibility_tree() -> None:
    """goto() + raw_snapshot(1) returns a full accessibility tree, not a stub."""
    backend = _make_backend(conversation_id="integ-snap")
    try:
        await backend.goto("https://example.test/page")
        snap = await backend.raw_snapshot(1)
        # Full tree fields — not the old stub {title, body_text} shape
        for key in ("url", "title", "forms", "elements", "roots"):
            assert key in snap, f"missing key {key!r} in snapshot"
        assert isinstance(snap["roots"], list)
        assert snap["url"] == "https://example.test/page"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_raw_snapshot_is_stable_between_calls() -> None:
    """Consecutive raw_snapshot(1) calls return the same element count."""
    backend = _make_backend(conversation_id="integ-stable")
    try:
        await backend.goto("https://example.test/page")
        snap1 = await backend.raw_snapshot(1)
        snap2 = await backend.raw_snapshot(1)
        assert snap1.get("elements") == snap2.get("elements")
    finally:
        await backend.close()


@pytest.mark.integration
async def test_screenshot_png_returns_valid_png_bytes() -> None:
    """screenshot_png() returns real PNG bytes, not a redacted stub."""
    backend = _make_backend(conversation_id="integ-shot")
    try:
        await backend.goto("https://example.test/")
        png_bytes = await backend.screenshot_png()
        assert isinstance(png_bytes, bytes)
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "response is not a valid PNG"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_extract_html_returns_string() -> None:
    """extract_html() returns the page HTML content."""
    backend = _make_backend(conversation_id="integ-extract")
    try:
        await backend.goto("https://example.test/page")
        html = await backend.extract_html(selector=None)
        assert isinstance(html, str)
    finally:
        await backend.close()


@pytest.mark.integration
async def test_evaluate_returns_serialisable_result() -> None:
    """evaluate() runs JS in the page V8 context and returns a serialisable result."""
    backend = _make_backend(conversation_id="integ-eval")
    try:
        await backend.goto("https://example.test/page")
        result = await backend.evaluate("1 + 1")
        assert result is not None
    finally:
        await backend.close()


@pytest.mark.integration
async def test_plain_left_single_click_is_accepted() -> None:
    """mouse_click() with default button/click_count goes through to the server."""
    backend = _make_backend(conversation_id="integ-click")
    try:
        await backend.goto("https://example.test/page")
        await backend.mouse_click(10, 20)
    finally:
        await backend.close()


@pytest.mark.integration
async def test_non_left_button_click_raises_explicit_error() -> None:
    """The browser-server API has no button parameter, so degrade explicitly."""
    backend = _make_backend(conversation_id="integ-right-click")
    try:
        await backend.goto("https://example.test/page")
        with pytest.raises(BrowserBackendError, match="left mouse clicks"):
            await backend.mouse_click(10, 20, button="right")
    finally:
        await backend.close()


@pytest.mark.integration
async def test_multi_click_raises_explicit_error() -> None:
    """The browser-server API has no click_count parameter, so degrade explicitly."""
    backend = _make_backend(conversation_id="integ-double-click")
    try:
        await backend.goto("https://example.test/page")
        with pytest.raises(BrowserBackendError, match="single clicks"):
            await backend.mouse_click(10, 20, click_count=2)
    finally:
        await backend.close()


@pytest.mark.integration
async def test_keyboard_down_and_up_raise_explicit_errors() -> None:
    """The browser-server API has no key down/up commands, so degrade explicitly."""
    backend = _make_backend(conversation_id="integ-key-down")
    try:
        await backend.goto("https://example.test/page")
        with pytest.raises(BrowserBackendError, match="keyboard_down"):
            await backend.keyboard_down("Control")
        with pytest.raises(BrowserBackendError, match="keyboard_up"):
            await backend.keyboard_up("Control")
    finally:
        await backend.close()


@pytest.mark.integration
async def test_request_handoff_returns_non_empty_url() -> None:
    """request_handoff() transitions the session to handoff state and returns a URL."""
    backend = _make_backend(conversation_id="integ-handoff")
    try:
        await backend.goto("https://example.test/checkout")
        result = await backend.request_handoff(
            reason="payment",
            handoff_note="Please complete checkout",
            expected_origin=None,
        )
        assert isinstance(result, dict)
        # browser-server returns a HandoffResponse with a handoff_url field
        assert result.get("handoff_url") or result.get("session_id")
    finally:
        await backend.close()


@pytest.mark.integration
async def test_bearer_token_is_sent_in_every_request() -> None:
    """Every outgoing request carries the configured Bearer token."""
    seen: list[httpx.Request] = []

    class _LoggingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._inner = httpx.ASGITransport(app=browser_server_app)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return await self._inner.handle_async_request(request)

    client = httpx.AsyncClient(
        transport=_LoggingTransport(),
        base_url=_SERVICE_URL,
        headers={"Authorization": f"Bearer {_SERVICE_TOKEN}"},
    )
    cfg = BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )
    backend = RemoteBrowserBackend(
        config=cfg,
        conversation_id="integ-auth-check",
        client=client,
    )
    try:
        await backend.goto("https://example.test/")
        await backend.raw_snapshot(1)
    finally:
        await backend.close()

    assert len(seen) >= 2  # at least POST /v1/sessions and POST .../agent-command
    for req in seen:
        auth = req.headers.get("authorization", "")
        assert auth == f"Bearer {_SERVICE_TOKEN}", f"missing/wrong auth on {req.url}"


@pytest.mark.integration
async def test_unknown_session_recreates_session_and_retries_command() -> None:
    """A server-side session eviction is recovered by creating a new session."""
    conversation_id = "integ-unknown-session"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/before-restart")
        stale_session_id = _session_id_for_conversation(conversation_id)
        await browser_server_registry.close(stale_session_id)
        browser_server_registry.sessions.pop(stale_session_id)

        await backend.goto("https://example.test/after-restart")

        recovered_session_id = _session_id_for_conversation(conversation_id)
        assert recovered_session_id != stale_session_id
        assert backend.current_url == "https://example.test/after-restart"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_unknown_session_handoff_fails_instead_of_handing_off_fresh_session() -> (
    None
):
    """Handoff must not silently replace the live browser the human expects."""
    conversation_id = "integ-unknown-session-handoff"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/checkout")
        stale_session_id = _session_id_for_conversation(conversation_id)
        await browser_server_registry.close(stale_session_id)
        browser_server_registry.sessions.pop(stale_session_id)

        with pytest.raises(BrowserBackendError, match="live browser session"):
            await backend.request_handoff(
                reason="other",
                handoff_note="Please take over",
                expected_origin=None,
            )

        assert [
            session.session_id
            for session in browser_server_registry.list_sessions()
            if session.conversation_id == conversation_id
        ] == []
    finally:
        await backend.close()


async def _expire_session(session_id: str) -> None:
    """Drive a live session to the EXPIRED terminal state, as the reaper would."""
    browser_server_registry.sessions[session_id].idle_expires_at = (
        now_utc() - timedelta(seconds=1)
    )
    await browser_server_registry.reap_expired()


def _live_session_id_for_conversation(conversation_id: str) -> str:
    """The single non-terminal session for a conversation.

    Unlike server eviction, an expired session lingers in the registry in the
    EXPIRED terminal state, so a recovered conversation has both the dead and the
    fresh session; the recovered backend points at the live one.
    """
    matches = [
        session.session_id
        for session in browser_server_registry.list_sessions()
        if session.conversation_id == conversation_id
        and session.state not in TERMINAL_STATES
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.integration
async def test_expired_session_recreates_session_and_retries_navigation() -> None:
    """An expired (terminal) session is recovered transparently on browser_open."""
    conversation_id = "integ-expired-nav"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/before-expiry")
        stale_session_id = _live_session_id_for_conversation(conversation_id)
        await _expire_session(stale_session_id)

        await backend.goto("https://example.test/after-expiry")

        recovered_session_id = _live_session_id_for_conversation(conversation_id)
        assert recovered_session_id != stale_session_id
        assert backend.current_url == "https://example.test/after-expiry"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_expired_session_command_prompts_browser_open() -> None:
    """A non-navigation command on an expired session fails clearly and clears it.

    The agent should be told to start over with browser_open rather than be
    wedged on a dead session (the bug that previously needed a pod restart).
    """
    conversation_id = "integ-expired-cmd"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/checkout")
        stale_session_id = _live_session_id_for_conversation(conversation_id)
        await _expire_session(stale_session_id)

        with pytest.raises(BrowserBackendError, match="browser_open"):
            await backend.raw_snapshot(1)

        # The dead session is cleared, so a fresh browser_open succeeds without
        # any manual lease release or pod restart.
        await backend.goto("https://example.test/restarted")
        recovered_session_id = _live_session_id_for_conversation(conversation_id)
        assert recovered_session_id != stale_session_id
    finally:
        await backend.close()


def _agent_active_session_id_for_conversation(conversation_id: str) -> str:
    """The single agent-owned (agent_active) session for a conversation.

    After a handoff the handed-off session lingers (non-terminal), so the fresh
    session the backend recovered onto is identified by being agent_active.
    """
    matches = [
        session.session_id
        for session in browser_server_registry.list_sessions()
        if session.conversation_id == conversation_id
        and session.state == SessionState.AGENT_ACTIVE
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.integration
async def test_handed_off_session_recreated_on_browser_open() -> None:
    """browser_open starts a fresh session after the agent handed the browser off.

    A handoff moves the session out of agent control (the agent loses the lease), so
    every later command gets a 403. Without recovery the conversation stays pinned to
    that un-drivable session forever; browser_open must transparently start anew.
    """
    conversation_id = "integ-handoff-reopen"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/checkout")
        stale_session_id = _session_id_for_conversation(conversation_id)
        await backend.request_handoff(
            reason="other",
            handoff_note="Please take over",
            expected_origin=None,
        )

        await backend.goto("https://example.test/after-handoff")

        recovered_session_id = _agent_active_session_id_for_conversation(
            conversation_id
        )
        assert recovered_session_id != stale_session_id
        assert backend.current_url == "https://example.test/after-handoff"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_handed_off_session_command_prompts_browser_open() -> None:
    """A non-navigation command on a handed-off session fails clearly and clears it.

    The agent that lost the lease should be told to start over (or claim the
    handback) rather than be wedged on a session it can no longer drive.
    """
    conversation_id = "integ-handoff-cmd"
    backend = _make_backend(conversation_id=conversation_id)
    try:
        await backend.goto("https://example.test/checkout")
        stale_session_id = _session_id_for_conversation(conversation_id)
        await backend.request_handoff(
            reason="other",
            handoff_note="Please take over",
            expected_origin=None,
        )

        with pytest.raises(BrowserBackendError, match="browser_open"):
            await backend.raw_snapshot(1)

        # The handed-off session is cleared, so a fresh browser_open succeeds.
        await backend.goto("https://example.test/restarted")
        recovered_session_id = _agent_active_session_id_for_conversation(
            conversation_id
        )
        assert recovered_session_id != stale_session_id
    finally:
        await backend.close()


@pytest.mark.integration
async def test_claim_handback_resumes_session() -> None:
    """claim_handback() re-attaches the backend to a session the human handed back.

    Simulates the full HITL loop:
      1. Agent requests handoff (allow_resume=True)
      2. Human claims the session via the handoff URL token
      3. Human hands back to the agent via /handover
      4. Agent calls claim_handback() — backend _session_id is updated
      5. Subsequent snapshot works on the resumed session
    """
    backend = _make_backend(conversation_id="integ-claim")
    transport = httpx.ASGITransport(app=browser_server_app)
    human_client = httpx.AsyncClient(
        transport=transport,
        base_url=_SERVICE_URL,
    )
    try:
        # 1. Agent navigates and requests handoff with allow_resume=True
        await backend.goto("https://example.test/account")
        handoff_result = await backend.request_handoff(
            reason="other",
            handoff_note="Please review and confirm",
            expected_origin=None,
            allow_resume=True,
        )
        session_id = str(handoff_result["session_id"])
        handoff_token = handoff_result["handoff_url"].split("token=", 1)[1]

        # 2. Human claims the session
        claimed = await human_client.post(
            f"/v1/sessions/{session_id}/claim",
            json={"token": handoff_token},
        )
        assert claimed.status_code == 200, claimed.text
        control_token = claimed.json()["control_token"]

        # 3. Human hands back to the agent
        handover = await human_client.post(
            f"/v1/sessions/{session_id}/handover",
            json={"token": control_token, "handoff_note": "Payment done"},
        )
        assert handover.status_code == 200, handover.text
        handover_token = handover.json()["handover_token"]

        # 4. Agent claims the handback
        claim_result = await backend.claim_handback(session_id, handover_token)
        assert claim_result.get("state") in {"agent_active", "agent_resumable"}

        # Idempotency of a retried claim_handback is the browser-server's
        # responsibility and is covered by its own agent_claim tests; it is not
        # re-asserted here so this suite does not couple to a browser-server
        # version newer than the one pinned in uv.lock.

        # 5. Snapshot works after reclaim
        snap = await backend.raw_snapshot(1)
        assert "roots" in snap
    finally:
        await backend.close()
        await human_client.aclose()


@pytest.mark.integration
async def test_missing_token_env_raises_browser_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrowserBackendError is raised immediately when the token env var is not set.

    RemoteBrowserBackend._headers() reads the token from the env var named by
    auth.token_env.  If that variable is absent it raises BrowserBackendError
    before making any HTTP request.
    """
    monkeypatch.delenv("BROWSER_HANDOFF_SERVICE_TOKEN", raising=False)

    transport = httpx.ASGITransport(app=browser_server_app)
    client = httpx.AsyncClient(transport=transport, base_url=_SERVICE_URL)
    cfg = BrowserHandoffConfig(
        enabled=True,
        service_url=_SERVICE_URL,
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )
    backend = RemoteBrowserBackend(
        config=cfg,
        conversation_id="integ-no-token",
        client=client,
    )
    with pytest.raises(BrowserBackendError, match="token env"):
        await backend.goto("https://example.test/")


@pytest.mark.integration
async def test_snapshot_reports_the_advanced_ref_counter() -> None:
    """The caller's counter goes in and the advanced one comes back out."""
    backend = _make_backend(conversation_id="integ-next-ref")
    try:
        await backend.goto("https://example.test/page")
        snap = await backend.raw_snapshot(5_000)
        assert snap["next_ref"] > 5_000
        # A second snapshot handed the advanced counter reuses the stamped
        # refs, so a ref names one node across snapshots.
        again = await backend.raw_snapshot(snap["next_ref"])
        assert _refs(again) == _refs(snap)
    finally:
        await backend.close()


@pytest.mark.integration
async def test_click_on_an_issued_ref_is_accepted() -> None:
    backend = _make_backend(conversation_id="integ-click-ref")
    try:
        await backend.goto("https://example.test/page")
        snap = await backend.raw_snapshot(1)
        ref = _refs(snap)[0]
        await backend.click(ref)
    finally:
        await backend.close()


@pytest.mark.integration
async def test_click_on_a_ref_the_page_never_issued_raises_stale_ref() -> None:
    """Staleness is the page's call, and it comes back as a typed error."""
    backend = _make_backend(conversation_id="integ-stale-ref")
    try:
        await backend.goto("https://example.test/page")
        await backend.raw_snapshot(1)
        with pytest.raises(StaleRefError) as exc:
            await backend.click("e999999")
        assert exc.value.ref == "e999999"
    finally:
        await backend.close()
