"""Tests for the A2A (Agent-to-Agent) protocol API endpoints."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
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


class TestAgentCard:
    @pytest.mark.asyncio
    async def test_agent_card_returns_valid_card(self, a2a_client: AsyncClient) -> None:
        resp = await a2a_client.get("/.well-known/agent.json")
        assert resp.status_code == 200
        card = resp.json()
        assert card["name"].startswith("Family Assistant")
        assert card["url"] == "http://testserver/api/a2a"
        assert card["version"] == "0.3.0"
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
            params={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Hello"}],
                }
            },
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
                "message": {
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": "Look at this"},
                        {
                            "type": "file",
                            "file": {"uri": "https://example.com/report.pdf"},
                        },
                    ],
                }
            },
        )
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        # The LLM must have received the file URL somewhere in its input.
        # If the converter uses attachment_content (which triggers a DB lookup
        # that fails for external URLs), the file gets silently dropped.
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
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "test"}],
                    "taskId": "my-custom-task-123",
                    "contextId": "my-ctx-456",
                }
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
            params={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                    "taskId": "get-test-task",
                }
            },
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
            params={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                    "taskId": "cancel-test-task",
                }
            },
        )
        await a2a_client.post("/api/a2a", json=send_body)

        cancel_body = _jsonrpc("tasks/cancel", params={"id": "cancel-test-task"})
        resp = await a2a_client.post("/api/a2a", json=cancel_body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32002  # TASK_NOT_CANCELABLE

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, a2a_client: AsyncClient) -> None:
        body = _jsonrpc("tasks/cancel", params={"id": "no-such-task"})
        resp = await a2a_client.post("/api/a2a", json=body)
        assert resp.status_code == 200

        data = resp.json()
        assert data["error"] is not None
        assert data["error"]["code"] == -32001  # TASK_NOT_FOUND


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
            params={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Hello stream"}],
                }
            },
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
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Hello persist"}],
                    "taskId": "stream-persist-test",
                }
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
