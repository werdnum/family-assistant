"""Unit tests for A2A client wrapper, auth config, and result converter."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

from family_assistant.a2a.auth import A2AAuthConfig
from family_assistant.a2a.client import (
    MAX_INLINE_ATTACHMENT_BYTES,
    A2AClientError,
    A2AClientWrapper,
)
from family_assistant.a2a.result_converter import a2a_task_to_chat_result
from family_assistant.llm.content_parts import (
    ContentPartDict,
    image_url_content,
    text_content,
)

if TYPE_CHECKING:
    from pytest_httpx import HTTPXMock


def _make_agent_card(url: str = "http://agent.test/api/a2a") -> dict:
    """Build an agent card JSON dict."""
    return {
        "name": "Test Agent",
        "description": "A test A2A agent",
        "url": url,
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
    }


def _make_task_response(
    text: str = "Hello from remote",
    state: str = "completed",
    task_id: str | None = None,
    message: str | None = None,
) -> dict:
    """Build a completed task JSON-RPC response."""
    tid = task_id or str(uuid.uuid4())
    artifact = {
        "artifactId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    result: dict = {
        "id": tid,
        "contextId": "test-ctx",
        "status": {"state": state},
        "artifacts": [artifact] if state == "completed" else [],
    }
    if message:
        result["status"]["message"] = {
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": message}],
        }
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": result,
    }


def _make_error_response(code: int = -32603, message: str = "Internal error") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "error": {"code": code, "message": message},
    }


def _content_parts(text: str) -> list[ContentPartDict]:
    return cast("list[ContentPartDict]", [text_content(text)])


# --- Auth tests ---


class TestA2AAuthConfig:
    def test_none_auth_returns_none(self) -> None:
        config = A2AAuthConfig(type="none")
        assert config.to_httpx_auth() is None

    def test_bearer_auth_requires_token_env(self) -> None:
        config = A2AAuthConfig(type="bearer")
        with pytest.raises(ValueError, match="requires token_env"):
            config.to_httpx_auth()

    def test_bearer_auth_missing_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_TOKEN", raising=False)
        config = A2AAuthConfig(type="bearer", token_env="MY_TOKEN")
        with pytest.raises(ValueError, match="not set or empty"):
            config.to_httpx_auth()

    def test_bearer_auth_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret123")
        config = A2AAuthConfig(type="bearer", token_env="MY_TOKEN")
        auth = config.to_httpx_auth()
        assert auth is not None

    def test_api_key_auth_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "key123")
        config = A2AAuthConfig(
            type="api_key", token_env="API_KEY", header_name="X-API-Key"
        )
        auth = config.to_httpx_auth()
        assert auth is not None

    def test_validate_env_vars_no_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret")
        config = A2AAuthConfig(type="bearer", token_env="MY_TOKEN")
        assert config.validate_env_vars() == []

    def test_validate_env_vars_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config = A2AAuthConfig(type="bearer", token_env="MISSING_VAR")
        errors = config.validate_env_vars()
        assert len(errors) == 1
        assert "MISSING_VAR" in errors[0]

    def test_validate_env_vars_none_type(self) -> None:
        config = A2AAuthConfig(type="none")
        assert config.validate_env_vars() == []


# --- Client wrapper tests ---


class TestA2AClientWrapper:
    @pytest.mark.asyncio
    async def test_discover_fetches_agent_card(self, httpx_mock: HTTPXMock) -> None:
        """Agent card is fetched and cached."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )

        card = await wrapper.discover()
        assert card.name == "Test Agent"

        # Second call should use cache (no additional HTTP call)
        card2 = await wrapper.discover()
        assert card2.name == "Test Agent"
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_success(self, httpx_mock: HTTPXMock) -> None:
        """Successful message/send returns a Task."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_response(
            url="http://agent.test/api/a2a",
            json=_make_task_response("Hello back!"),
        )

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.completed
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_jsonrpc_error(self, httpx_mock: HTTPXMock) -> None:
        """JSON-RPC error raises A2AClientError."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_response(
            url="http://agent.test/api/a2a",
            json=_make_error_response(-32600, "Invalid request"),
        )

        with pytest.raises(A2AClientError, match="JSON-RPC error -32600"):
            await wrapper.send_message(_content_parts("Hello"))
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_failed_task_returns_task(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """Failed task state is returned (caller handles via result_converter)."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_response(
            url="http://agent.test/api/a2a",
            json=_make_task_response(
                state="failed", message="LLM error", text="ignored"
            ),
        )

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.failed
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_http_error(self, httpx_mock: HTTPXMock) -> None:
        """HTTP error raises A2AClientError."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_response(
            url="http://agent.test/api/a2a",
            status_code=500,
        )

        with pytest.raises(A2AClientError, match="HTTP 500"):
            await wrapper.send_message(_content_parts("Hello"))
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_timeout(self, httpx_mock: HTTPXMock) -> None:
        """Timeout raises A2AClientError."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test", timeout=0.01)
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_exception(
            httpx.ReadTimeout("timed out"),
            url="http://agent.test/api/a2a",
        )

        with pytest.raises(A2AClientError, match="Timeout"):
            await wrapper.send_message(_content_parts("Hello"))
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_connection_refused(self, httpx_mock: HTTPXMock) -> None:
        """Connection error raises A2AClientError."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            url="http://agent.test/api/a2a",
        )

        with pytest.raises(A2AClientError, match="Cannot connect"):
            await wrapper.send_message(_content_parts("Hello"))
        await wrapper.close()

    def test_attachment_size_validation_passes(self) -> None:
        """Small inline attachments pass validation."""
        parts = _content_parts("hello")
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        wrapper._convert_and_validate_parts(parts)

    def test_attachment_size_validation_rejects_large(self) -> None:
        """Inline attachments exceeding limit are rejected."""
        large_data = "x" * (MAX_INLINE_ATTACHMENT_BYTES + 1000)
        parts = cast(
            "list[ContentPartDict]",
            [image_url_content(f"data:image/png;base64,{large_data}")],
        )
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        with pytest.raises(A2AClientError, match="exceeds limit"):
            wrapper._convert_and_validate_parts(parts)

    @pytest.mark.asyncio
    async def test_close_clears_state(self, httpx_mock: HTTPXMock) -> None:
        """Close clears the cached agent card and HTTP client."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )

        await wrapper.discover()
        assert wrapper._agent_card is not None

        await wrapper.close()
        assert wrapper._agent_card is None
        assert wrapper._httpx_client is None

    @pytest.mark.asyncio
    async def test_send_message_with_context_and_task_id(
        self, httpx_mock: HTTPXMock
    ) -> None:
        """Context ID and task ID are passed through in the message."""
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            json=_make_agent_card(),
        )
        httpx_mock.add_response(
            url="http://agent.test/api/a2a",
            json=_make_task_response("OK"),
        )

        task = await wrapper.send_message(
            _content_parts("Hello"),
            context_id="ctx-123",
            task_id="task-456",
        )
        assert task.status.state == TaskState.completed

        # Verify the request contained our context_id and task_id
        request = httpx_mock.get_request(url="http://agent.test/api/a2a")
        assert request is not None
        body = request.read().decode()
        assert "ctx-123" in body
        assert "task-456" in body
        await wrapper.close()


# --- Result converter tests ---


def _status_message(text: str) -> Message:
    """Build a Message for use in TaskStatus.message."""
    return Message(
        role=Role.agent,
        parts=[Part(root=TextPart(text=text))],
        message_id=str(uuid.uuid4()),
    )


def _make_task(
    state: TaskState = TaskState.completed,
    artifacts: list[Artifact] | None = None,
    history: list[Message] | None = None,
    message: str | None = None,
) -> Task:
    """Build a Task object for testing."""
    status_msg = _status_message(message) if message else None
    return Task(
        id=str(uuid.uuid4()),
        context_id="test-ctx",
        status=TaskStatus(state=state, message=status_msg),
        artifacts=artifacts,
        history=history,
    )


class TestA2ATaskToChatResult:
    def test_completed_with_text_artifact(self) -> None:
        """Completed task with text artifact produces success result."""
        task = _make_task(
            artifacts=[
                Artifact(
                    artifact_id="a1",
                    parts=[Part(root=TextPart(text="Result text"))],
                )
            ]
        )
        result = a2a_task_to_chat_result(task)
        assert not result.has_error
        assert result.text_reply == "Result text"

    def test_completed_with_multiple_text_artifacts(self) -> None:
        """Multiple text artifacts are joined with double newline."""
        task = _make_task(
            artifacts=[
                Artifact(
                    artifact_id="a1",
                    parts=[Part(root=TextPart(text="Part 1"))],
                ),
                Artifact(
                    artifact_id="a2",
                    parts=[Part(root=TextPart(text="Part 2"))],
                ),
            ]
        )
        result = a2a_task_to_chat_result(task)
        assert not result.has_error
        assert result.text_reply == "Part 1\n\nPart 2"

    def test_completed_falls_back_to_agent_message(self) -> None:
        """When no artifacts, falls back to last agent message."""
        task = _make_task(
            artifacts=[],
            history=[
                Message(
                    role=Role.user,
                    parts=[Part(root=TextPart(text="Hello"))],
                    message_id="m1",
                ),
                Message(
                    role=Role.agent,
                    parts=[Part(root=TextPart(text="Agent reply"))],
                    message_id="m2",
                ),
            ],
        )
        result = a2a_task_to_chat_result(task)
        assert not result.has_error
        assert result.text_reply == "Agent reply"

    def test_completed_no_output_is_error(self) -> None:
        """Completed task with no artifacts or messages is an error."""
        task = _make_task(artifacts=[], history=[])
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "no output" in result.text_reply

    def test_failed_task(self) -> None:
        """Failed task state produces error result."""
        task = _make_task(state=TaskState.failed, message="LLM crashed")
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "LLM crashed" in result.text_reply

    def test_canceled_task(self) -> None:
        """Canceled task produces error result."""
        task = _make_task(state=TaskState.canceled)
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "cancelled" in result.text_reply

    def test_input_required_task(self) -> None:
        """Input required state produces error result."""
        task = _make_task(state=TaskState.input_required, message="Need more details")
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "more information" in result.text_reply

    def test_rejected_task(self) -> None:
        """Rejected task produces error result."""
        task = _make_task(state=TaskState.rejected, message="Not authorized")
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "declined" in result.text_reply
