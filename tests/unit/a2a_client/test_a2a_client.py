"""Unit tests for A2A client wrapper, auth config, and result converter."""

from __future__ import annotations

import json
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, cast

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
    A2APermanentError,
)
from family_assistant.a2a.result_converter import a2a_task_to_chat_result
from family_assistant.llm.content_parts import (
    ContentPartDict,
    image_url_content,
    text_content,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def _make_streaming_agent_card(url: str) -> dict:
    card = _make_agent_card(url)
    card["capabilities"]["streaming"] = True
    card["preferredTransport"] = "JSONRPC"
    return card


def _content_parts(text: str) -> list[ContentPartDict]:
    return cast("list[ContentPartDict]", [text_content(text)])


def _text_part_text(part: Part) -> str:
    root = part.root
    assert isinstance(root, TextPart)
    return root.text


class FakeA2AAgentHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "completed"
    task_id: ClassVar[str] = "task-test"
    task_state: ClassVar[str] = "completed"
    task_message: ClassVar[str] = "Remote task failed"
    text: ClassVar[str] = "Hello from remote"
    streaming: ClassVar[bool] = False
    poll_count: ClassVar[int] = 0
    agent_url: ClassVar[str] = ""

    def log_message(self, _fmt: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/.well-known/agent-card.json":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_json(
            _make_streaming_agent_card(self.rpc_url())
            if self.streaming
            else _make_agent_card(self.rpc_url())
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        method = body.get("method")
        if method == "message/send":
            self.handle_message_send(body)
            return
        if method == "message/stream":
            self.handle_message_stream(body)
            return
        if method == "tasks/get":
            self.handle_tasks_get(body)
            return
        self.send_json(self.jsonrpc_error(body.get("id"), -32601, "Method not found"))

    def handle_message_send(self, body: dict) -> None:
        if self.mode == "jsonrpc_error":
            self.send_json(
                self.jsonrpc_error(body.get("id"), -32600, "Invalid request")
            )
            return
        if self.mode == "message":
            self.send_json({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": self.message(),
            })
            return
        if self.mode == "poll":
            self.send_json({
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": self.task("working", artifacts=False),
            })
            return
        self.send_json({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": self.task(
                self.task_state, artifacts=self.task_state == "completed"
            ),
        })

    def handle_message_stream(self, body: dict) -> None:
        if self.mode == "jsonrpc_error":
            self.send_sse(
                body.get("id"),
                [self.jsonrpc_error(body.get("id"), -32600, "Invalid request")],
            )
            return
        if self.mode == "message":
            self.send_sse(
                body.get("id"),
                [{"jsonrpc": "2.0", "id": body.get("id"), "result": self.message()}],
            )
            return
        if self.mode == "stream":
            self.send_sse(
                body.get("id"),
                [
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": self.status_event("working", final=False),
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": self.artifact_event(),
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": self.status_event("completed", final=True),
                    },
                ],
            )
            return
        self.send_sse(
            body.get("id"),
            [
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": self.status_event(self.task_state, final=True),
                }
            ],
        )

    def handle_tasks_get(self, body: dict) -> None:
        type(self).poll_count += 1
        self.send_json({
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": self.task("completed", artifacts=True),
        })

    def send_json(self, value: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_sse(self, _request_id: object, events: list[dict]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

    def rpc_url(self) -> str:
        server = cast("ThreadingHTTPServer", self.server)
        return f"http://127.0.0.1:{server.server_port}/a2a"

    def task(self, state: str, *, artifacts: bool) -> dict:
        task: dict = {
            "id": self.task_id,
            "contextId": "test-ctx",
            "status": {"state": state},
        }
        if state != "completed":
            task["status"]["message"] = self.status_message(self.task_message)
        if artifacts:
            task["artifacts"] = [self.artifact()]
        return task

    def message(self) -> dict:
        return {
            "kind": "message",
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "contextId": "test-ctx",
            "parts": [{"kind": "text", "text": self.text}],
        }

    def status_event(self, state: str, *, final: bool) -> dict:
        return {
            "kind": "status-update",
            "taskId": self.task_id,
            "contextId": "test-ctx",
            "status": {
                "state": state,
                "message": self.status_message(self.task_message),
            },
            "final": final,
        }

    def artifact_event(self) -> dict:
        return {
            "kind": "artifact-update",
            "taskId": self.task_id,
            "contextId": "test-ctx",
            "artifact": self.artifact(),
            "append": False,
            "lastChunk": True,
        }

    def artifact(self) -> dict:
        return {
            "artifactId": "artifact-1",
            "parts": [{"kind": "text", "text": self.text}],
        }

    @staticmethod
    def status_message(text: str) -> dict:
        return {
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        }

    @staticmethod
    def jsonrpc_error(request_id: object, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


@pytest.fixture
def fake_a2a_agent() -> Iterator[type[FakeA2AAgentHandler]]:
    FakeA2AAgentHandler.mode = "completed"
    FakeA2AAgentHandler.task_id = f"task-{uuid.uuid4()}"
    FakeA2AAgentHandler.task_state = "completed"
    FakeA2AAgentHandler.task_message = "Remote task failed"
    FakeA2AAgentHandler.text = "Hello from remote"
    FakeA2AAgentHandler.streaming = False
    FakeA2AAgentHandler.poll_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeA2AAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeA2AAgentHandler.agent_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield FakeA2AAgentHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


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

    @pytest.mark.asyncio
    async def test_bad_auth_config_raises_permanent_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A local misconfiguration (bearer auth whose token_env is unset) must
        # surface as A2APermanentError so the delegation worker fails fast with
        # the real auth error instead of polling until the wall-clock cap.
        monkeypatch.delenv("MISSING_TOKEN", raising=False)
        wrapper = A2AClientWrapper(
            agent_url="http://agent.test",
            auth_config=A2AAuthConfig(type="bearer", token_env="MISSING_TOKEN"),
        )
        with pytest.raises(A2APermanentError, match="Auth configuration error"):
            await wrapper.get_task("a2a-task-1")


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
    async def test_discover_4xx_raises_permanent(self, httpx_mock: HTTPXMock) -> None:
        # A 4xx agent-card fetch (bad agent-card URL / bad auth) is deterministic:
        # the worker must fail fast, not poll until the cap.
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            status_code=403,
        )
        with pytest.raises(A2APermanentError):
            await wrapper.discover()
        await wrapper.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [503, 408, 429])
    async def test_discover_transient_statuses_are_retryable(
        self, httpx_mock: HTTPXMock, status_code: int
    ) -> None:
        # A 5xx and the retryable 4xx (408 timeout, 429 rate limited) agent-card
        # fetches are transient: a plain (retryable) A2AClientError, not the
        # permanent subclass.
        wrapper = A2AClientWrapper(agent_url="http://agent.test")
        httpx_mock.add_response(
            url="http://agent.test/.well-known/agent-card.json",
            status_code=status_code,
        )
        with pytest.raises(A2AClientError) as exc_info:
            await wrapper.discover()
        assert not isinstance(exc_info.value, A2APermanentError)
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_success(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Successful message/send returns a completed Task."""
        fake_a2a_agent.mode = "completed"
        fake_a2a_agent.text = "Hello back!"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.completed
        assert task.artifacts is not None
        assert _text_part_text(task.artifacts[0].parts[0]) == "Hello back!"
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_polling_until_completed(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Non-terminal message/send tasks are polled with tasks/get."""
        fake_a2a_agent.mode = "poll"
        fake_a2a_agent.text = "Finished later"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url, timeout=5)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.completed
        assert task.artifacts is not None
        assert _text_part_text(task.artifacts[0].parts[0]) == "Finished later"
        assert fake_a2a_agent.poll_count == 1
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_direct_message_response(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Agents may answer with a Message instead of a Task."""
        fake_a2a_agent.mode = "message"
        fake_a2a_agent.text = "Direct answer"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.completed
        assert task.history is not None
        assert _text_part_text(task.history[-1].parts[0]) == "Direct answer"
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_streaming_until_completed(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Streaming agents are consumed until a terminal status event."""
        fake_a2a_agent.streaming = True
        fake_a2a_agent.mode = "stream"
        fake_a2a_agent.text = "Streamed answer"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.completed
        assert task.artifacts is not None
        assert _text_part_text(task.artifacts[0].parts[0]) == "Streamed answer"
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_jsonrpc_error(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """JSON-RPC error raises A2AClientError."""
        fake_a2a_agent.mode = "jsonrpc_error"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        with pytest.raises(A2AClientError, match="JSON-RPC error -32600"):
            await wrapper.send_message(_content_parts("Hello"))
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_send_message_failed_task_returns_task(
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Failed task state is returned (caller handles via result_converter)."""
        fake_a2a_agent.task_state = "failed"
        fake_a2a_agent.task_message = "LLM error"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == TaskState.failed
        assert task.status.message is not None
        assert _text_part_text(task.status.message.parts[0]) == "LLM error"
        await wrapper.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("canceled", TaskState.canceled),
            ("rejected", TaskState.rejected),
            ("auth-required", TaskState.auth_required),
            ("input-required", TaskState.input_required),
        ],
    )
    async def test_send_message_terminal_error_states_return_task(
        self,
        fake_a2a_agent: type[FakeA2AAgentHandler],
        state: str,
        expected: TaskState,
    ) -> None:
        """Terminal non-success states are returned for result conversion."""
        fake_a2a_agent.task_state = state
        fake_a2a_agent.task_message = "Terminal state message"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(_content_parts("Hello"))
        assert task.status.state == expected
        assert task.status.message is not None
        assert _text_part_text(task.status.message.parts[0]) == "Terminal state message"
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
        # Permanent: an oversized attachment is a deterministic local input
        # error, so the worker must fail fast rather than poll until the cap.
        with pytest.raises(A2APermanentError, match="exceeds limit"):
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
        self, fake_a2a_agent: type[FakeA2AAgentHandler]
    ) -> None:
        """Context ID and task ID are passed through in the message."""
        fake_a2a_agent.text = "OK"
        wrapper = A2AClientWrapper(agent_url=fake_a2a_agent.agent_url)

        task = await wrapper.send_message(
            _content_parts("Hello"),
            context_id="ctx-123",
            task_id="task-456",
        )
        assert task.status.state == TaskState.completed
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

    def test_auth_required_task(self) -> None:
        """Auth-required task produces error result."""
        task = _make_task(state=TaskState.auth_required, message="Login required")
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "declined" in result.text_reply
        assert "Login required" in result.text_reply

    def test_non_terminal_task(self) -> None:
        """A non-terminal task is not treated as success."""
        task = _make_task(state=TaskState.working, message="Still running")
        result = a2a_task_to_chat_result(task)
        assert result.has_error
        assert "unexpected state" in result.text_reply
