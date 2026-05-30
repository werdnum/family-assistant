"""Integration test: RemoteBrowserBackend against a real browser-server (fake runtime).

browser-server runs in-process via httpx.ASGITransport with BROWSER_RUNTIME=fake,
so no Chromium or network is needed.  The test exercises the full HTTP round-trip
from family-assistant's RemoteBrowserBackend through the browser-server REST API.

The sibling repo is added to sys.path by tests/integration/tools/conftest.py before
any imports in this file are resolved.
"""

from __future__ import annotations

import httpx
import pytest

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools.browser_backend import (
    BrowserBackendError,
    RemoteBrowserBackend,
)

# conftest.py inserts the sibling browser-server repo into sys.path.
# pytest.importorskip skips this entire module if the import cannot be resolved
# (e.g. in a checkout without the sibling repo).
_bhs = pytest.importorskip(
    "browser_handoff_service.main",
    reason="browser-server sibling repo not available",
)
_browser_server_app = _bhs.app

_SERVICE_TOKEN = "integration-test-token"
_SERVICE_URL = "http://browser-server.local"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_RUNTIME", "fake")
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", _SERVICE_TOKEN)


def _make_backend(*, conversation_id: str = "integ-conv-1") -> RemoteBrowserBackend:
    """Return a RemoteBrowserBackend wired to the real browser-server app via ASGITransport."""
    transport = httpx.ASGITransport(app=_browser_server_app)
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


@pytest.mark.integration
async def test_goto_and_raw_snapshot_return_full_accessibility_tree() -> None:
    """goto() + raw_snapshot() returns a full accessibility tree, not a stub."""
    backend = _make_backend(conversation_id="integ-snap")
    try:
        await backend.goto("https://example.test/page")
        snap = await backend.raw_snapshot()
        # Full tree fields — not the old stub {title, body_text} shape
        for key in ("url", "title", "forms", "elements", "roots"):
            assert key in snap, f"missing key {key!r} in snapshot"
        assert isinstance(snap["roots"], list)
        assert snap["url"] == "https://example.test/page"
    finally:
        await backend.close()


@pytest.mark.integration
async def test_raw_snapshot_is_stable_between_calls() -> None:
    """Consecutive raw_snapshot() calls return the same element count."""
    backend = _make_backend(conversation_id="integ-stable")
    try:
        await backend.goto("https://example.test/page")
        snap1 = await backend.raw_snapshot()
        snap2 = await backend.raw_snapshot()
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
            self._inner = httpx.ASGITransport(app=_browser_server_app)

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
        await backend.raw_snapshot()
    finally:
        await backend.close()

    assert len(seen) >= 2  # at least POST /v1/sessions and POST .../agent-command
    for req in seen:
        auth = req.headers.get("authorization", "")
        assert auth == f"Bearer {_SERVICE_TOKEN}", f"missing/wrong auth on {req.url}"


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
    transport = httpx.ASGITransport(app=_browser_server_app)
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

        # 5. Snapshot works after reclaim
        snap = await backend.raw_snapshot()
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

    transport = httpx.ASGITransport(app=_browser_server_app)
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

