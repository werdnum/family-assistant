"""Tests for the Keychute-backed Monty HTTP API."""

import asyncio
import base64
import json
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

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
from family_assistant.storage.database import Database
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    TaintTrackingToolsProvider,
    ToolPolicyDeniedError,
)
from family_assistant.tools.types import ToolCallReviewTurnState, ToolExecutionContext

_REQUEST_ID = "11111111-1111-4111-8111-111111111111"
_GRANT_ID = "22222222-2222-4222-8222-222222222222"


def _taint_execution_context(
    db: Database,
    tracker: InMemoryTurnTaintTracker,
    *,
    policy: TaintPolicyConfig | None = None,
    provider: TaintTrackingToolsProvider | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="tester",
        turn_id="turn",
        db_context=db,
        processing_service=None,
        clock=None,
        plugins=None,
        event_sources=None,
        attachment_registry=None,
        credential_resolvers=None,
        api_backend=None,
        timezone=ZoneInfo("UTC"),
        user_id="user",
        processing_profile_id="profile",
        tools_provider=provider
        or TaintTrackingToolsProvider(CompositeToolsProvider([]), taint_policy=policy),
        taint_tracker=tracker,
        tool_call_review_state=ToolCallReviewTurnState(),
        tool_call_review_messages=(),
    )


@pytest.mark.asyncio
async def test_request_uses_direct_api_and_preserves_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_engine: AsyncEngine,
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
    db = Database(engine=db_engine)
    execution_context = _taint_execution_context(db, tracker)
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
    assert len(await db.taint_audit_events.list_for_turn("turn")) == 1


@pytest.mark.asyncio
async def test_request_denies_high_taint_before_contacting_keychute(
    db_engine: AsyncEngine,
) -> None:
    """A script with no confirmation channel is refused without reaching Keychute."""
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
    db = Database(engine=db_engine)
    execution_context = _taint_execution_context(
        db,
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

    audit = await db.taint_audit_events.list_for_turn("turn")
    policy_events = [e for e in audit if e["event_type"] == "policy_evaluation"]
    review_events = [e for e in audit if e["event_type"] == "tool_call_review"]
    assert len(policy_events) == 1
    assert len(review_events) == 1
    assert policy_events[0]["sink_class"] == SinkClass.SANDBOX_NETWORK.value
    assert policy_events[0]["effective_outcome"] == "adjudicate"
    assert review_events[0]["review_verdict"] == "confirm"


class _DenyingReviewToolsProvider(TaintTrackingToolsProvider):
    """Taint reviewer that refuses, so the request stops before Keychute."""

    def __init__(self) -> None:
        super().__init__(CompositeToolsProvider([]))
        # ast-grep-ignore: no-dict-any - records the reviewer's keyword arguments verbatim
        self.review_requests: list[dict[str, Any]] = []

    async def authorize_taint_sink(
        self,
        *,
        name: str,
        sink_class: SinkClass,
        # ast-grep-ignore: no-dict-any - mirrors the sink arguments of the real reviewer
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        call_id: str | None = None,
        taint_policy: TaintPolicyConfig | None = None,
    ) -> None:
        self.review_requests.append({
            "name": name,
            "sink_class": sink_class,
            "arguments": arguments,
            "context": context,
        })
        raise ToolPolicyDeniedError("keychute_http_request", "held for inspection")


def _fail_if_keychute_contacted(_request: httpx.Request) -> httpx.Response:
    pytest.fail("A refused review must not contact Keychute")


@pytest.mark.asyncio
async def test_request_review_receives_complete_outbound_envelope(
    db_engine: AsyncEngine,
) -> None:
    authorizer = _DenyingReviewToolsProvider()
    execution_context = _taint_execution_context(
        Database(engine=db_engine), InMemoryTurnTaintTracker(), provider=authorizer
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_fail_if_keychute_contacted)
    ) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True, url="https://keychute.test", token=SecretStr("token")
            ),
            "keychute_http_request(...) ",
            execution_context,
            http_client,
        )
        with pytest.raises(
            KeychuteScriptError,
            match="egress denied by runtime taint policy: held for inspection",
        ):
            await client.request(
                "weather",
                "https://example.test/x",
                method="post",
                headers={"Content-Type": "application/json", "X-Tenant": "home"},
                body='{"city":"Melbourne"}',
                reason="forecast",
                ttl_seconds=60,
                max_uses=2,
                approval_timeout_seconds=10,
                request_timeout_seconds=5.0,
            )

    assert len(authorizer.review_requests) == 1
    review = authorizer.review_requests[0]
    assert review["context"] is execution_context
    assert review["sink_class"] is SinkClass.SANDBOX_NETWORK
    assert review["arguments"] == {
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
    db_engine: AsyncEngine,
) -> None:
    authorizer = _DenyingReviewToolsProvider()
    execution_context = _taint_execution_context(
        Database(engine=db_engine), InMemoryTurnTaintTracker(), provider=authorizer
    )
    request_body = b"\xff" * (4 * 1024 * 1024)
    encoding_started = threading.Event()
    release_encoding = threading.Event()
    released_by_event_loop: list[bool] = []
    original_b64encode = base64.b64encode

    def blocking_b64encode(value: bytes) -> bytes:
        encoding_started.set()
        released_by_event_loop.append(release_encoding.wait(timeout=10))
        return original_b64encode(value)

    monkeypatch.setattr(base64, "b64encode", blocking_b64encode)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_fail_if_keychute_contacted)
    ) as http_client:
        client = KeychuteScriptHttpClient(
            KeychuteConfig(
                enabled=True, url="https://keychute.test", token=SecretStr("token")
            ),
            "keychute_http_request(...) ",
            execution_context,
            http_client,
        )
        request_task = asyncio.create_task(
            client.request(
                "binary-api",
                "https://example.test/upload",
                method="POST",
                headers={"Content-Type": "application/octet-stream"},
                body=request_body,
                reason="upload exact payload",
            )
        )
        try:
            encoding_started_in_time = await asyncio.to_thread(
                encoding_started.wait, 10
            )
        finally:
            release_encoding.set()
        with pytest.raises(KeychuteScriptError, match="held for inspection"):
            await request_task

    assert encoding_started_in_time
    assert released_by_event_loop == [True]
    assert len(authorizer.review_requests) == 1
    body = authorizer.review_requests[0]["arguments"]["body"]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"key": "value"}, ["a"], 3])
async def test_request_rejects_non_bytes_body_before_contacting_keychute(
    body: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Keychute contacted: {request.url}")

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
        with pytest.raises(TypeError, match=r"body must be str or bytes.*json_encode"):
            await client.request(
                "weather",
                "https://example.test/x",
                method="POST",
                body=body,  # type: ignore[arg-type]
            )


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
