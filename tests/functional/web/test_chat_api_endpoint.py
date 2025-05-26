import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Callable

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant import storage
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
from family_assistant.processing import ProcessingService,
ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext,
get_db_context
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
from family_assistant.web.routers.api import ChatMessageResponse
from tests.mocks.mock_llm import MatcherArgs, Rule,
RuleBasedMockLLMClient

logger = logging.getLogger(__name__)


# --- Fixtures ---

# Use the existing db_engine fixture from tests/conftest.py


@pytest_asyncio.fixture(scope="function")
async def db_context(db_engine: AsyncEngine) ->
AsyncGenerator[DatabaseContext, None]:
    """Provides a DatabaseContext for a single test function."""
    async with get_db_context(engine=db_engine) as ctx:
        yield ctx


@pytest.fixture(scope="function")
def mock_processing_service_config() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for tests."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": "You are a test assistant. Current time:
{current_time}. Notes: {notes_context}. Calendar: {calendar_context}.
Known Users: {known_users_context}. Server URL: {server_url}.
Aggregated Context: {aggregated_other_context}"
        },
        calendar_config={},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": ["add_or_update_note"], # Ensure our
target tool is enabled
            "enable_mcp_server_ids": [],
            "confirm_tools": [],  # Ensure add_or_update_note is NOT
here for API test
        },
    )


@pytest.fixture(scope="function")
def mock_llm_client() -> RuleBasedMockLLMClient:
    """Provides a RuleBasedMockLLMClient for tests."""
    return RuleBasedMockLLMClient(rules=[]) # Rules will be set
per-test


@pytest_asyncio.fixture(scope="function")
async def test_tools_provider(
    mock_processing_service_config: ProcessingServiceConfig,
) -> ToolsProvider:
    """
    Provides a ToolsProvider stack (Local, MCP, Composite, Confirming)
    configured for testing.
    """
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, # Use actual definitions
        implementations=local_tool_implementations, # Use actual
implementations
        embedding_generator=None, # Not needed for add_note
        calendar_config=mock_processing_service_config.calendar_config,
    )
    # Mock MCP provider as it's not the focus here
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed
(mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions() # Initialize

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=set(
            mock_processing_service_config.tools_config.get("confirm_to
ols", [])
        ),
        calendar_config=mock_processing_service_config.calendar_config,
    )
    await confirming_provider.get_tool_definitions() # Initialize
    return confirming_provider


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_context: DatabaseContext, # For context providers
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test
components."""
    # Create mock context providers
    notes_provider = NotesContextProvider(
        get_db_context_func=lambda: db_context, # Simplified for test
        prompts=mock_processing_service_config.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=mock_processing_service_config.calendar_config,
        timezone_str=mock_processing_service_config.timezone_str,
        prompts=mock_processing_service_config.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={},
prompts=mock_processing_service_config.prompts
    )
    context_providers = [notes_provider, calendar_provider,
known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config={}, # Minimal app_config for this test
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine, # Use the main db_engine fixture
    test_processing_service: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_llm_client: LLMInterface,
) -> FastAPI:
    """
    Creates a FastAPI application instance for testing, with a mock
ProcessingService.
    """
    # Make a copy of the actual app to avoid modifying it globally
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware, # Use actual middleware
    )
    app.include_router(actual_app.router) # Include actual routers

    # Override dependencies in app.state
    app.state.processing_service = test_processing_service
    app.state.tools_provider = test_tools_provider # For
/api/tools/execute if needed
    app.state.engine = db_engine # For get_db dependency
    app.state.config = { # Minimal config for dependencies
        "auth_enabled": True, # Assume auth is enabled for API token
tests
        "database_url": str(db_engine.url),
        "default_profile_settings": { # For KnownUsersContextProvider
            "chat_id_to_name_map": {},
            "processing_config": {"prompts": {}}
        }
    }
    app.state.llm_client = mock_llm_client # For other parts that might
use it

    # Ensure database is initialized for this app instance
    async with get_db_context(engine=db_engine) as temp_db_ctx:
        await storage.init_db(engine=db_engine) # Initialize main
schema
        await storage.init_vector_db(temp_db_ctx) # Initialize vector
schema

    return app


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) ->
AsyncGenerator[AsyncClient, None]:
    """Provides an HTTPX AsyncClient for the test FastAPI app."""
    async with AsyncClient(app=app_fixture,
base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture(scope="function")
async def api_token_fixture(db_context: DatabaseContext) -> str:
    """
    Creates a valid API token in the database and returns the raw token
string.
    """
    user_identifier = f"test_api_user_{uuid.uuid4()}@example.com"
    token_name = "Test API Token"
    # This function from storage.api_tokens creates and stores the
token, returning the raw token
    raw_token, _ = await storage.create_and_store_api_token(
        db_context=db_context, user_identifier=user_identifier,
name=token_name
    )
    return raw_token


# --- Test Cases ---

@pytest.mark.asyncio
async def test_api_chat_unauthenticated(test_client: AsyncClient) ->
None:
    """Test that sending a message without an API token fails with
401."""
    response = await test_client.post(
        "/api/v1/chat/send_message", json={"prompt": "Hello"}
    )
    assert response.status_code == 401
    assert "Not authenticated" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_chat_invalid_token(test_client: AsyncClient) ->
None:
    """Test that sending a message with an invalid API token fails with
401."""
    response = await test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Hello"},
        headers={"Authorization": "Bearer invalidtoken123"},
    )
    assert response.status_code == 401
    assert "Invalid or expired API token" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_chat_add_note_tool(
    test_client: AsyncClient,
    db_context: DatabaseContext,
    api_token_fixture: str,
    mock_llm_client: RuleBasedMockLLMClient, # To set rules
    test_processing_service: ProcessingService, # To access its config
) -> None:
    """
    Test sending a prompt that triggers the 'add_or_update_note' tool,
    verifies the tool call, database change, and final response.
    """
    # Arrange
    user_prompt = "Please add a note. Title: API Test Note. Content:
This is a test from the API."
    note_title = "API Test Note"
    note_content = "This is a test from the API."
    tool_call_id = f"call_{uuid.uuid4()}"
    llm_intermediate_reply = "Okay, I will add that note for you."
    llm_final_reply = f"I have added the note titled '{note_title}'."

    # Configure mock LLM rules
    # Rule 1: User prompt -> LLM requests add_or_update_note tool
    def rule1_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_msg_content = messages[-1].get("content") if messages else
""
        return isinstance(last_msg_content, str) and "Please add a
note" in last_msg_content

    rule1_output = LLMOutput(
        content=llm_intermediate_reply,
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="add_or_update_note",
                    arguments=json.dumps({"title": note_title,
"content": note_content}),
                ),
            )
        ],
    )
    # Rule 2: Tool result -> LLM gives final confirmation
    def rule2_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.get("role") == "tool" and msg.get("tool_call_id") ==
tool_call_id
            for msg in messages
        )

    rule2_output = LLMOutput(content=llm_final_reply)
    mock_llm_client.rules = [(rule1_matcher, rule1_output),
(rule2_matcher, rule2_output)]

    # Ensure 'add_or_update_note' is not in confirm_tools for the
test_processing_service
    # This is handled by mock_processing_service_config fixture

    # Act
    response = await test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": user_prompt},
        headers={"Authorization": f"Bearer {api_token_fixture}"},
    )

    # Assert API Response
    assert response.status_code == 200
    response_data = ChatMessageResponse(**response.json())
    assert response_data.reply == llm_final_reply
    assert response_data.conversation_id is not None
    assert response_data.turn_id is not None
    logger.info(f"API Response: {response_data}")

    # Assert LLM was called twice (initial prompt, then tool result)
    assert len(mock_llm_client.get_calls()) == 2

    # Assert Database State (Note created)
    note = await storage.get_note_by_title(db_context, note_title)
    assert note is not None
    assert note["content"] == note_content
    logger.info(f"Note '{note_title}' found in database with correct
content.")

    # Assert Message History
    # Fetch history for the conversation_id from the response
    history = await storage.get_recent_history(
        db_context,
        interface_type="api",
        conversation_id=response_data.conversation_id,
        limit=10, # Get enough messages
        max_age=asyncio.timedelta(minutes=5) # Recent enough
    )
    assert len(history) >= 3 # User prompt, Assistant tool request,
Tool response, Final Assistant reply

    # Check for user message
    user_msg_found = any(h["role"] == "user" and h["content"] ==
user_prompt for h in history)
    assert user_msg_found, "User prompt not found in history"

    # Check for assistant message with tool call
    assistant_tool_call_msg_found = any(
        h["role"] == "assistant" and h.get("tool_calls") is not None
and
        h["tool_calls"][0]["id"] == tool_call_id and
        h["tool_calls"][0]["function"]["name"] == "add_or_update_note"
        for h in history
    )
    assert assistant_tool_call_msg_found, "Assistant tool call request
not found in history"

    # Check for tool response message
    tool_response_msg_found = any(
        h["role"] == "tool" and h["tool_call_id"] == tool_call_id
        # Content of add_or_update_note is usually "Note '...'
added/updated."
        and "Note 'API Test Note' added/updated." in str(h["content"])
        for h in history
    )
    assert tool_response_msg_found, "Tool response not found in
history"

    # Check for final assistant reply
    final_assistant_reply_found = any(
        h["role"] == "assistant" and h["content"] == llm_final_reply
and h.get("tool_calls") is None
        for h in history
    )
    assert final_assistant_reply_found, "Final assistant reply not
found in history"

    logger.info("Message history assertions passed.")
