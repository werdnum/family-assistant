import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, TypedDict, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import (
    CalendarContextProvider,
    ContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import MessageReasoningInfo
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.security.taint import TaintSource
from family_assistant.services.notifier import MESSAGE_CATEGORY, NotificationMetadata
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteModel, NoteReadPolicy
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS as local_tool_registrations,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
    ToolsProvider,
)
from family_assistant.tools.types import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.conversation_stream_hub import ConversationStreamHub
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.functional.web.conftest import run_chat_turn_stream
from tests.helpers import wait_for_condition
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig

logger = logging.getLogger(__name__)


class SSEEvent(TypedDict):
    type: str
    # ast-grep-ignore: no-dict-any - JSON-parsed SSE data is genuinely arbitrary
    data: dict[str, Any]


def parse_sse_events(response_text: str) -> list[SSEEvent]:
    """Parse SSE events from a streaming HTTP response body."""
    events: list[SSEEvent] = []
    current_event_type = None
    for line in response_text.split("\n"):
        if line.startswith("event:"):
            current_event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event_type:
            data_str = line.split(":", 1)[1].strip()
            if data_str:
                events.append(
                    SSEEvent(type=current_event_type, data=json.loads(data_str))
                )
                current_event_type = None
    return events


# --- Fixtures ---


@pytest_asyncio.fixture(scope="function")
async def db_context(
    db_engine: AsyncEngine,
) -> AsyncGenerator[Database]:
    """Provides a Database for a single test function."""
    ctx = Database(engine=db_engine)
    yield ctx


@pytest.fixture(scope="function")
def mock_processing_service_config() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for tests."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": "You are a test assistant. Server URL: {server_url}."
        },
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        id="chat_api_test_profile",
        # Matches default_assistant, the profile these tests stand in for. The
        # retryable-turn test needs it: the setup step it fails is the context
        # taint aggregation, which only runs for a profile granted the context.
        include_aggregated_context=True,
    )


@pytest.fixture(scope="function")
def mock_llm_client() -> RuleBasedMockLLMClient:
    """Provides a RuleBasedMockLLMClient for tests."""
    return RuleBasedMockLLMClient(rules=[])  # Rules will be set per-test


@pytest_asyncio.fixture(scope="function")
async def test_tools_provider(
    mock_processing_service_config: ProcessingServiceConfig,
) -> ToolsProvider:
    """
    Provides a policy-enforced ToolsProvider stack configured for testing.
    """
    local_provider = LocalToolsProvider(
        registrations=local_tool_registrations,
        embedding_generator=None,
    )
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )
    await policy_provider.get_tool_definitions()
    return policy_provider


def _build_processing_service(
    *,
    llm_client: LLMInterface,
    tools_provider: ToolsProvider,
    service_config: ProcessingServiceConfig,
    context_providers: list[ContextProvider],
) -> ProcessingService:
    return ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_engine: AsyncEngine,
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components."""

    def get_entered_db_context_for_provider() -> Database:
        """
        Returns an awaitable that resolves to an entered Database.
        This matches the expected type for NotesContextProvider's get_db_context_func.
        """
        new_ctx = Database(engine=db_engine)
        return new_ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config.prompts,
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
        timezone=mock_processing_service_config.timezone,
        prompts=mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=mock_processing_service_config.prompts
    )
    return _build_processing_service(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=[notes_provider, calendar_provider, known_users_provider],
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine,
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_llm_client: LLMInterface,
) -> FastAPI:
    """
    Creates a FastAPI application instance for testing, with a mock
    ProcessingService.
    """
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware,
    )
    app.include_router(actual_app.router)

    app.state.processing_service = test_processing_service
    app.state.tools_provider = test_tools_provider
    app.state.database_engine = db_engine
    app.state.config = AppConfig(
        database_url=str(db_engine.url),
    )
    app.state.llm_client = mock_llm_client
    app.state.debug_mode = False

    app.state.web_chat_interface = WebChatInterface(db_engine)

    temp_db_ctx = Database(engine=db_engine)
    await init_db(db_engine)
    await temp_db_ctx.init_vector_db()

    return app


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Provides an HTTPX AsyncClient for the test FastAPI app."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --- Test Cases ---


async def test_api_chat_send_message_stream_minimal(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """Test the streaming chat API endpoint with a minimal conversation."""
    # Arrange
    user_prompt = "Hello, can you help me?"
    llm_response = (
        "Of course! I'd be happy to help you. What do you need assistance with?"
    )

    # Configure mock LLM to respond to the prompt
    mock_llm_client.rules.append((
        lambda args: any(
            msg.role == "user" and user_prompt in str(msg.content or "")
            for msg in args.get("messages", [])
        ),
        LLMOutput(
            content=llm_response,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=50, completion_tokens=50, total_tokens=100
            ),
        ),
    ))

    # Act - Make streaming request
    response = await run_chat_turn_stream(
        test_client,
        {"prompt": user_prompt},
    )

    # Assert response basics
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    # Parse SSE events
    events = parse_sse_events(response.text)

    # Assert we got the expected events
    text_events = [e for e in events if e["type"] == "text"]
    turn_started_events = [e for e in events if e["type"] == "turn_started"]
    turn_ended_events = [e for e in events if e["type"] == "turn_ended"]

    # Should have text content
    assert len(text_events) > 0
    combined_text = "".join(e["data"]["content"] for e in text_events)
    assert combined_text == llm_response

    # Should bracket the turn with exactly one turn_started and one turn_ended
    assert len(turn_started_events) == 1
    assert len(turn_ended_events) == 1
    assert turn_ended_events[0]["data"]["status"] == "complete"

    # Check database state - messages should be saved
    # Need to extract conversation_id from the stream (it's generated)
    # Since we don't have it in the response, we'll check for any recent messages
    fresh_ctx = Database(engine=db_engine)
    recent_conversations = await fresh_ctx.message_history.get_all_grouped(
        interface_type="api"
    )

    # Should have at least one conversation
    assert len(recent_conversations) > 0

    # Get the most recent conversation
    latest_conversation_messages = list(recent_conversations.values())[-1]

    # Should have user and assistant messages
    user_messages = [m for m in latest_conversation_messages if m["role"] == "user"]
    assistant_messages = [
        m for m in latest_conversation_messages if m["role"] == "assistant"
    ]

    assert len(user_messages) >= 1
    assert len(assistant_messages) >= 1
    assert user_messages[-1]["content"] == user_prompt
    assert assistant_messages[-1]["content"] == llm_response


async def test_api_chat_send_message_stream_with_tools(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
) -> None:
    """Test the streaming chat API endpoint with tool calls."""
    # Arrange
    user_prompt = "Create a note titled 'Stream Test' with content 'Testing streaming'"
    note_title = "Stream Test"
    note_content = "Testing streaming"
    tool_call_id = "streaming_call_123"
    llm_final_reply = (
        "I've created the note 'Stream Test' with the content you requested."
    )

    # Configure mock LLM to call the tool on first request
    def first_llm_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return (
            len(messages) >= 1
            and any(
                msg.role == "user" and user_prompt in str(msg.content or "")
                for msg in messages
            )
            and not any(msg.role == "tool" for msg in messages)
        )

    mock_llm_client.rules.append((
        first_llm_call_matcher,
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments=json.dumps({
                            "title": note_title,
                            "content": note_content,
                        }),
                    ),
                )
            ],
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=25, completion_tokens=25, total_tokens=50
            ),
        ),
    ))

    # Configure mock LLM for second call (after tool execution)
    def second_llm_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id for msg in messages
        )

    mock_llm_client.rules.append((
        second_llm_call_matcher,
        LLMOutput(
            content=llm_final_reply,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=35, completion_tokens=40, total_tokens=75
            ),
        ),
    ))

    # Act - Make streaming request
    response = await run_chat_turn_stream(
        test_client,
        {"prompt": user_prompt},
    )

    # Assert response basics
    assert response.status_code == 200

    # Parse SSE events
    events = parse_sse_events(response.text)

    # Assert we got the expected event types
    event_types = [e["type"] for e in events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "text" in event_types
    assert "turn_started" in event_types
    assert "turn_ended" in event_types

    # Check tool call event
    tool_call_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_call_events) == 1
    tool_call_data = tool_call_events[0]["data"]["tool_call"]
    assert tool_call_data["id"] == tool_call_id
    assert tool_call_data["function"]["name"] == "add_or_update_note"

    # Check tool result event
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) == 1
    assert tool_result_events[0]["data"]["tool_call_id"] == tool_call_id
    assert "successfully" in tool_result_events[0]["data"]["result"]

    # Check final text
    text_events = [e for e in events if e["type"] == "text"]
    combined_text = "".join(e["data"]["content"] for e in text_events)
    assert combined_text == llm_final_reply

    # Check database - note should be created
    # Use a fresh Database to avoid PostgreSQL snapshot isolation issues
    # where a pre-existing transaction may not see data committed by the API handler.
    # Retry briefly since PostgreSQL commits may not be immediately visible.
    async def _check_note_exists() -> NoteModel | None:
        fresh_ctx = Database(engine=db_engine)
        return await fresh_ctx.notes.get_by_title(
            note_title, read_policy=NoteReadPolicy.UNRESTRICTED
        )

    note = await wait_for_condition(
        _check_note_exists,
        timeout=5.0,
        interval=0.2,
        description="note to be committed",
    )
    assert note is not None
    assert note.content == note_content


async def test_streaming_continues_after_tool_error(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
) -> None:
    """Test that streaming continues after a non-fatal tool error.

    When the LLM calls a tool that fails (e.g., tool not found), the error
    should be returned as a tool_result, and the LLM should continue
    generating a response. The stream should NOT be terminated.
    """
    user_prompt = "Please add a note for me"
    tool_call_id = "error_tool_call_123"
    llm_final_reply = "I'm sorry, that didn't work. Let me help you another way."

    # First LLM call: tries to call a tool with invalid (non-JSON) arguments
    def first_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(
            msg.role == "user" and user_prompt in str(msg.content or "")
            for msg in messages
        ) and not any(msg.role == "tool" for msg in messages)

    mock_llm_client.rules.append((
        first_call_matcher,
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments="not valid json {{{",
                    ),
                )
            ],
            reasoning_info=MessageReasoningInfo(total_tokens=0),
        ),
    ))

    # Second LLM call: after receiving the tool error, generate a helpful response
    def second_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id for msg in messages
        )

    mock_llm_client.rules.append((
        second_call_matcher,
        LLMOutput(
            content=llm_final_reply,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(total_tokens=0),
        ),
    ))

    # Act
    response = await run_chat_turn_stream(
        test_client,
        {"prompt": user_prompt},
    )

    assert response.status_code == 200

    # Parse SSE events
    events = parse_sse_events(response.text)

    event_types = [e["type"] for e in events]

    # Should have tool_call event
    assert "tool_call" in event_types, f"Expected tool_call event, got: {event_types}"

    # Should have tool_result event with error
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) >= 1, (
        f"Expected tool_result event, got: {event_types}"
    )
    tool_result_text = tool_result_events[0]["data"]["result"]
    assert "Error" in tool_result_text or "Invalid" in tool_result_text, (
        f"Expected error in tool result, got: {tool_result_text}"
    )

    # CRITICAL: Stream should continue after tool error - LLM should generate final response
    assert "text" in event_types, (
        f"Stream was cut off after tool error! Expected text events after tool_result "
        f"but got event types: {event_types}"
    )
    text_events = [e for e in events if e["type"] == "text"]
    combined_text = "".join(e["data"]["content"] for e in text_events)
    assert combined_text == llm_final_reply

    # Should complete normally with a single turn_ended event
    assert "turn_ended" in event_types, (
        f"Missing turn_ended event. Events: {event_types}"
    )
    turn_ended_events = [e for e in events if e["type"] == "turn_ended"]
    assert len(turn_ended_events) == 1, (
        f"Expected exactly 1 turn_ended event, got {len(turn_ended_events)}: {event_types}"
    )
    assert turn_ended_events[0]["data"]["status"] == "complete"

    # Should NOT have error events
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 0, f"Unexpected error events in stream: {error_events}"


class _ToolRaisingProvider:
    """Delegates to a real tools provider, except that one tool raises."""

    def __init__(
        self, wrapped: ToolsProvider, *, failing_tool: str, error: Exception
    ) -> None:
        self._wrapped = wrapped
        self._failing_tool = failing_tool
        self._error = error

    async def get_tool_definitions(self) -> list[ToolDefinition]:
        return await self._wrapped.get_tool_definitions()

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, object],
        context: ToolExecutionContext,
        call_id: str | None = None,
    ) -> str | ToolResult:
        if name == self._failing_tool:
            raise self._error
        return await self._wrapped.execute_tool(name, arguments, context, call_id)

    async def close(self) -> None:
        await self._wrapped.close()


async def test_streaming_continues_after_tool_execution_exception(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
) -> None:
    """Test that streaming continues when a tool raises an exception during execution.

    The exception should be caught and returned as a tool_result error,
    allowing the LLM to continue generating a response.
    """
    user_prompt = "Create a note called Test"
    tool_call_id = "exec_error_call_456"
    llm_final_reply = "The tool encountered an error. How else can I help?"

    # First LLM call: calls add_or_update_note
    def first_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(
            msg.role == "user" and user_prompt in str(msg.content or "")
            for msg in messages
        ) and not any(msg.role == "tool" for msg in messages)

    mock_llm_client.rules.append((
        first_call_matcher,
        LLMOutput(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id,
                    type="function",
                    function=ToolCallFunction(
                        name="add_or_update_note",
                        arguments=json.dumps({"title": "Test", "content": "test"}),
                    ),
                )
            ],
            reasoning_info=MessageReasoningInfo(total_tokens=0),
        ),
    ))

    # Second LLM call: after receiving the tool error
    def second_call_matcher(args: MatcherArgs) -> bool:
        messages = args.get("messages", [])
        return any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id for msg in messages
        )

    mock_llm_client.rules.append((
        second_call_matcher,
        LLMOutput(
            content=llm_final_reply,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(total_tokens=0),
        ),
    ))

    app_fixture.state.processing_service = _build_processing_service(
        llm_client=mock_llm_client,
        tools_provider=_ToolRaisingProvider(
            test_tools_provider,
            failing_tool="add_or_update_note",
            error=RuntimeError(
                "Simulated tool execution failure: database connection lost"
            ),
        ),
        service_config=mock_processing_service_config,
        context_providers=test_processing_service.context_providers,
    )

    response = await run_chat_turn_stream(
        test_client,
        {"prompt": user_prompt},
    )

    assert response.status_code == 200

    events = parse_sse_events(response.text)
    event_types = [e["type"] for e in events]

    assert "tool_call" in event_types, f"Expected tool_call. Events: {event_types}"

    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) == 1, f"Expected tool_result. Events: {event_types}"
    assert tool_result_events[0]["data"]["tool_call_id"] == tool_call_id
    tool_result_text = tool_result_events[0]["data"]["result"]
    assert "Error" in tool_result_text, (
        f"Expected error in tool result, got: {tool_result_text}"
    )
    assert "database connection lost" in tool_result_text

    # CRITICAL: Stream should continue after tool execution error
    assert "text" in event_types, (
        f"Stream was cut off after tool execution error! Expected text events "
        f"but got: {event_types}"
    )
    text_events = [e for e in events if e["type"] == "text"]
    combined_text = "".join(e["data"]["content"] for e in text_events)
    assert combined_text == llm_final_reply

    turn_ended_events = [e for e in events if e["type"] == "turn_ended"]
    assert len(turn_ended_events) == 1, (
        f"Missing turn_ended event. Events: {event_types}"
    )
    assert turn_ended_events[0]["data"]["status"] == "complete"

    # Tool errors are returned as tool_results, not stream errors
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 0, f"Unexpected error events: {error_events}"


class _SpyNotifier:
    """In-memory notifier capturing dispatched notifications for assertions."""

    def __init__(self) -> None:
        self.notified = asyncio.Event()
        self.calls: list[tuple[str, str, str, NotificationMetadata | None]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send_notification(
        self,
        user_identifier: str,
        title: str,
        body: str,
        db_context: Database,
        *,
        metadata: NotificationMetadata | None = None,
    ) -> None:
        self.calls.append((user_identifier, title, body, metadata))
        self.notified.set()


async def test_streaming_continues_and_notifies_after_client_disconnect(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the client disconnects mid-turn, processing finishes in the background
    and the final reply is delivered via push notification.

    The producer owns its own database context, so the subscriber going away
    must not close a connection the producer is still using: no database
    connection errors may be logged.
    """
    user_prompt = "Keep working even if I close the app"
    llm_response = "All done in the background!"
    conversation_id = "disconnect-conv-1"

    mock_llm_client.rules.append((
        lambda args: any(
            msg.role == "user" and user_prompt in str(msg.content or "")
            for msg in args.get("messages", [])
        ),
        LLMOutput(
            content=llm_response,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=10, completion_tokens=10, total_tokens=20
            ),
        ),
    ))

    # Gate the LLM so the turn is still in flight when we simulate the disconnect.
    started = asyncio.Event()
    release = asyncio.Event()
    original_generate = mock_llm_client.generate_response

    async def gated_generate_response(*args: object, **kwargs: object) -> LLMOutput:
        started.set()
        await release.wait()
        return await original_generate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mock_llm_client, "generate_response", gated_generate_response)

    # Wire a spy notifier so we can observe the push delivery.
    spy = _SpyNotifier()
    app_fixture.state.web_chat_interface = WebChatInterface(db_engine, notifier=spy)

    with caplog.at_level(logging.ERROR):
        # Start the turn. The producer runs in the hub, decoupled from any HTTP
        # request, so it survives the subscriber disconnecting.
        turn_id = str(uuid.uuid4())
        post_response = await test_client.post(
            "/api/v1/chat/turns",
            json={
                "turn_id": turn_id,
                "prompt": user_prompt,
                "interface_type": "web",
                "conversation_id": conversation_id,
            },
        )
        assert post_response.status_code == 200
        hub: ConversationStreamHub = app_fixture.state.conversation_stream_hub

        # Open a stream subscription and wait until it is attached and
        # processing is underway.
        subscribe_task = asyncio.create_task(
            test_client.get(
                f"/api/v1/chat/conversations/{conversation_id}/stream",
                params={"from_seq": 0},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=10)
        await wait_for_condition(
            lambda: hub.subscriber_count(conversation_id) == 1,
            timeout=10.0,
            description="stream subscriber to attach",
        )

        # Simulate the client closing the app: cancel the in-flight subscription
        # before it can observe (and acknowledge) the turn_ended event.
        subscribe_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscribe_task
        await wait_for_condition(
            lambda: hub.subscriber_count(conversation_id) == 0,
            timeout=10.0,
            description="stream subscriber to detach",
        )

        # Processing should continue in the background; let the LLM finish.
        release.set()

        # The completed reply must be delivered as a push notification.
        await asyncio.wait_for(spy.notified.wait(), timeout=10)

        # Let the background producer task fully settle before assertions/teardown.
        producer_tasks = hub.get_active_producer_tasks(conversation_id)
        if producer_tasks:
            await asyncio.gather(*producer_tasks, return_exceptions=True)

    db_error_messages = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and (
            "NoneType" in record.getMessage()
            or "database connection" in record.getMessage().lower()
        )
    ]
    assert db_error_messages == [], (
        f"Database connection errors logged after disconnect: {db_error_messages}"
    )

    assert len(spy.calls) == 1
    user_identifier, _title, body, metadata = spy.calls[0]
    assert user_identifier == "test_user"
    assert llm_response in body
    assert metadata is not None
    assert metadata.category == MESSAGE_CATEGORY
    assert metadata.conversation_id == conversation_id

    # The assistant reply must also have been persisted despite the disconnect.
    fresh_ctx = Database(engine=db_engine)
    messages = await fresh_ctx.message_history.get_recent_with_metadata(
        interface_type="web",
        conversation_id=conversation_id,
        limit=20,
    )
    assistant_messages = [
        m
        for m in messages
        if m["role"] == "assistant" and llm_response in str(m.get("content") or "")
    ]
    assert assistant_messages, "Assistant reply should be persisted after disconnect"


class _TaintLookupUnavailableError(RuntimeError):
    pass


class _TaintLookupFailsOnceProvider:
    """A tainted context provider whose first taint lookup fails."""

    def __init__(self) -> None:
        self.taint_lookups = 0

    @property
    def name(self) -> str:
        return "taint_lookup_fails_once"

    async def get_context_fragments(self, acting_user_id: str | None) -> list[str]:
        return []

    async def get_context_taint_sources(self) -> tuple[TaintSource, ...]:
        self.taint_lookups += 1
        if self.taint_lookups == 1:
            raise _TaintLookupUnavailableError("taint store unavailable")
        return ()


async def test_setup_failure_before_the_prompt_write_leaves_the_turn_retryable(
    app_fixture: FastAPI,
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_engine: AsyncEngine,
) -> None:
    """A failure during pre-producer setup must not strand the prompt.

    The prompt row is what the durable idempotency branch keys on, so a retry
    carrying the same turn_id would return already_complete and never run the
    turn -- leaving the user looking at a prompt that can never get a reply.
    """
    user_prompt = "Answer me even though setup failed once"
    llm_response = "Here is your answer."
    conversation_id = "setup-failure-conv-1"

    mock_llm_client.rules.append((
        lambda args: any(
            msg.role == "user" and user_prompt in str(msg.content or "")
            for msg in args.get("messages", [])
        ),
        LLMOutput(
            content=llm_response,
            tool_calls=None,
            reasoning_info=MessageReasoningInfo(
                prompt_tokens=10, completion_tokens=10, total_tokens=20
            ),
        ),
    ))

    app_fixture.state.processing_service = _build_processing_service(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=[
            *test_processing_service.context_providers,
            _TaintLookupFailsOnceProvider(),
        ],
    )

    turn_id = str(uuid.uuid4())
    body = {
        "turn_id": turn_id,
        "prompt": user_prompt,
        "interface_type": "web",
        "conversation_id": conversation_id,
    }

    with pytest.raises(RuntimeError) as setup_failure:
        await test_client.post("/api/v1/chat/turns", json=body)
    failure_chain: list[BaseException] = []
    failure: BaseException | None = setup_failure.value
    while failure is not None:
        failure_chain.append(failure)
        failure = failure.__cause__
    assert any(
        isinstance(exc, _TaintLookupUnavailableError) for exc in failure_chain
    ), f"POST failed for an unexpected reason: {failure_chain!r}"

    # The retry carries the same turn_id and must actually run the turn.
    # The prompt must not have survived the failed setup: it is what the
    # durable idempotency branch keys on.
    stranded = await Database(engine=db_engine).message_history.get_user_row_by_turn_id(
        turn_id
    )
    assert stranded is None

    # Simulate the restart that makes this reachable. Within one process the
    # hub's own in-memory turn record short-circuits the retry first; after a
    # restart only the durable row is left to decide, which is the case the
    # ordering above exists for.
    app_fixture.state.conversation_stream_hub = ConversationStreamHub()

    retry = await test_client.post("/api/v1/chat/turns", json=body)
    assert retry.status_code == 200
    assert retry.json()["already_complete"] is False

    hub = app_fixture.state.conversation_stream_hub
    producer_tasks = hub.get_active_producer_tasks(conversation_id)
    if producer_tasks:
        await asyncio.gather(*producer_tasks, return_exceptions=True)

    # The retry actually ran the turn, so the prompt has its reply.
    messages = await Database(
        engine=db_engine
    ).message_history.get_recent_with_metadata(
        interface_type="web",
        conversation_id=conversation_id,
        limit=20,
    )
    assert any(
        msg["role"] == "assistant" and llm_response in str(msg["content"] or "")
        for msg in messages
    )
