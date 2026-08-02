"""Tests for the A2A (Agent-to-Agent) protocol API endpoints.

Validates protocol compliance against the official a2a-sdk types.
"""

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from a2a.client import Client, ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.errors import A2AClientJSONRPCError
from a2a.types import AgentCard as SdkAgentCard
from a2a.types import Message as SdkMessage
from a2a.types import Part as SdkPart
from a2a.types import Role as SdkRole
from a2a.types import SendMessageResponse as SdkSendMessageResponse
from a2a.types import SendStreamingMessageResponse as SdkStreamResponse
from a2a.types import TaskIdParams, TaskQueryParams
from a2a.types import TextPart as SdkTextPart
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from family_assistant.processing import ProcessingService
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput
from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest_asyncio.fixture
async def a2a_client(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
) -> AsyncGenerator[AsyncClient]:
    """HTTPX client with processing_services registry set for A2A endpoints."""
    profile_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {
        profile_id: api_test_processing_service,
    }
    app_fixture.state.a2a_cancel_events = {}
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _jsonrpc(
    method: str,
    params: dict | None = None,
    request_id: str | int = 1,
) -> dict:
    """Build a JSON-RPC 2.0 request body."""
    body: dict = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        body["params"] = params
    return body


def _a2a_message(
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
    extra_parts: list[dict] | None = None,
) -> dict:
    """Build an A2A message dict with required fields."""
    parts: list[dict] = [{"kind": "text", "text": text}]
    if extra_parts:
        parts.extend(extra_parts)
    msg: dict = {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": parts,
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    return msg


class TestAgentCard:
    @pytest.mark.asyncio
    async def test_agent_card_returns_valid_card(self, a2a_client: AsyncClient) -> None:
        resp = await a2a_client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        card = resp.json()
        assert card["name"].startswith("Family Assistant")
        assert card["url"] == "http://testserver/api/a2a"
        assert card["version"] == "0.1.0"
        assert card["capabilities"]["streaming"] is True
        assert "skills" in card

    @pytest.mark.asyncio
    async def test_agent_card_includes_profile_skills(
        self, a2a_client: AsyncClient
    ) -> None:
        resp = await a2a_client.get("/.well-known/agent.json")
        card = resp.json()
        skills = card["skills"]
        assert len(skills) >= 1
        skill_ids = [s["id"] for s in skills]
        assert "chat_api_test_profile" in skill_ids


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_returns_completed_task(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="Hello from A2A!")

        body = _jsonrpc(
            "message/send",
            params={"message": _a2a_message("Hello")},
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )

        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert "result" in data

        task = data["result"]
        assert "id" in task
        assert task["status"]["state"] == "completed"
        assert task["artifacts"] is not None
        assert len(task["artifacts"]) >= 1

    @pytest.mark.asyncio
    async def test_send_message_with_file_part_reaches_llm(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """A file URL sent via A2A must reach the LLM, not get silently dropped."""
        api_mock_llm_client.default_response = MockLLMOutput(
            content="I can see the file"
        )

        body = _jsonrpc(
            "message/send",
            params={
                "message": _a2a_message(
                    "Look at this",
                    extra_parts=[
                        {
                            "kind": "file",
                            "file": {"uri": "https://example.com/report.pdf"},
                        },
                    ],
                )
            },
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        calls = api_mock_llm_client.get_calls()
        assert len(calls) >= 1
        all_content = str(calls[-1]["kwargs"]["messages"])
        assert "https://example.com/report.pdf" in all_content, (
            f"File URL was silently dropped! LLM received: {all_content}"
        )

    @pytest.mark.asyncio
    async def test_send_message_with_task_id(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="OK")

        body = _jsonrpc(
            "message/send",
            params={
                "message": _a2a_message(
                    "test", task_id="my-custom-task-123", context_id="my-ctx-456"
                )
            },
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        task = resp.json()["result"]
        assert task["id"] == "my-custom-task-123"
        assert task["contextId"] == "my-ctx-456"

    @pytest.mark.asyncio
    async def test_send_message_invalid_params(self, a2a_client: AsyncClient) -> None:
        body = _jsonrpc(
            "message/send",
            params={"bad": "params"},
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32602  # INVALID_PARAMS


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_task_after_send(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="reply")

        send_body = _jsonrpc(
            "message/send",
            params={"message": _a2a_message("hello", task_id="get-test-task")},
        )
        await a2a_client.post("/api/a2a", json=send_body)

        get_body = _jsonrpc("tasks/get", params={"id": "get-test-task"})
        resp = await a2a_client.post("/api/a2a", json=get_body)
        assert resp.status_code == 200

        data = resp.json()
        assert "result" in data
        task = data["result"]
        assert task["id"] == "get-test-task"
        assert task["status"]["state"] == "completed"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, a2a_client: AsyncClient) -> None:
        body = _jsonrpc("tasks/get", params={"id": "nonexistent-task"})
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32001  # TASK_NOT_FOUND


class TestCancelTask:
    @pytest.mark.asyncio
    async def test_cancel_completed_task_fails(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="done")

        send_body = _jsonrpc(
            "message/send",
            params={"message": _a2a_message("hello", task_id="cancel-test-task")},
        )
        await a2a_client.post("/api/a2a", json=send_body)

        cancel_body = _jsonrpc("tasks/cancel", params={"id": "cancel-test-task"})
        resp = await a2a_client.post("/api/a2a", json=cancel_body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32002  # TASK_NOT_CANCELABLE

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_cancel_during_blocking_send_reports_the_canceled_task(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A blocking send that loses to a cancel must not report completion.

        Its 'working' row is durable the moment it is written, so a concurrent
        tasks/cancel can land mid-send. The guarded terminal update then
        no-ops -- canceled wins in the database -- and the response has to say
        so rather than handing back a completed task the row contradicts.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        original_generate = api_mock_llm_client.generate_response

        async def gated_generate(*args: object, **kwargs: object) -> MockLLMOutput:
            started.set()
            await release.wait()
            return await original_generate(*args, **kwargs)  # type: ignore[arg-type] # passthrough of the mock's signature

        monkeypatch.setattr(api_mock_llm_client, "generate_response", gated_generate)

        task_id = str(uuid.uuid4())
        send_task = asyncio.create_task(
            a2a_client.post(
                "/api/a2a",
                json=_jsonrpc(
                    "message/send",
                    params={"message": _a2a_message("slow", task_id=task_id)},
                ),
            )
        )

        # The task row is written before the LLM call, so reaching the gate
        # means tasks/cancel has something to cancel.
        await asyncio.wait_for(started.wait(), timeout=10)

        cancel = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/cancel", params={"id": task_id})
        )
        assert cancel.json()["result"]["status"]["state"] == "canceled"

        release.set()
        send = await asyncio.wait_for(send_task, timeout=10)

        assert send.json()["result"]["status"]["state"] == "canceled"

        get_resp = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/get", params={"id": task_id})
        )
        assert get_resp.json()["result"]["status"]["state"] == "canceled"

    async def test_cancel_nonexistent_task(self, a2a_client: AsyncClient) -> None:
        body = _jsonrpc("tasks/cancel", params={"id": "no-such-task"})
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32001  # TASK_NOT_FOUND


class TestAsyncSendMessage:
    """message/send with configuration.blocking=false (background processing)."""

    @pytest.mark.asyncio
    async def test_blocking_false_returns_working_then_completes(
        self,
        a2a_client: AsyncClient,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="async reply")
        task_id = str(uuid.uuid4())

        body = _jsonrpc(
            "message/send",
            params={
                "message": _a2a_message("Hello", task_id=task_id),
                "configuration": {"blocking": False},
            },
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200, resp.text

        # The response is the non-terminal working task, returned before the
        # background task runs.
        task = resp.json()["result"]
        assert task["id"] == task_id
        assert task["status"]["state"] == "working"

        # Let the background task finish, then the persisted task is terminal.
        background = app_fixture.state.a2a_background_tasks.get(task_id)
        if background is not None:
            await background

        get_resp = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/get", params={"id": task_id})
        )
        completed = get_resp.json()["result"]
        assert completed["status"]["state"] == "completed"
        assert completed["artifacts"]

    @pytest.mark.asyncio
    async def test_resending_same_task_id_is_idempotent(
        self,
        a2a_client: AsyncClient,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="once only")
        task_id = str(uuid.uuid4())
        params = {
            "message": _a2a_message("Hello", task_id=task_id),
            "configuration": {"blocking": False},
        }

        first = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("message/send", params=params)
        )
        assert first.json()["result"]["status"]["state"] == "working"
        background = app_fixture.state.a2a_background_tasks.get(task_id)
        if background is not None:
            await background
        calls_after_first = len(api_mock_llm_client.get_calls())

        # Re-sending the same task id returns the existing (completed) task and
        # does not re-process it (no second LLM call, no duplicate background run).
        second = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("message/send", params=params)
        )
        result = second.json()["result"]
        assert result["id"] == task_id
        assert result["status"]["state"] == "completed"
        assert len(api_mock_llm_client.get_calls()) == calls_after_first

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_concurrent_resends_same_task_id_are_idempotent(
        self,
        a2a_client: AsyncClient,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        release = asyncio.Event()
        api_mock_llm_client.default_response = MockLLMOutput(content="once only")
        api_mock_llm_client.response_gate = release

        task_id = str(uuid.uuid4())
        params = {
            "message": _a2a_message("Hello", task_id=task_id),
            "configuration": {"blocking": False},
        }

        async def send(request_id: int) -> dict:
            resp = await a2a_client.post(
                "/api/a2a",
                json=_jsonrpc("message/send", params=params, request_id=request_id),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "error" not in data
            return data["result"]

        first, second = await asyncio.gather(send(1), send(2))

        assert first["id"] == task_id
        assert second["id"] == task_id
        assert first["status"]["state"] == "working"
        assert second["status"]["state"] == "working"

        background = app_fixture.state.a2a_background_tasks.get(task_id)
        assert background is not None
        release.set()
        await background

        assert len(api_mock_llm_client.get_calls()) == 1

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_blocking_false_cancel_interrupts_in_flight_task(
        self,
        a2a_client: AsyncClient,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        # Postgres-only: hard-cancelling the in-flight background task tears down
        # its DB connection. The production pool gives that task its own
        # connection; SQLite's shared StaticPool connection would be closed for
        # the whole engine, which is a test-harness artifact, not a real bug.
        # Gate the fake LLM so the background task stays in flight until released,
        # giving tasks/cancel a running task to interrupt.
        release = asyncio.Event()
        api_mock_llm_client.response_gate = release

        task_id = str(uuid.uuid4())
        send = await a2a_client.post(
            "/api/a2a",
            json=_jsonrpc(
                "message/send",
                params={
                    "message": _a2a_message("slow", task_id=task_id),
                    "configuration": {"blocking": False},
                },
            ),
        )
        assert send.json()["result"]["status"]["state"] == "working"

        # The background task is parked on the gate; cancel it.
        cancel = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/cancel", params={"id": task_id})
        )
        assert cancel.status_code == 200
        assert cancel.json()["result"]["status"]["state"] == "canceled"

        background = app_fixture.state.a2a_background_tasks.get(task_id)
        if background is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await background
        release.set()

        get_resp = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/get", params={"id": task_id})
        )
        assert get_resp.json()["result"]["status"]["state"] == "canceled"

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_blocking_false_shutdown_cancel_persists_terminal(
        self,
        a2a_client: AsyncClient,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        # A graceful shutdown (stop_services) cancels in-flight background sends
        # DIRECTLY — not via tasks/cancel — so no row was pre-marked canceled. The
        # handler must still persist a terminal state, otherwise the row is stuck
        # 'working' forever (after a restart there is no background work to finish
        # it, so tasks/get would never reach terminal). Postgres-only for the same
        # connection-teardown reason as the tasks/cancel test above.
        release = asyncio.Event()
        api_mock_llm_client.response_gate = release

        task_id = str(uuid.uuid4())
        send = await a2a_client.post(
            "/api/a2a",
            json=_jsonrpc(
                "message/send",
                params={
                    "message": _a2a_message("slow", task_id=task_id),
                    "configuration": {"blocking": False},
                },
            ),
        )
        assert send.json()["result"]["status"]["state"] == "working"

        # Simulate shutdown: cancel the background task directly (no tasks/cancel).
        background = app_fixture.state.a2a_background_tasks.get(task_id)
        assert background is not None
        background.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background
        release.set()

        get_resp = await a2a_client.post(
            "/api/a2a", json=_jsonrpc("tasks/get", params={"id": task_id})
        )
        assert get_resp.json()["result"]["status"]["state"] == "canceled"


class TestUnknownMethod:
    @pytest.mark.asyncio
    async def test_unknown_method_returns_error(self, a2a_client: AsyncClient) -> None:
        body = _jsonrpc("nonexistent/method")
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32601  # METHOD_NOT_FOUND


class TestStreamMessage:
    @pytest.mark.asyncio
    async def test_stream_returns_sse_events(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(
            content="Streamed response"
        )

        body = _jsonrpc(
            "message/stream",
            params={"message": _a2a_message("Hello stream")},
        )
        resp = await a2a_client.post("/api/a2a/stream", json=body)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_stream_persists_task(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(
            content="Persisted streamed response"
        )

        stream_body = _jsonrpc(
            "message/stream",
            params={
                "message": _a2a_message("Hello persist", task_id="stream-persist-test")
            },
        )
        resp = await a2a_client.post("/api/a2a/stream", json=stream_body)
        assert resp.status_code == 200

        get_body = _jsonrpc("tasks/get", params={"id": "stream-persist-test"})
        resp = await a2a_client.post("/api/a2a", json=get_body)
        assert resp.status_code == 200

        data = resp.json()
        assert "result" in data
        task = data["result"]
        assert task["id"] == "stream-persist-test"
        assert task["status"]["state"] == "completed"
        assert task["artifacts"] is not None
        assert len(task["artifacts"]) >= 1
        assert task["history"] is not None
        assert len(task["history"]) >= 2

    @pytest.mark.asyncio
    async def test_stream_wrong_method_returns_error(
        self, a2a_client: AsyncClient
    ) -> None:
        body = _jsonrpc(
            "tasks/get",
            params={"id": "some-task"},
        )
        resp = await a2a_client.post("/api/a2a/stream", json=body)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def _parse_sse_events(raw: str) -> list[dict]:
    """Extract JSON data payloads from raw SSE text."""
    events: list[dict] = []
    for block in re.split(r"\n\n+", raw.strip()):
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


class TestSdkCompliance:
    """Validate our responses parse correctly with the official a2a-sdk types.

    These tests catch protocol drift -- if our output doesn't match
    the SDK's Pydantic models, the test fails immediately.
    """

    @pytest.mark.asyncio
    async def test_agent_card_parses_with_sdk(self, a2a_client: AsyncClient) -> None:
        """Agent card must be valid per the SDK's AgentCard model."""
        resp = await a2a_client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        SdkAgentCard.model_validate(resp.json())

    @pytest.mark.asyncio
    async def test_agent_card_v2_path_parses_with_sdk(
        self, a2a_client: AsyncClient
    ) -> None:
        """The spec v0.3.0 path /.well-known/agent-card.json must also work."""
        resp = await a2a_client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        SdkAgentCard.model_validate(resp.json())

    @pytest.mark.asyncio
    async def test_send_message_parses_with_sdk(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """message/send response must be valid per the SDK's SendMessageResponse."""
        api_mock_llm_client.default_response = MockLLMOutput(content="SDK test reply")

        body = _jsonrpc(
            "message/send",
            params={"message": _a2a_message("SDK compliance test")},
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200
        parsed = SdkSendMessageResponse.model_validate(resp.json())
        assert hasattr(parsed.root, "result"), (
            f"Expected success response, got error: {parsed.root}"
        )

    @pytest.mark.asyncio
    async def test_stream_events_parse_with_sdk(
        self,
        a2a_client: AsyncClient,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """Every SSE event from message/stream must parse with the SDK types."""
        api_mock_llm_client.default_response = MockLLMOutput(content="SDK stream test")

        body = _jsonrpc(
            "message/stream",
            params={"message": _a2a_message("Stream SDK test")},
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        events = _parse_sse_events(resp.text)
        assert len(events) >= 2, f"Expected at least 2 SSE events, got {len(events)}"

        for i, event_data in enumerate(events):
            parsed = SdkStreamResponse.model_validate(event_data)
            assert hasattr(parsed.root, "result"), (
                f"SSE event {i} was an error, not a result: {event_data}"
            )


def _sdk_message(
    text: str,
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> SdkMessage:
    """Build an SDK Message object for use with the SDK client."""
    return SdkMessage(
        role=SdkRole.user,
        message_id=str(uuid.uuid4()),
        parts=[SdkPart(root=SdkTextPart(text=text))],
        task_id=task_id,
        context_id=context_id,
    )


@pytest_asyncio.fixture
async def sdk_client(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
) -> AsyncGenerator[Client]:
    """SDK Client backed by ASGITransport for in-process testing."""
    profile_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {
        profile_id: api_test_processing_service,
    }
    app_fixture.state.a2a_cancel_events = {}
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as httpx_client:
        card_resp = await httpx_client.get("/.well-known/agent.json")
        card = SdkAgentCard.model_validate(card_resp.json())
        client = await ClientFactory.connect(
            card, client_config=ClientConfig(httpx_client=httpx_client)
        )
        yield client


class TestSdkClient:
    """Tests using the official a2a-sdk Client to prove end-to-end protocol compliance."""

    @pytest.mark.asyncio
    async def test_sdk_get_card(self, sdk_client: Client) -> None:
        card = await sdk_client.get_card()
        assert card.name.startswith("Family Assistant")
        assert len(card.skills) >= 1

    @pytest.mark.asyncio
    async def test_sdk_send_message(
        self,
        sdk_client: Client,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(
            content="Hello from SDK client!"
        )

        msg = _sdk_message("Hello")
        task = None
        async for item in sdk_client.send_message(msg):
            if isinstance(item, tuple):
                task = item[0]

        assert task is not None, "Expected a Task from send_message"
        assert task.status.state.value == "completed"
        assert task.artifacts is not None
        assert len(task.artifacts) >= 1
        first_part = task.artifacts[0].parts[0].root
        assert isinstance(first_part, SdkTextPart)
        assert "Hello from SDK client!" in first_part.text

    @pytest.mark.asyncio
    async def test_sdk_send_message_with_task_id(
        self,
        sdk_client: Client,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="OK")

        custom_task_id = f"sdk-task-{uuid.uuid4().hex[:8]}"
        custom_context_id = f"sdk-ctx-{uuid.uuid4().hex[:8]}"
        msg = _sdk_message("test", task_id=custom_task_id, context_id=custom_context_id)

        task = None
        async for item in sdk_client.send_message(msg):
            if isinstance(item, tuple):
                task = item[0]

        assert task is not None
        assert task.id == custom_task_id
        assert task.context_id == custom_context_id

    @pytest.mark.asyncio
    async def test_sdk_get_task(
        self,
        sdk_client: Client,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="reply")

        task_id = f"sdk-get-{uuid.uuid4().hex[:8]}"
        msg = _sdk_message("hello", task_id=task_id)

        async for _ in sdk_client.send_message(msg):
            pass

        retrieved = await sdk_client.get_task(TaskQueryParams(id=task_id))
        assert retrieved.id == task_id
        assert retrieved.status.state.value == "completed"

    @pytest.mark.asyncio
    async def test_sdk_cancel_completed_task(
        self,
        sdk_client: Client,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="done")

        task_id = f"sdk-cancel-{uuid.uuid4().hex[:8]}"
        msg = _sdk_message("hello", task_id=task_id)

        async for _ in sdk_client.send_message(msg):
            pass

        with pytest.raises(A2AClientJSONRPCError):
            await sdk_client.cancel_task(TaskIdParams(id=task_id))

    @pytest.mark.asyncio
    async def test_sdk_streaming(
        self,
        sdk_client: Client,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="Streamed via SDK")

        msg = _sdk_message("Hello stream")
        events_received: list[tuple] = []
        final_task = None

        async for item in sdk_client.send_message(msg):
            if isinstance(item, tuple):
                final_task = item[0]
                events_received.append(item)

        assert len(events_received) >= 1, "Expected at least one event"
        assert final_task is not None
        assert final_task.status.state.value == "completed"
        assert final_task.artifacts is not None
        assert len(final_task.artifacts) >= 1
