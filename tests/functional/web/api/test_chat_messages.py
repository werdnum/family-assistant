import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
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
from family_assistant.llm.messages import (
    AssistantMessage,
    MessageAttachmentMetadata,
    ToolMessage,
    UserMessage,
)
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage import init_db
from family_assistant.storage.database import Database
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
from family_assistant.web.app_creator import app as actual_app
from family_assistant.web.dependencies import get_current_user
from family_assistant.web.models import ChatMessageResponse
from family_assistant.web.web_chat_interface import WebChatInterface
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig

logger = logging.getLogger(__name__)


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
        delegation_security_level=DelegationSecurityLevel.CONFIRM,  # Added
        id="chat_api_test_profile",  # Added
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
        embedding_generator=None,  # Not needed for add_note
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
    )
    # Mock MCP provider as it's not the focus here
    mock_mcp_provider = AsyncMock(spec=MCPToolsProvider)
    mock_mcp_provider.get_tool_definitions.return_value = []
    mock_mcp_provider.execute_tool.return_value = "MCP tool executed (mock)."
    mock_mcp_provider.close.return_value = None

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mock_mcp_provider]
    )
    await composite_provider.get_tool_definitions()  # Initialize

    policy_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )
    await policy_provider.get_tool_definitions()
    return policy_provider


@pytest.fixture(scope="function")
def test_processing_service(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config: ProcessingServiceConfig,
    db_context: Database,  # This is an instance of Database from the fixture
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components."""

    # NotesContextProvider expects get_db_context_func to be Callable[[], Awaitable[Database]]
    # This means it wants a function that, when called and awaited, returns an *entered* Database.
    # The db_context fixture provides an already entered Database instance.
    # We need its engine to create new contexts for the provider if it manages its own lifecycle.
    captured_engine = db_context.engine

    def get_entered_db_context_for_provider() -> Database:
        """
        Returns an awaitable that resolves to an entered Database.
        This matches the expected type for NotesContextProvider's get_db_context_func.
        """
        new_ctx = Database(engine=captured_engine)
        return new_ctx

    # Create mock context providers
    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config.prompts,
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
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),  # Minimal app_config for this test
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture(
    db_engine: AsyncEngine,  # Use the main db_engine fixture
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
        middleware=actual_app.user_middleware,  # Use actual middleware
    )
    app.include_router(actual_app.router)  # Include actual routers

    # Override dependencies in app.state
    app.state.processing_service = test_processing_service
    app.state.tools_provider = test_tools_provider  # For /api/tools/execute if needed
    app.state.database_engine = db_engine  # For get_db dependency
    app.state.config = AppConfig(
        database_url=str(db_engine.url),
    )
    app.state.llm_client = mock_llm_client  # For other parts that might use it
    app.state.debug_mode = False  # Explicitly set for tests

    # Initialize WebChatInterface for web API tests
    app.state.web_chat_interface = WebChatInterface(db_engine)

    # Ensure database is initialized for this app instance
    temp_db_ctx = Database(engine=db_engine)
    await init_db(db_engine)  # Initialize main schema
    await temp_db_ctx.init_vector_db()  # Initialize vector schema

    return app


@pytest_asyncio.fixture(scope="function")
async def test_client(app_fixture: FastAPI) -> AsyncGenerator[AsyncClient]:
    """Provides an HTTPX AsyncClient for the test FastAPI app."""
    transport = ASGITransport(app=app_fixture)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture(scope="function")
def mock_processing_service_config_no_tools() -> ProcessingServiceConfig:
    """Provides a mock ProcessingServiceConfig for tests with no tools."""
    return ProcessingServiceConfig(
        prompts={
            "system_prompt": "You are a test assistant. Server URL: {server_url}."
        },
        timezone=ZoneInfo("UTC"),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.BLOCKED,
        id="chat_api_test_profile_no_tools",
    )


@pytest.fixture(scope="function")
def test_processing_service_no_tools(
    mock_llm_client: RuleBasedMockLLMClient,
    test_tools_provider: ToolsProvider,
    mock_processing_service_config_no_tools: ProcessingServiceConfig,
    db_context: Database,
) -> ProcessingService:
    """Creates a ProcessingService instance with mock/test components and no tools."""

    captured_engine = db_context.engine

    def get_entered_db_context_for_provider() -> Database:
        new_ctx = Database(engine=captured_engine)
        return new_ctx

    notes_provider = NotesContextProvider(
        get_db_context_func=get_entered_db_context_for_provider,
        prompts=mock_processing_service_config_no_tools.prompts,
    )
    calendar_provider = CalendarContextProvider(
        calendar_config=cast(
            "CalendarConfig", {"caldav": {"calendar_urls": ["http://test.com"]}}
        ),
        timezone=mock_processing_service_config_no_tools.timezone,
        prompts=mock_processing_service_config_no_tools.prompts,
    )
    known_users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={}, prompts=mock_processing_service_config_no_tools.prompts
    )
    context_providers = [notes_provider, calendar_provider, known_users_provider]

    return ProcessingService(
        llm_client=mock_llm_client,
        tools_provider=test_tools_provider,
        service_config=mock_processing_service_config_no_tools,
        context_providers=context_providers,
        server_url="http://testserver",
        app_config=AppConfig(),
    )


@pytest_asyncio.fixture(scope="function")
async def app_fixture_no_tools(
    db_engine: AsyncEngine,
    test_processing_service_no_tools: ProcessingService,
    test_tools_provider: ToolsProvider,
    mock_llm_client: LLMInterface,
) -> FastAPI:
    """
    Creates a FastAPI application instance for testing, with a mock
    ProcessingService that has no tools.
    """
    app = FastAPI(
        title=actual_app.title,
        docs_url=actual_app.docs_url,
        redoc_url=actual_app.redoc_url,
        middleware=actual_app.user_middleware,
    )
    app.include_router(actual_app.router)

    app.state.processing_service = test_processing_service_no_tools
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
async def test_client_no_tools(
    app_fixture_no_tools: FastAPI,
) -> AsyncGenerator[AsyncClient]:
    """Provides an HTTPX AsyncClient for the test FastAPI app with no tools."""
    transport = ASGITransport(app=app_fixture_no_tools)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --- Test Cases ---


@pytest.mark.asyncio
async def test_api_chat_add_note_tool(
    test_client: AsyncClient,
    db_context: Database,
    mock_llm_client: RuleBasedMockLLMClient,  # To set rules
    test_processing_service: ProcessingService,  # To access its config
) -> None:
    """
    Test sending a prompt that triggers the 'add_or_update_note' tool,
    verifies the tool call, database change, and final response.
    """
    # Arrange
    user_prompt = (
        "Please add a note. Title: API Test Note. Content: This is a test from the API."
    )
    note_title = "API Test Note"
    note_content = "This is a test from the API."
    tool_call_id = f"call_{uuid.uuid4()}"
    llm_intermediate_reply = "Okay, I will add that note for you."
    llm_final_reply = f"I have added the note titled '{note_title}'."

    # Configure mock LLM rules
    # Rule 1: User prompt -> LLM requests add_or_update_note tool
    def rule1_matcher(kwargs: MatcherArgs) -> bool:
        # get_last_message_text skips the trailing <turn_context> block, so
        # "the last message" still means the newest real one -- the user prompt
        # on the first call, the tool result on the second.
        return "Please add a note" in get_last_message_text(kwargs.get("messages", []))

    rule1_output = LLMOutput(
        content=llm_intermediate_reply,
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
    )
    # Rule 2: Tool result -> LLM gives final confirmation

    def rule2_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            msg.role == "tool" and msg.tool_call_id == tool_call_id for msg in messages
        )

    rule2_output = LLMOutput(content=llm_final_reply)
    mock_llm_client.rules = [
        (rule1_matcher, rule1_output),
        (rule2_matcher, rule2_output),
    ]

    # Act
    response = await test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": user_prompt},
        # No headers needed as authentication is off
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
    note = await db_context.notes.get_by_title(note_title, visibility_grants=None)
    assert note is not None
    assert note.content == note_content

    # Assert Message History
    # Fetch history for the conversation_id from the response
    history = await db_context.message_history.get_recent(
        interface_type="api",
        conversation_id=response_data.conversation_id,
        limit=10,  # Get enough messages
        max_age=timedelta(minutes=5),  # Recent enough
    )
    assert (
        len(history) >= 3
    )  # User prompt, Assistant tool request, Tool response, Final Assistant reply

    # Check for user message (history now contains typed LLMMessage objects)
    user_msg_found = any(
        isinstance(h, UserMessage) and h.content == user_prompt for h in history
    )
    assert user_msg_found, "User prompt not found in history"

    # Check for assistant message with tool call
    assistant_tool_call_msg_found = any(
        isinstance(h, AssistantMessage)
        and h.tool_calls is not None
        and h.tool_calls[0].id == tool_call_id
        and h.tool_calls[0].function.name == "add_or_update_note"
        for h in history
    )
    assert assistant_tool_call_msg_found, (
        "Assistant tool call request not found in history"
    )

    # Check for tool response message
    tool_response_msg_found = any(
        isinstance(h, ToolMessage)
        and h.tool_call_id == tool_call_id
        and "has been" in str(h.content)
        and "successfully" in str(h.content)
        for h in history
    )
    assert tool_response_msg_found, (
        "Tool response with expected success message not found in history"
    )

    # Check for final assistant reply
    final_assistant_reply_found = any(
        isinstance(h, AssistantMessage)
        and h.content == llm_final_reply
        and h.tool_calls is None
        for h in history
    )
    assert final_assistant_reply_found, "Final assistant reply not found in history"

    logger.info("Message history assertions passed.")


@pytest.mark.asyncio
async def test_api_chat_send_message_persists_user_id(
    test_client: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_context: Database,
) -> None:
    """Test that user_id is correctly persisted when sending a message."""
    # Arrange
    user_prompt = "Does this message save my user ID?"
    llm_response = "Yes, it should."
    expected_user_id = "test_user"  # From get_current_user dependency when auth is off

    # Configure mock LLM to give a simple response
    mock_llm_client.rules.append((
        lambda args: user_prompt in str(args.get("messages", [])),
        LLMOutput(content=llm_response),
    ))

    # Act
    response = await test_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": user_prompt},
    )

    # Assert API Response
    assert response.status_code == 200
    response_data = ChatMessageResponse(**response.json())
    conversation_id = response_data.conversation_id

    # Assert Database State
    history = await db_context.message_history.get_recent_with_metadata(
        interface_type="api",
        conversation_id=conversation_id,
        limit=10,
        max_age=timedelta(minutes=1),
    )

    assert len(history) == 2  # Should be user message and assistant message

    # Check user message
    user_message = next((m for m in history if m["role"] == "user"), None)
    assert user_message is not None
    assert user_message["content"] == user_prompt
    assert user_message["user_id"] == expected_user_id

    # Check assistant message
    assistant_message = next((m for m in history if m["role"] == "assistant"), None)
    assert assistant_message is not None
    assert assistant_message["content"] == llm_response
    assert assistant_message["user_id"] == expected_user_id

    logger.info(
        f"Successfully verified user_id '{expected_user_id}' was persisted for all messages."
    )


@pytest.mark.asyncio
async def test_api_chat_send_message_persists_user_id_no_tools(
    test_client_no_tools: AsyncClient,
    mock_llm_client: RuleBasedMockLLMClient,
    db_context: Database,
) -> None:
    """Test that user_id is correctly persisted when sending a message with no tools enabled."""
    # Arrange
    user_prompt = "Does this message save my user ID without tools?"
    llm_response = "Yes, it should."
    expected_user_id = "test_user"  # From get_current_user dependency when auth is off

    # Configure mock LLM to give a simple response
    mock_llm_client.rules.append((
        lambda args: user_prompt in str(args.get("messages", [])),
        LLMOutput(content=llm_response),
    ))

    # Act
    response = await test_client_no_tools.post(
        "/api/v1/chat/send_message",
        json={"prompt": user_prompt},
    )

    # Assert API Response
    assert response.status_code == 200
    response_data = ChatMessageResponse(**response.json())
    conversation_id = response_data.conversation_id

    # Assert Database State
    history = await db_context.message_history.get_recent_with_metadata(
        interface_type="api",
        conversation_id=conversation_id,
        limit=10,
        max_age=timedelta(minutes=1),
    )

    assert len(history) == 2  # Should be user message and assistant message

    # Check user message
    user_message = next((m for m in history if m["role"] == "user"), None)
    assert user_message is not None
    assert user_message["content"] == user_prompt
    assert user_message["user_id"] == expected_user_id

    # Check assistant message
    assistant_message = next((m for m in history if m["role"] == "assistant"), None)
    assert assistant_message is not None
    assert assistant_message["content"] == llm_response
    assert assistant_message["user_id"] == expected_user_id

    logger.info(
        f"Successfully verified user_id '{expected_user_id}' was persisted for all messages without tools."
    )


@pytest.mark.asyncio
async def test_conversation_share_is_authenticated_read_only_and_revocable(
    test_client: AsyncClient,
    app_fixture: FastAPI,
    db_context: Database,
    tmp_path: Path,
) -> None:
    conversation_id = str(uuid.uuid4())
    await db_context.message_history.add_message(
        UserMessage(content="Please help me choose a gift"),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
    )
    attachment_registry = AttachmentRegistry(str(tmp_path), db_context.engine)
    app_fixture.state.attachment_registry = attachment_registry
    attachment = await attachment_registry.register_user_attachment(
        db_context,
        b"shared details",
        "details.txt",
        "text/plain",
        conversation_id=conversation_id,
        user_id="test_user",
    )
    await db_context.message_history.add_message(
        AssistantMessage(content="Here are the gift options."),
        interface_type="web",
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id="test_user",
        attachments=[
            MessageAttachmentMetadata(
                type="attachment_reference",
                attachment_id=attachment.attachment_id,
            )
        ],
    )
    foreign_attachment = await attachment_registry.register_user_attachment(
        db_context,
        b"private",
        "private.txt",
        "text/plain",
        conversation_id=str(uuid.uuid4()),
        user_id="test_user",
    )

    create_response = await test_client.post(
        f"/api/v1/chat/conversations/{conversation_id}/share"
    )
    assert create_response.status_code == 200
    share_url = create_response.json()["share_url"]
    token = share_url.rsplit("/", 1)[-1]
    stored_share = await db_context.conversation_shares.get_by_conversation(
        conversation_id
    )
    assert stored_share is not None
    assert stored_share.token_hash != token
    assert len(stored_share.token_hash) == 64
    status_response = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/share"
    )
    assert status_response.json() == {"active": True}

    replacement_response = await test_client.post(
        f"/api/v1/chat/conversations/{conversation_id}/share"
    )
    replacement_token = replacement_response.json()["share_url"].rsplit("/", 1)[-1]
    assert replacement_token != token

    async def wife_user() -> dict[str, str]:
        return {"user_identifier": "wife"}

    app_fixture.dependency_overrides[get_current_user] = wife_user
    non_owner_share = await test_client.post(
        f"/api/v1/chat/conversations/{conversation_id}/share"
    )
    assert non_owner_share.status_code == 404
    replaced_read = await test_client.get(
        f"/api/v1/shared-conversations/{token}/messages"
    )
    assert replaced_read.status_code == 404
    token = replacement_token
    owner_read = await test_client.get(
        f"/api/v1/chat/conversations/{conversation_id}/messages"
    )
    assert owner_read.status_code == 404

    shared_read = await test_client.get(
        f"/api/v1/shared-conversations/{token}/messages"
    )
    assert shared_read.status_code == 200
    payload = shared_read.json()
    assert [message["content"] for message in payload["messages"]] == [
        "Please help me choose a gift",
        "Here are the gift options.",
    ]
    shared_attachment_url = payload["messages"][1]["attachments"][0]["content_url"]
    attachment_response = await test_client.get(shared_attachment_url)
    assert attachment_response.status_code == 200
    assert attachment_response.content == b"shared details"
    foreign_attachment_response = await test_client.get(
        f"/api/v1/shared-conversations/{token}/attachments/"
        f"{foreign_attachment.attachment_id}"
    )
    assert foreign_attachment_response.status_code == 404

    conversation_list = await test_client.get(
        "/api/v1/chat/conversations?interface_type=web"
    )
    assert conversation_list.status_code == 200
    assert conversation_list.json()["conversations"] == []

    app_fixture.dependency_overrides.pop(get_current_user)
    revoke_response = await test_client.delete(
        f"/api/v1/chat/conversations/{conversation_id}/share"
    )
    assert revoke_response.status_code == 204
    app_fixture.dependency_overrides[get_current_user] = wife_user
    revoked_read = await test_client.get(
        f"/api/v1/shared-conversations/{token}/messages"
    )
    assert revoked_read.status_code == 404
    app_fixture.dependency_overrides.pop(get_current_user)
