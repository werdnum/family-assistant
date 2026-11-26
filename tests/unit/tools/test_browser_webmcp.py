"""Unit tests for WebMCP discovery, snapshot deduplication, and invocation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx
import pytest
from browser_handoff_service.webmcp import (
    WEBMCP_CALL_JS as SERVER_WEBMCP_CALL_JS,
)
from browser_handoff_service.webmcp import (
    WEBMCP_LIST_JS as SERVER_WEBMCP_LIST_JS,
)

from family_assistant.config_models import BrowserHandoffConfig, RemoteA2AAuthConfig
from family_assistant.llm.messages import ToolMessage
from family_assistant.services.tool_call_review import (
    _reported_page,  # noqa: PLC2701  # Testing URL/title extraction from WebMCP results
    resolve_browser_action_page,
)
from family_assistant.tools import browser_dom
from family_assistant.tools.authenticated_site_surface import admissible_tools
from family_assistant.tools.browser_backend import (
    AuthenticatedSessionSpec,
    AuthenticatedSessionUnavailableError,
    BrowserBackendError,
    RemoteBrowserBackend,
)
from family_assistant.tools.browser_dom import (
    Snapshot,
    browser_snapshot_tool,
    browser_webmcp_call_tool,
)
from family_assistant.tools.browser_session import (
    BrowserSession,
    close_browser_session,
)
from family_assistant.tools.browser_webmcp import (
    WEBMCP_CALL_JS,
    WEBMCP_LIST_JS,
    WebMCPCallResult,
    WebMCPToolDescriptor,
    bound_webmcp_result,
    format_webmcp_snapshot_block,
    is_webmcp_eligible_origin,
    sanitize_webmcp_tools,
    webmcp_tools_fingerprint,
)
from family_assistant.tools.catalog import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_METADATA_BY_NAME,
    TOOLS_DEFINITION,
)
from family_assistant.tools.metadata import ToolDescriptor, ToolTag

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from family_assistant.tools.types import ToolExecutionContext


def _sample_tools() -> list[WebMCPToolDescriptor]:
    return [
        {
            "name": "search_catalog",
            "description": "Search products\nin the store",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "annotations": {"readOnlyHint": True, "untrustedContentHint": True},
            "origin": "https://shop.example.com",
        },
        {
            "name": "proceed_to_checkout",
            "description": "Open checkout page",
            "input_schema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": False, "consequentialHint": True},
            "origin": "https://shop.example.com",
        },
    ]


class _FakeBackend:
    """Minimal BrowserBackend double for browser_dom WebMCP unit tests."""

    def __init__(
        self, snapshots: list[Snapshot], call_result: WebMCPCallResult
    ) -> None:
        self._snapshots = list(snapshots)
        self._call_result = call_result
        self.current_url = snapshots[0]["url"]
        self.calls: list[tuple[str, dict[str, object], float]] = []
        self.settle_calls = 0

    async def raw_snapshot(self, next_ref: int) -> Snapshot:
        snap = (
            self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]
        )
        snap = cast("Snapshot", dict(snap))
        snap["next_ref"] = max(next_ref + 1, snap.get("next_ref", 1))
        self.current_url = snap["url"]
        return snap

    async def webmcp_call(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float = 15.0,
    ) -> WebMCPCallResult:
        self.calls.append((tool_name, arguments, timeout_seconds))
        result_url = self._call_result.get("url")
        if result_url:
            self.current_url = str(result_url)
        return self._call_result

    async def settle(self, timeout_ms: int = 5000) -> None:
        del timeout_ms
        self.settle_calls += 1


@pytest.fixture
async def _exec_context() -> AsyncIterator[ToolExecutionContext]:
    conv_id = f"webmcp-unit-{uuid4().hex}"
    ctx = cast(
        "ToolExecutionContext",
        SimpleNamespace(
            conversation_id=conv_id,
            subconversation_id=None,
            turn_id="turn-1",
            tool_call_id="call-1",
            tool_call_batch=None,
            processing_service=None,
            processing_profile_id="browser_profile",
            timezone=None,
        ),
    )
    yield ctx
    await close_browser_session(ctx)


def test_webmcp_helpers_sanitize_bound_and_format() -> None:
    assert is_webmcp_eligible_origin("https://shop.example.com") is True
    assert is_webmcp_eligible_origin("http://localhost:8000") is True
    assert is_webmcp_eligible_origin("http://shop.example.com") is False
    assert is_webmcp_eligible_origin(None) is False

    tools = sanitize_webmcp_tools(
        [
            {
                "name": "get_cart",
                "description": "Read cart",
                "inputSchema": '{"type":"object","properties":{}}',
                "annotations": {"readOnlyHint": True},
                "origin": "https://shop.example.com",
            },
            {
                "name": "evil",
                "description": "Cross-origin",
                "origin": "https://evil.example.net",
            },
        ],
        "https://shop.example.com",
    )
    assert [t["name"] for t in tools] == ["get_cart"]
    assert tools[0].get("input_schema") == {"type": "object", "properties": {}}

    parsed, truncated = bound_webmcp_result('{"content":[{"type":"text","text":"ok"}]}')
    assert truncated is False
    assert isinstance(parsed, dict)

    cut, truncated = bound_webmcp_result("a" * 100, max_bytes=20)
    assert truncated is True
    assert cut == "a" * 20

    sample = _sample_tools()
    full_block = format_webmcp_snapshot_block(sample, full_schemas=True)
    assert "untrusted page metadata" in full_block
    assert "search_catalog [read-only]: Search products in the store" in full_block
    assert "proceed_to_checkout [consequential]: Open checkout page" in full_block
    assert '"required":["query"]' in full_block

    compact_block = format_webmcp_snapshot_block(sample, full_schemas=False)
    assert "schemas shown earlier" in compact_block
    assert "search_catalog, proceed_to_checkout" in compact_block
    assert '"required"' not in compact_block

    assert webmcp_tools_fingerprint(sample) == webmcp_tools_fingerprint(_sample_tools())


def test_webmcp_js_parity_with_browser_server() -> None:
    assert WEBMCP_LIST_JS == SERVER_WEBMCP_LIST_JS
    assert WEBMCP_CALL_JS == SERVER_WEBMCP_CALL_JS


@pytest.mark.asyncio
async def test_snapshot_deduplicates_webmcp_schemas_and_resets_on_navigation(
    monkeypatch: pytest.MonkeyPatch,
    _exec_context: ToolExecutionContext,
) -> None:
    tools = _sample_tools()
    snap_with_tools: Snapshot = {
        "url": "https://shop.example.com/",
        "title": "Shop",
        "forms": 0,
        "elements": 1,
        "next_ref": 2,
        "roots": [{"ref": "e1", "role": "heading", "name": "Shop"}],
        "webmcp_tools": tools,
    }
    snap_checkout: Snapshot = {
        "url": "https://shop.example.com/checkout",
        "title": "Checkout",
        "forms": 1,
        "elements": 1,
        "next_ref": 3,
        "roots": [{"ref": "e2", "role": "heading", "name": "Checkout"}],
        "webmcp_tools": tools,
    }
    backend = _FakeBackend(
        [snap_with_tools, snap_with_tools, snap_checkout],
        call_result={
            "status": "ok",
            "tool_name": "proceed_to_checkout",
            "result": {"ok": True},
            "navigated": True,
            "url": "https://shop.example.com/checkout",
            "title": "Checkout",
        },
    )

    async def _fake_get_backend(_ctx: ToolExecutionContext) -> _FakeBackend:
        return backend

    async def _no_ucp(_ctx: ToolExecutionContext, _url: str | None) -> str | None:
        return None

    monkeypatch.setattr(browser_dom, "get_browser_backend", _fake_get_backend)
    monkeypatch.setattr(browser_dom, "_ucp_hint_on_url_change", _no_ucp)

    # First snapshot emits full schemas
    first = await browser_snapshot_tool(_exec_context)
    assert "input_schema:" in first.get_text()
    assert '"required":["query"]' in first.get_text()

    # Second snapshot with identical tools emits compact reminder only
    second = await browser_snapshot_tool(_exec_context)
    assert "schemas shown earlier" in second.get_text()
    assert "input_schema:" not in second.get_text()

    # Navigating via browser_webmcp_call resets the fingerprint and settles
    call_res = await browser_webmcp_call_tool(
        _exec_context,
        tool_name="proceed_to_checkout",
        arguments={},
        timeout_seconds=99.0,  # clamped to 30.0
    )
    assert backend.calls == [("proceed_to_checkout", {}, 30.0)]
    assert backend.settle_calls == 1
    data = call_res.get_data()
    assert isinstance(data, dict)
    assert data["status"] == "ok"
    assert data["url"] == "https://shop.example.com/checkout"

    # Next snapshot after navigation re-emits full schemas
    third = await browser_snapshot_tool(_exec_context)
    assert "input_schema:" in third.get_text()


@pytest.mark.asyncio
async def test_webmcp_call_tool_validates_blank_name_and_handles_backend_error(
    monkeypatch: pytest.MonkeyPatch,
    _exec_context: ToolExecutionContext,
) -> None:
    blank = await browser_webmcp_call_tool(_exec_context, tool_name="   ")
    blank_data = blank.get_data()
    assert isinstance(blank_data, dict)
    assert blank_data["reason"] == "invalid_tool_name"

    class _BoomBackend:
        current_url = "https://shop.example.com/"

        async def webmcp_call(
            self,
            tool_name: str,
            arguments: dict[str, object],
            *,
            timeout_seconds: float = 15.0,
        ) -> WebMCPCallResult:
            del tool_name, arguments, timeout_seconds
            raise BrowserBackendError("connection reset")

    async def _fake_get_backend(_ctx: ToolExecutionContext) -> _BoomBackend:
        return _BoomBackend()

    monkeypatch.setattr(browser_dom, "get_browser_backend", _fake_get_backend)
    failed = await browser_webmcp_call_tool(_exec_context, tool_name="get_cart")
    failed_data = failed.get_data()
    assert isinstance(failed_data, dict)
    assert failed_data["status"] == "error"
    assert failed_data["reason"] == "backend_error"
    assert "connection reset" in str(failed_data["detail"])


@pytest.mark.asyncio
async def test_remote_backend_webmcp_call_and_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROWSER_HANDOFF_SERVICE_TOKEN", "test-token")
    seen: list[httpx.Request] = []
    mode = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"session_id": "bs_webmcp"})
        if request.url.path == "/v1/sessions/bs_webmcp/webmcp-call":
            if mode["status"] == 403:
                return httpx.Response(
                    403,
                    json={
                        "detail": "WebMCP calls are denied unless the agent owns the lease"
                    },
                )
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "tool_name": body["tool_name"],
                    "result": {"count": 1},
                    "url": "https://shop.example.com/cart",
                    "title": "Cart",
                    "navigated": True,
                },
            )
        return httpx.Response(404)

    cfg = BrowserHandoffConfig(
        enabled=True,
        service_url="http://browser-server.local",
        auth=RemoteA2AAuthConfig(
            type="bearer", token_env="BROWSER_HANDOFF_SERVICE_TOKEN"
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://browser-server.local",
    )
    backend = RemoteBrowserBackend(cfg, "conv-webmcp", client=client)
    res = await backend.webmcp_call("add_to_cart", {"sku": "A1"}, timeout_seconds=12.0)
    assert res["status"] == "ok"
    assert res.get("result") == {"count": 1}
    assert backend.current_url == "https://shop.example.com/cart"
    assert seen[-1].url.path == "/v1/sessions/bs_webmcp/webmcp-call"
    sent_payload = json.loads(seen[-1].content)
    assert sent_payload == {
        "tool_name": "add_to_cart",
        "arguments": {"sku": "A1"},
        "timeout_seconds": 12.0,
    }

    # Lease lost raises BrowserBackendError and clears session
    mode["status"] = 403
    with pytest.raises(BrowserBackendError, match="no longer|not available"):
        await backend.webmcp_call("add_to_cart", {})
    assert backend.session_id is None

    # Authenticated session raises AuthenticatedSessionUnavailableError on lease loss
    auth_backend = RemoteBrowserBackend(
        cfg,
        "conv-webmcp-auth",
        client=client,
        authenticated=AuthenticatedSessionSpec(
            site_id="shop",
            jar_id=None,
            confine_origins=frozenset({"https://shop.example.com"}),
            credential_alias=None,
        ),
    )
    auth_backend._session_id = "bs_webmcp"  # Simulate bound authenticated session
    with pytest.raises(AuthenticatedSessionUnavailableError):
        await auth_backend.webmcp_call("add_to_cart", {})
    await client.aclose()


def test_tool_call_review_and_catalog_registration_for_webmcp() -> None:
    assert "browser_webmcp_call" in AVAILABLE_FUNCTIONS
    assert any(d["function"]["name"] == "browser_webmcp_call" for d in TOOLS_DEFINITION)
    meta = LOCAL_TOOL_METADATA_BY_NAME["browser_webmcp_call"]
    assert ToolTag.BROWSER in meta.tags
    assert ToolTag.OUTPUT_UNTRUSTED in meta.tags
    assert "browser_webmcp_call" in admissible_tools(LOCAL_TOOL_METADATA_BY_NAME)

    # ToolCallReviewer extracts current page URL & title from browser_webmcp_call results
    msg = ToolMessage(
        tool_call_id="call_1",
        name="browser_webmcp_call",
        content=json.dumps({
            "status": "ok",
            "tool_name": "proceed_to_checkout",
            "url": "https://shop.example.com/checkout",
            "title": "Checkout",
            "navigated": True,
        }),
    )
    reported = _reported_page(msg)
    assert reported == {"url": "https://shop.example.com/checkout", "title": "Checkout"}

    descriptor = ToolDescriptor(
        name="browser_webmcp_call",
        definition={
            "type": "function",
            "function": {
                "name": "browser_webmcp_call",
                "description": "Call a WebMCP tool",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        tags=frozenset({ToolTag.BROWSER, ToolTag.STATE_CHANGING}),
        origin="local",
    )
    current = resolve_browser_action_page([msg], descriptor, {"tool_name": "get_cart"})
    assert current == {"url": "https://shop.example.com/checkout", "title": "Checkout"}

    # BrowserSession starts with clean WebMCP state
    session = BrowserSession()
    assert session.last_webmcp_fingerprint is None
    assert session.last_webmcp_tools == {}
