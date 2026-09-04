"""Tests for the Keychute-backed Monty HTTP API."""

import asyncio
import base64
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import SecretStr

from family_assistant.config_models import KeychuteConfig
from family_assistant.scripting.apis.keychute import (
    KeychuteScriptError,
    KeychuteScriptHttpClient,
    add_keychute_http_api,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyMode,
    TaintSource,
    TaintSourceType,
)
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    TaintTrackingToolsProvider,
)
from family_assistant.tools.types import ToolCallReviewTurnState, ToolExecutionContext

_REQUEST_ID = "11111111-1111-4111-8111-111111111111"
_GRANT_ID = "22222222-2222-4222-8222-222222222222"


def _taint_execution_context(
    tracker: InMemoryTurnTaintTracker,
    *,
    policy: TaintPolicyConfig | None = None,
) -> Mock:
    provider = TaintTrackingToolsProvider(
        CompositeToolsProvider([]),
        taint_policy=policy,
    )
    execution_context = Mock(spec=ToolExecutionContext)
    execution_context.taint_tracker = tracker
    execution_context.tools_provider = provider
    execution_context.processing_service = None
    execution_context.request_confirmation_callback = None
    execution_context.tool_call_review_state = ToolCallReviewTurnState()
    execution_context.tool_call_review_messages = ()
    execution_context.tool_call_review_trigger = None
    execution_context.interface_type = "test"
    execution_context.conversation_id = "conversation"
    execution_context.turn_id = "turn"
    execution_context.processing_profile_id = "profile"
    execution_context.subconversation_id = None
    execution_context.user_id = "user"
    execution_context.db_context = Mock()
    execution_context.db_context.taint_audit_events.add = AsyncMock()
    return execution_context


@pytest.mark.asyncio
async def test_request_uses_direct_api_and_preserves_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYCHUTE_TOKEN", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("first-token", encoding="utf-8")
    script_source = (
        'result = keychute_http_request("weather", "https://example.test/x")'
    )
    created_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created_body
        if request.url.path == "/v1/access-requests":
            assert request.headers["authorization"] == "Bearer first-token"
            created_body = json.loads(request.content)
            token_file.write_text("rotated-token", encoding="utf-8")
            return httpx.Response(
                201,
                json={"request_id": _REQUEST_ID, "state": "pending"},
            )
        assert request.headers["authorization"] == "Bearer rotated-token"
        if request.url.path.endswith("/wait"):
            return httpx.Response(
                200,
                json={
                    "request_id": _REQUEST_ID,
                    "state": "approved",
                    "grant_id": _GRANT_ID,
                },
            )
        if request.url.path == f"/v1/grants/{_GRANT_ID}":
            return httpx.Response(
                200,
                json={
                    "grant_id": _GRANT_ID,
                    "mechanism": "brokered",
                    "constraints": {
                        "origins": [{"host": "example.test"}],
                        "methods": ["POST"],
                        "path_prefixes": ["/x"],
                        "ttl_seconds": 60,
                        "max_uses": 2,
                    },
                    "revoked": False,
                },
            )
        assert request.url.path == f"/v1/grants/{_GRANT_ID}/proxy/x"
        assert request.url.query == b"q=1"
        assert request.headers["content-type"] == "application/json"
        assert request.content == b'{"city":"Melbourne"}'
        return httpx.Response(
            201,
            headers=[("X-Test", "first"), ("X-Test", "second")],
            json={"ok": True},
        )

    tracker = InMemoryTurnTaintTracker()
    execution_context = _taint_execution_context(tracker)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True,
                url="https://keychute.test",
                token_file=str(token_file),
            ),
            script_source,
            execution_context,
            http_client,
        )
        response = await client.request(
            "weather",
            "https://example.test/x?q=1",
            method="POST",
            headers={"Content-Type": "application/json"},
            body='{"city":"Melbourne"}',
            reason="forecast",
            ttl_seconds=60,
            max_uses=2,
            approval_timeout_seconds=10,
            request_timeout_seconds=5,
        )

    assert response["status_code"] == 201
    assert response["headers"]["x-test"] == ["first", "second"]
    assert json.loads(response["body"]) == {"ok": True}
    assert tracker.snapshot().max_tier is SourceTrustTier.UNKNOWN_EXTERNAL
    assert created_body["secret_name"] == "weather"
    assert created_body["mechanism"] == "brokered"
    assert created_body["constraints"] == {
        "origins": [{"host": "example.test"}],
        "methods": ["POST"],
        "path_prefixes": ["/x"],
        "ttl_seconds": 60,
        "max_uses": 2,
    }
    assert created_body["context"] == {
        "reason": "forecast",
        "structured": {"script": script_source},
    }
    execution_context.db_context.taint_audit_events.add.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_denies_high_taint_before_contacting_keychute() -> None:
    tracker = InMemoryTurnTaintTracker()
    tracker.add_source(
        TaintSource(
            source_type=TaintSourceType.TOOL_OUTPUT,
            source_id="attacker-controlled-response",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Untrusted external content entered the script.",
        )
    )
    execution_context = _taint_execution_context(
        tracker,
        policy=TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Taint-denied egress must not contact Keychute")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True,
                url="https://keychute.test",
                token=SecretStr("client-token"),
            ),
            "keychute_http_request(...) ",
            execution_context,
            http_client,
        )
        with pytest.raises(
            KeychuteScriptError,
            match="egress denied by runtime taint policy",
        ):
            await client.request("weather", "https://example.test/x")

    audit = execution_context.db_context.taint_audit_events.add
    policy_calls = [
        call
        for call in audit.await_args_list
        if call.kwargs["event_type"] == "policy_evaluation"
    ]
    review_calls = [
        call
        for call in audit.await_args_list
        if call.kwargs["event_type"] == "tool_call_review"
    ]
    assert len(policy_calls) == 1
    assert len(review_calls) == 1
    assert policy_calls[0].kwargs["sink_class"] == SinkClass.SANDBOX_NETWORK.value
    assert policy_calls[0].kwargs["effective_outcome"] == "adjudicate"
    assert review_calls[0].kwargs["review_verdict"] == "deny"


@pytest.mark.asyncio
async def test_request_review_receives_complete_outbound_envelope() -> None:
    execution_context = _taint_execution_context(InMemoryTurnTaintTracker())
    authorizer = execution_context.tools_provider
    assert isinstance(authorizer, TaintTrackingToolsProvider)
    authorize = AsyncMock()
    client = KeychuteScriptHttpClient(
        KeychuteConfig(
            enabled=True, url="https://keychute.test", token=SecretStr("token")
        ),
        "keychute_http_request(...) ",
        execution_context,
    )

    with patch.object(authorizer, "authorize_taint_sink", authorize):
        await client._authorize_egress(
            secret_name="weather",
            url="https://example.test/x",
            method="POST",
            headers={"Content-Type": "application/json", "X-Tenant": "home"},
            request_body=b'{"city":"Melbourne"}',
            reason="forecast",
            ttl_seconds=60,
            max_uses=2,
            approval_timeout_seconds=10,
            request_timeout_seconds=5.0,
        )

    authorize.assert_awaited_once()
    awaited = authorize.await_args
    assert awaited is not None
    assert awaited.kwargs["arguments"] == {
        "secret_name": "weather",
        "url": "https://example.test/x",
        "method": "POST",
        "headers": {"Content-Type": "application/json", "X-Tenant": "home"},
        "body": {"encoding": "utf-8", "content": '{"city":"Melbourne"}'},
        "reason": "forecast",
        "ttl_seconds": 60,
        "max_uses": 2,
        "approval_timeout_seconds": 10,
        "request_timeout_seconds": 5.0,
    }


@pytest.mark.asyncio
async def test_large_binary_review_body_is_encoded_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_context = _taint_execution_context(InMemoryTurnTaintTracker())
    authorizer = execution_context.tools_provider
    assert isinstance(authorizer, TaintTrackingToolsProvider)
    authorize = AsyncMock()
    client = KeychuteScriptHttpClient(
        KeychuteConfig(
            enabled=True, url="https://keychute.test", token=SecretStr("token")
        ),
        "keychute_http_request(...) ",
        execution_context,
    )
    request_body = b"\xff" * (4 * 1024 * 1024)
    encoding_started = threading.Event()
    release_encoding = threading.Event()
    original_b64encode = base64.b64encode

    def blocking_b64encode(value: bytes) -> bytes:
        encoding_started.set()
        release_encoding.wait(timeout=1)
        return original_b64encode(value)

    monkeypatch.setattr(base64, "b64encode", blocking_b64encode)

    with patch.object(authorizer, "authorize_taint_sink", authorize):
        review_task = asyncio.create_task(
            client._authorize_egress(
                secret_name="binary-api",
                url="https://example.test/upload",
                method="POST",
                headers={"Content-Type": "application/octet-stream"},
                request_body=request_body,
                reason="upload exact payload",
                ttl_seconds=60,
                max_uses=1,
                approval_timeout_seconds=10,
                request_timeout_seconds=5.0,
            )
        )
        try:
            assert await asyncio.to_thread(encoding_started.wait, 0.5)
            event_loop_progressed = asyncio.Event()
            asyncio.get_running_loop().call_soon(event_loop_progressed.set)
            await asyncio.wait_for(event_loop_progressed.wait(), timeout=0.1)
        finally:
            release_encoding.set()
        await review_task

    authorize.assert_awaited_once()
    awaited = authorize.await_args
    assert awaited is not None
    body = awaited.kwargs["arguments"]["body"]
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == request_body


@pytest.mark.asyncio
async def test_request_surfaces_keychute_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": "policy-denied", "message": "denied"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True,
                url="https://keychute.test",
                token=SecretStr("client-token"),
            ),
            "keychute_http_request(...)",
            http_client=http_client,
        )
        with pytest.raises(
            KeychuteScriptError,
            match=r"HTTP 403: denied \(policy-denied\)",
        ):
            await client.request("weather", "https://example.test/x")


@pytest.mark.asyncio
async def test_request_enforces_response_body_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/access-requests":
            return httpx.Response(
                201,
                json={
                    "request_id": _REQUEST_ID,
                    "state": "approved",
                    "grant_id": _GRANT_ID,
                },
            )
        if request.url.path == f"/v1/grants/{_GRANT_ID}":
            return httpx.Response(
                200,
                json={
                    "mechanism": "brokered",
                    "constraints": {
                        "origins": [{"host": "example.test"}],
                        "methods": ["GET"],
                    },
                    "revoked": False,
                },
            )
        return httpx.Response(200, content=b"four")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True,
                url="https://keychute.test",
                token=SecretStr("client-token"),
                max_response_bytes=3,
            ),
            "keychute_http_request(...)",
            http_client=http_client,
        )
        with pytest.raises(KeychuteScriptError, match="3-byte limit"):
            await client.request("weather", "https://example.test/x")


def test_api_is_added_only_when_enabled() -> None:
    original: dict[str, object] = {"value": 1}

    assert (
        add_keychute_http_api(
            original,
            config=KeychuteConfig(enabled=False),
            script_source="value",
        )
        is original
    )
    enabled = add_keychute_http_api(
        original,
        config=KeychuteConfig(enabled=True),
        script_source="value",
    )
    assert enabled is not None
    assert callable(enabled["keychute_http_request"])
