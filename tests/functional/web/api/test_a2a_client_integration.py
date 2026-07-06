"""Integration tests for A2A client remote delegation.

Tests the full round-trip: A2AClientWrapper -> FA A2A server -> ProcessingService.
Uses the project's own A2A server as the remote endpoint via ASGITransport
(no real HTTP, everything in-process).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from family_assistant.a2a.client import (
    A2AClientError,
    A2AClientWrapper,
    A2ATaskNotFoundError,
)
from family_assistant.a2a.remote_service import RemoteA2AService
from family_assistant.a2a.result_converter import a2a_task_to_chat_result
from family_assistant.a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.content_parts import ContentPartDict, text_content
from family_assistant.processing import PENDING, ChatInteractionResult
from family_assistant.processing.types import RemoteServiceConfig
from family_assistant.security.taint import (
    A2A_TAINT_METADATA_KEY,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.web.routers.a2a_api import (
    _initial_taint_sources_from_message,  # noqa: PLC2701 - regression test covers server-side metadata restore
)
from tests.mocks.mock_llm import LLMOutput as MockLLMOutput

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

    from family_assistant.processing import ProcessingService
    from family_assistant.storage.context import DatabaseContext
    from tests.mocks.mock_llm import RuleBasedMockLLMClient


@pytest_asyncio.fixture
async def a2a_test_server(
    app_fixture: FastAPI,
    api_test_processing_service: ProcessingService,
) -> AsyncGenerator[AsyncClient]:
    """HTTPX client pointing at the FA A2A server for integration tests."""
    profile_id = api_test_processing_service.service_config.id
    app_fixture.state.processing_services = {
        profile_id: api_test_processing_service,
    }
    app_fixture.state.a2a_cancel_events = {}
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def a2a_client_wrapper(
    a2a_test_server: AsyncClient,
) -> AsyncGenerator[A2AClientWrapper]:
    """A2AClientWrapper pointed at the in-process FA A2A server.

    Injects the test httpx client to bypass real HTTP.
    """
    wrapper = A2AClientWrapper(agent_url="http://testserver")
    # Inject the test client so requests go through ASGITransport
    wrapper._httpx_client = a2a_test_server
    yield wrapper
    # Don't close — the a2a_test_server fixture owns the client lifecycle


@pytest_asyncio.fixture
async def remote_service(
    a2a_client_wrapper: A2AClientWrapper,
) -> RemoteA2AService:
    """RemoteA2AService backed by the in-process FA A2A server."""
    config = RemoteServiceConfig(
        id="remote_test_profile",
        description="Test remote A2A profile",
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
    )
    return RemoteA2AService(service_config=config, client=a2a_client_wrapper)


class _CapturingA2AClient:
    def __init__(self) -> None:
        self.captured_metadata: dict[str, object] | None = None

    async def send_message(
        self,
        content_parts: list[ContentPartDict],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Task:
        _ = content_parts
        _ = task_id
        self.captured_metadata = metadata
        return Task(
            id="captured-task",
            context_id=context_id or "captured-context",
            status=TaskStatus(state=TaskState.completed),
            artifacts=[
                Artifact(
                    artifact_id="captured-artifact",
                    parts=[Part(root=TextPart(text="remote ok"))],
                )
            ],
        )


@pytest.mark.asyncio
async def test_remote_a2a_preserves_runtime_taint_metadata(
    api_db_context: DatabaseContext,
) -> None:
    source = TaintSource(
        source_type=TaintSourceType.EMAIL,
        source_id="message-123",
        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
        labels=frozenset({"source_unknown_external"}),
        reason="external email",
    )
    client = _CapturingA2AClient()
    service = RemoteA2AService(
        service_config=RemoteServiceConfig(
            id="remote_test_profile",
            description="Test remote A2A profile",
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        client=cast("A2AClientWrapper", client),
    )

    result = await service.handle_chat_interaction(
        db_context=api_db_context,
        interface_type="test",
        conversation_id="tainted-conversation",
        trigger_content_parts=[text_content("Summarize this email")],
        trigger_interface_message_id=None,
        user_name="test_user",
        initial_taint_sources=[source],
    )

    assert not result.has_error
    assert client.captured_metadata is not None
    raw_taint = client.captured_metadata[A2A_TAINT_METADATA_KEY]
    assert isinstance(raw_taint, dict)
    assert TurnTaintState.from_metadata(raw_taint).sources == (source,)

    message = Message(
        role=Role.user,
        parts=[Part(root=TextPart(text="Summarize this email"))],
        message_id="message-123",
        metadata=client.captured_metadata,
    )
    assert _initial_taint_sources_from_message(message) == (source,)


class TestA2AClientIntegration:
    """Test A2AClientWrapper against the real FA A2A server."""

    @pytest.mark.asyncio
    async def test_discover_agent_card(
        self, a2a_client_wrapper: A2AClientWrapper
    ) -> None:
        """Client discovers the agent card from the A2A server."""
        card = await a2a_client_wrapper.discover()
        assert card.name.startswith("Family Assistant")
        assert card.url == "http://testserver/api/a2a"

    @pytest.mark.asyncio
    async def test_send_message_round_trip(
        self,
        a2a_client_wrapper: A2AClientWrapper,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """Send a message through the client and get a completed task back."""
        api_mock_llm_client.default_response = MockLLMOutput(
            content="Hello from the remote agent!"
        )

        task = await a2a_client_wrapper.send_message(
            [text_content("What is 2+2?")],
            context_id="integration-test-ctx",
        )

        assert task.status.state.value == "completed"
        assert task.artifacts is not None
        assert len(task.artifacts) >= 1

    @pytest.mark.asyncio
    async def test_send_message_and_convert_result(
        self,
        a2a_client_wrapper: A2AClientWrapper,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """Full pipeline: send message, convert to ChatInteractionResult."""
        api_mock_llm_client.default_response = MockLLMOutput(
            content="The answer is 42."
        )

        task = await a2a_client_wrapper.send_message([
            text_content("What is the meaning of life?")
        ])
        result = a2a_task_to_chat_result(task)

        assert not result.has_error
        assert "42" in result.text_reply

    @pytest.mark.asyncio
    async def test_context_id_isolation(
        self,
        a2a_client_wrapper: A2AClientWrapper,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        """Different context IDs create separate conversations."""
        api_mock_llm_client.default_response = MockLLMOutput(content="Response")

        task1 = await a2a_client_wrapper.send_message(
            [text_content("Hello 1")], context_id="ctx-alpha"
        )
        task2 = await a2a_client_wrapper.send_message(
            [text_content("Hello 2")], context_id="ctx-beta"
        )

        assert task1.context_id == "ctx-alpha"
        assert task2.context_id == "ctx-beta"
        assert task1.id != task2.id


class TestA2AClientAsyncMethods:
    """Client submit/get_task/cancel_task against the real FA A2A server."""

    @pytest.mark.asyncio
    async def test_submit_returns_working_then_get_task_completes(
        self,
        a2a_client_wrapper: A2AClientWrapper,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="async done")

        task = await a2a_client_wrapper.submit(
            [text_content("do it")], context_id="async-ctx"
        )
        # The async server returns a non-terminal task without blocking.
        assert task.status.state.value == "working"
        assert task.context_id == "async-ctx"

        background = app_fixture.state.a2a_background_tasks.get(task.id)
        if background is not None:
            await background

        polled = await a2a_client_wrapper.get_task(task.id)
        assert polled.status.state.value == "completed"
        result = a2a_task_to_chat_result(polled)
        assert "async done" in result.text_reply

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_cancel_task_cancels_in_flight(
        self,
        a2a_client_wrapper: A2AClientWrapper,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        # Postgres-only for the same reason as the server-side cancel test:
        # hard-cancelling the in-flight task tears down its DB connection, which
        # the production pool isolates but SQLite's shared StaticPool cannot.
        release = asyncio.Event()
        api_mock_llm_client.response_gate = release

        task = await a2a_client_wrapper.submit([text_content("slow")])
        assert task.status.state.value == "working"

        canceled = await a2a_client_wrapper.cancel_task(task.id)
        assert canceled.status.state.value == "canceled"

        background = app_fixture.state.a2a_background_tasks.get(task.id)
        if background is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await background
        release.set()

        polled = await a2a_client_wrapper.get_task(task.id)
        assert polled.status.state.value == "canceled"


class TestRemoteA2AServiceIntegration:
    """Test RemoteA2AService handle_chat_interaction against the real A2A server."""

    @pytest.mark.asyncio
    async def test_handle_chat_interaction_success(
        self,
        remote_service: RemoteA2AService,
        api_mock_llm_client: RuleBasedMockLLMClient,
        api_db_context: DatabaseContext,
    ) -> None:
        """RemoteA2AService produces a successful ChatInteractionResult."""
        api_mock_llm_client.default_response = MockLLMOutput(
            content="Remote delegation worked!"
        )

        result = await remote_service.handle_chat_interaction(
            db_context=api_db_context,
            interface_type="test",
            conversation_id="test-conv-1",
            trigger_content_parts=[text_content("Please help me")],
            trigger_interface_message_id=None,
            user_name="test_user",
            subconversation_id=str(uuid.uuid4()),
        )

        assert not result.has_error
        assert "Remote delegation worked!" in result.text_reply

    @pytest.mark.asyncio
    async def test_handle_chat_interaction_with_subconversation_id(
        self,
        remote_service: RemoteA2AService,
        api_mock_llm_client: RuleBasedMockLLMClient,
        api_db_context: DatabaseContext,
    ) -> None:
        """Subconversation ID is used in the A2A context_id for isolation."""
        api_mock_llm_client.default_response = MockLLMOutput(content="OK")

        sub_id = str(uuid.uuid4())
        result = await remote_service.handle_chat_interaction(
            db_context=api_db_context,
            interface_type="test",
            conversation_id="conv-123",
            trigger_content_parts=[text_content("Test isolation")],
            trigger_interface_message_id=None,
            user_name="test_user",
            subconversation_id=sub_id,
        )

        assert not result.has_error

    @pytest.mark.asyncio
    async def test_handle_chat_interaction_without_subconversation(
        self,
        remote_service: RemoteA2AService,
        api_mock_llm_client: RuleBasedMockLLMClient,
        api_db_context: DatabaseContext,
    ) -> None:
        """When no subconversation_id, conversation_id is used for context."""
        api_mock_llm_client.default_response = MockLLMOutput(content="No sub")

        result = await remote_service.handle_chat_interaction(
            db_context=api_db_context,
            interface_type="test",
            conversation_id="direct-conv-456",
            trigger_content_parts=[text_content("Direct call")],
            trigger_interface_message_id=None,
            user_name="test_user",
        )

        assert not result.has_error
        assert "No sub" in result.text_reply

    @pytest.mark.asyncio
    async def test_remote_service_kind_is_remote(
        self, remote_service: RemoteA2AService
    ) -> None:
        """RemoteA2AService.kind is 'remote'."""
        assert remote_service.kind == "remote"

    @pytest.mark.asyncio
    async def test_remote_service_config_accessible(
        self, remote_service: RemoteA2AService
    ) -> None:
        """RemoteA2AService exposes its config."""
        config = remote_service.service_config
        assert config.id == "remote_test_profile"
        assert config.delegation_security_level == DelegationSecurityLevel.UNRESTRICTED


class TestRemoteA2AServiceAsync:
    """RemoteA2AService submit_async/poll_async/cancel_async against the server."""

    @pytest.mark.asyncio
    async def test_submit_async_and_poll_to_completion(
        self,
        remote_service: RemoteA2AService,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        api_mock_llm_client.default_response = MockLLMOutput(content="remote async")

        submission = await remote_service.submit_async(
            [text_content("go")],
            conversation_id="conv-async",
            subconversation_id="sub-async",
        )
        # The client sends no task id (A2A §3.4.2); the server assigns one.
        assert submission.remote_task_id
        # The async server returns a non-terminal task, so no inline result yet.
        assert submission.terminal_result is None

        background = app_fixture.state.a2a_background_tasks.get(
            submission.remote_task_id
        )
        if background is not None:
            await background

        result = await remote_service.poll_async(
            submission.remote_task_id, submission.remote_context_id
        )
        assert isinstance(result, ChatInteractionResult)
        assert not result.has_error
        assert "remote async" in result.text_reply

    @pytest.mark.asyncio
    async def test_poll_async_pending_while_in_flight(
        self,
        remote_service: RemoteA2AService,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        # Gate the LLM so the remote task stays non-terminal across the poll.
        # No cancellation here, so this is safe on SQLite (the parked background
        # task does not hold the shared connection).
        release = asyncio.Event()
        api_mock_llm_client.response_gate = release

        submission = await remote_service.submit_async(
            [text_content("slow")],
            conversation_id="conv-pending",
            subconversation_id=None,
        )
        pending = await remote_service.poll_async(
            submission.remote_task_id, submission.remote_context_id
        )
        assert pending is PENDING

        release.set()
        background = app_fixture.state.a2a_background_tasks.get(
            submission.remote_task_id
        )
        if background is not None:
            await background

        result = await remote_service.poll_async(
            submission.remote_task_id, submission.remote_context_id
        )
        assert isinstance(result, ChatInteractionResult)

    @pytest.mark.asyncio
    async def test_poll_async_unknown_task_raises_not_found(
        self,
        remote_service: RemoteA2AService,
    ) -> None:
        # A real tasks/get for an id the server never created returns the A2A
        # task-not-found error, which the client surfaces as A2ATaskNotFoundError
        # — the worker's cue to re-submit rather than fail the delegation.
        with pytest.raises(A2ATaskNotFoundError):
            await remote_service.poll_async("a2a-never-created", None)

    @pytest.mark.postgres
    @pytest.mark.asyncio
    async def test_cancel_async_cancels_remote(
        self,
        remote_service: RemoteA2AService,
        app_fixture: FastAPI,
        api_mock_llm_client: RuleBasedMockLLMClient,
    ) -> None:
        # Postgres-only: cancellation tears down the background task's DB
        # connection (see the server-side cancel test).
        release = asyncio.Event()
        api_mock_llm_client.response_gate = release

        submission = await remote_service.submit_async(
            [text_content("slow")],
            conversation_id="conv-cancel",
            subconversation_id=None,
        )
        await remote_service.cancel_async(submission.remote_task_id)

        background = app_fixture.state.a2a_background_tasks.get(
            submission.remote_task_id
        )
        if background is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await background
        release.set()

        result = await remote_service.poll_async(
            submission.remote_task_id, submission.remote_context_id
        )
        assert isinstance(result, ChatInteractionResult)
        assert result.has_error


class TestA2AClientConnectionErrors:
    """Test error handling when the remote agent is unreachable."""

    @pytest.mark.asyncio
    async def test_connection_refused(self) -> None:
        """Client raises A2AClientError when agent is unreachable."""
        wrapper = A2AClientWrapper(agent_url="http://localhost:1", timeout=1.0)
        with pytest.raises(A2AClientError):
            await wrapper.discover()
        await wrapper.close()

    @pytest.mark.asyncio
    async def test_remote_service_handles_connection_error(
        self,
    ) -> None:
        """RemoteA2AService returns error result on connection failure."""
        wrapper = A2AClientWrapper(agent_url="http://localhost:1", timeout=1.0)
        config = RemoteServiceConfig(
            id="unreachable_agent",
            description="Should fail",
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        )
        service = RemoteA2AService(service_config=config, client=wrapper)

        mock_db = AsyncMock()
        result = await service.handle_chat_interaction(
            db_context=mock_db,
            interface_type="test",
            conversation_id="err-conv",
            trigger_content_parts=[text_content("Hello")],
            trigger_interface_message_id=None,
            user_name="test_user",
        )

        assert result.has_error
        assert "unreachable_agent" in result.text_reply
        await wrapper.close()
