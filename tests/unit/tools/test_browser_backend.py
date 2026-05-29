"""Unit tests for the browser backend selection and the remote browser-server client.

The local Playwright backend is exercised by ``tests/functional/tools/test_browser_dom.py``;
here we cover backend selection (local vs remote) and the ``RemoteBrowserBackend`` HTTP
mapping against a mocked ``browser-server``.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.tools.browser_backend import (
    HandoffUnavailableError,
    LocalPlaywrightBackend,
    RemoteBrowserBackend,
    get_browser_backend,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")


_PNG_1X1 = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode("ascii")


def _make_mock_browser_server() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """Return an httpx client wired to a fake browser-server plus a request log."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/sessions":
            return httpx.Response(
                200, json={"session_id": "bs_test", "state": "agent_active"}
            )
        if path.endswith("/agent-command"):
            body = json.loads(request.content)
            ctype = body["type"]
            result: dict[str, object]
            if ctype == "navigate":
                result = {"url": body["args"]["url"], "title": "Fixture"}
            elif ctype == "snapshot":
                result = {
                    "url": "https://example.test/page",
                    "title": "Fixture",
                    "forms": 1,
                    "elements": 1,
                    "roots": [{"ref": "e1", "role": "heading", "name": "Welcome"}],
                }
            elif ctype == "screenshot":
                result = {"mime_type": "image/png", "image_base64": _PNG_1X1}
            elif ctype == "extract":
                result = {
                    "url": "https://example.test/page",
                    "html": "<h1>Welcome</h1>",
                }
            elif ctype == "exec":
                result = {"result": "Fixture", "url": "https://example.test/page"}
            else:
                result = {
                    "accepted": True,
                    "url": "https://example.test/page",
                    "title": "Fixture",
                }
            return httpx.Response(
                200, json={"command_id": "cmd_1", "ok": True, "result": result}
            )
        if path.endswith("/handoff"):
            return httpx.Response(
                200,
                json={
                    "session_id": "bs_test",
                    "state": "handoff_requested",
                    "handoff_url": "https://browser.example/sessions/bs_test?token=abc",
                    "expires_at": "2026-05-29T00:00:00Z",
                },
            )
        if path.endswith("/close"):
            return httpx.Response(200, json={"state": "cancelled"})
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return client, seen


def _config(enabled: bool = True) -> BrowserHandoffConfig:
    return BrowserHandoffConfig(
        enabled=enabled,
        service_url="http://browser-server.test:8000",
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )


@pytest.mark.asyncio
async def test_remote_backend_snapshot_and_refs() -> None:
    client, seen = _make_mock_browser_server()
    backend = RemoteBrowserBackend(_config(), "conv_1", client=client)
    await backend.goto("https://example.test/page")
    snapshot = await backend.raw_snapshot()
    assert snapshot["title"] == "Fixture"
    assert snapshot["roots"][0]["ref"] == "e1"
    assert backend.current_url == "https://example.test/page"
    # A session is created once and reused for subsequent commands.
    assert sum(1 for r in seen if r.url.path == "/v1/sessions") == 1
    await backend.close()


@pytest.mark.asyncio
async def test_remote_backend_screenshot_decodes_png() -> None:
    client, _ = _make_mock_browser_server()
    backend = RemoteBrowserBackend(_config(), "conv_shot", client=client)
    png = await backend.screenshot_png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    await backend.close()


@pytest.mark.asyncio
async def test_remote_backend_sends_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "s3cret")
    client, seen = _make_mock_browser_server()
    backend = RemoteBrowserBackend(_config(), "conv_auth", client=client)
    await backend.goto("https://example.test/page")
    assert seen[0].headers["authorization"] == "Bearer s3cret"
    await backend.close()


@pytest.mark.asyncio
async def test_remote_backend_request_handoff_returns_url() -> None:
    client, _ = _make_mock_browser_server()
    backend = RemoteBrowserBackend(_config(), "conv_handoff", client=client)
    result = await backend.request_handoff(
        reason="payment",
        handoff_note="pay",
        expected_origin=None,
        allowed_resume="never",
    )
    assert result["handoff_url"].endswith("token=abc")
    await backend.close()


def _exec_context(*, enabled: bool, profile_id: str | None) -> ToolExecutionContext:
    app_config = SimpleNamespace(browser_handoff_config=_config(enabled=enabled))
    service = SimpleNamespace(app_config=app_config)
    ctx = SimpleNamespace(
        processing_service=service,
        processing_profile_id=profile_id,
        conversation_id="conv_select",
    )
    return cast("ToolExecutionContext", ctx)


@pytest.mark.asyncio
async def test_get_browser_backend_uses_remote_for_handoff_profile() -> None:
    ctx = _exec_context(enabled=True, profile_id="browser_profile")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, RemoteBrowserBackend)
    await backend.close()


@pytest.mark.asyncio
async def test_get_browser_backend_local_when_disabled() -> None:
    ctx = _exec_context(enabled=False, profile_id="browser_profile")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, LocalPlaywrightBackend)


@pytest.mark.asyncio
async def test_get_browser_backend_local_for_non_handoff_profile() -> None:
    ctx = _exec_context(enabled=True, profile_id="default_assistant")
    backend = await get_browser_backend(ctx)
    assert isinstance(backend, LocalPlaywrightBackend)


@pytest.mark.asyncio
async def test_local_backend_handoff_is_unavailable() -> None:
    backend = LocalPlaywrightBackend.__new__(LocalPlaywrightBackend)
    with pytest.raises(HandoffUnavailableError):
        await backend.request_handoff(
            reason="payment",
            handoff_note="",
            expected_origin=None,
            allowed_resume="never",
        )
