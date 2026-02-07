import json
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.llm import (
    LLMInterface,
    LLMOutput,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage import init_db
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,
)
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import MatcherArgs, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig

logger = logging.getLogger(__name__)


# --- Fixtures ---


@pytest_asyncio.fixture(scope="function")
async def db_context(
    db_engine: AsyncEngine,
) -> AsyncGenerator[DatabaseContext]:
    """Provides a DatabaseContext for a single test function."""
    async with get_db_context(engine=db_engine) as ctx:
        yield ctx


@pytest.fixture(scope="function")
def mock_processing_service_config() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for tests."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": (
                "You are a test assistant. Current time: {current_time}. "
                "Server URL: {server_url}. "
                "Context: {aggregated_other_context}"
            )
        },
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": [
                "add_or_update_note"
            ],  # Ensure our target tool is enabled
            "enable_mcp_server_ids": [],
            "confirm_tools": [],  # Ensure add_or_update_note is NOT here for API test
        },
        delegation_security_level="confirm",
        id="chat_api_test_profile",
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
    Provides a ToolsProvider stack (Local, MCP, Composite, Confirming)
    configured for testing.
    """
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        embedding_generator=None,
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
    )
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=set(
            mock_processing_service_config.tools_config.get("confirm_tools", [])
        ),
    )
    await confirming_provider.get_tool_definitions()
    return confirming_provider


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_context: DatabaseContext,
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components."""

    captured_engine = db_context.engine

    async def get_entered_db_context_for_provider() -> DatabaseContext:
        """
        Returns an awaitable that resolves to an entered DatabaseContext.
        This matches the expected type for NotesContextProvider's get_db_context_func.
        """
        async with get_db_context(engine=captured_engine) as new_ctx:
            return new_ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
        timezone_str=mock_processing_service_config.timezone_str,
        prompts=mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=mock_processing_service_config.prompts
    )
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),
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

    async with get_db_context(engine=db_engine) as temp_db_ctx:
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
    db_context: DatabaseContext,
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
            reasoning_info={"model": "test-model", "usage": {"total_tokens": 100}},
        ),
    ))

    # Act - Make streaming request
    response = await test_client.post(
        "/api/v1/chat/send_message_stream",
        json={"prompt": user_prompt},
    )

    # Assert response basics
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    # Parse SSE events
    events = []
    for line in response.text.split("\n"):
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_str = line.split(":", 1)[1].strip()
            if data_str:
                events.append({"type": event_type, "data": json.loads(data_str)})

    # Assert we got the expected events
    text_events = [e for e in events if e["type"] == "text"]
    end_events = [e for e in events if e["type"] == "end"]
    close_events = [e for e in events if e["type"] == "close"]

    # Should have text content
    assert len(text_events) > 0
    combined_text = "".join(e["data"]["content"] for e in text_events)
    assert combined_text == llm_response

    # Should have end event
    assert len(end_events) == 1

    # Should have close event
    assert len(close_events) == 1

    # Check database state - messages should be saved
    # Need to extract conversation_id from the stream (it's generated)
    # Since we don't have it in the response, we'll check for any recent messages
    recent_conversations = await db_context.message_history.get_all_grouped(
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
    db_context: DatabaseContext,
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
            reasoning_info={"model": "test-model", "usage": {"total_tokens": 50}},
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
            reasoning_info={"model": "test-model", "usage": {"total_tokens": 75}},
        ),
    ))

    # Act - Make streaming request
    response = await test_client.post(
        "/api/v1/chat/send_message_stream",
        json={"prompt": user_prompt},
    )

    # Assert response basics
    assert response.status_code == 200

    # Parse SSE events
    events = []
    current_event_type = None
    for line in response.text.split("\n"):
        if line.startswith("event:"):
            current_event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event_type:
            data_str = line.split(":", 1)[1].strip()
            if data_str:
                events.append({
                    "type": current_event_type,
                    "data": json.loads(data_str),
                })

    # Assert we got the expected event types
    event_types = [e["type"] for e in events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "text" in event_types
    assert "end" in event_types
    assert "close" in event_types

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
    note = await db_context.notes.get_by_title(note_title, visibility_grants=None)
    assert note is not None
    assert note.content == note_content
