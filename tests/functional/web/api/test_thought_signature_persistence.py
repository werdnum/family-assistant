"""Integration test for thought signature persistence through HTTP API.

This test verifies that thought signatures are preserved through the entire
user journey via the HTTP API with real LLM integration.
"""

import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
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
)
from family_assistant.web.app_creator import app
from family_assistant.web.web_chat_interface import WebChatInterface

if TYPE_CHECKING:
    from fastapi import FastAPI


@pytest_asyncio.fixture
async def llm_integration_processing_service(
    db_engine: AsyncEngine,
) -> AsyncGenerator[ProcessingService]:
    """ProcessingService with real Gemini LLM client for integration testing."""
    # Create real LLM client with thinking model
    llm_client = GoogleGenAIClient(
        api_key=os.getenv("GEMINI_API_KEY", "test-key"),
        model="gemini-3-pro-preview",  # Production model that manifests the bug
    )

    # Set up tools provider
    local_tools = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        embedding_generator=None,
    )
    composite_tools = CompositeToolsProvider(providers=[local_tools])
    tools_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_tools,
        tools_requiring_confirmation=set(),  # No confirmation needed for integration test
    )

    # Create processing service config
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone_str="UTC",
        max_history_messages=20,
        history_max_age_hours=72,
        delegation_security_level="high",
        tools_config={},
        id="test",
    )

    # Set up context providers
    # Define async function for notes provider
    async def get_db_context_for_notes() -> DatabaseContext:
        return get_db_context(engine=db_engine)

    calendar_provider = CalendarContextProvider(
        calendar_config={},  # type: ignore[arg-type]
        timezone_str=config.timezone_str,
        prompts=config.prompts,
    )
    notes_provider = NotesContextProvider(
        get_db_context_func=get_db_context_for_notes,
        prompts=config.prompts,
    )
    users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={},
        prompts=config.prompts,
    )

    context_providers = [calendar_provider, notes_provider, users_provider]

    # Create processing service
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=config,
        context_providers=context_providers,
        server_url="http://test",
        app_config=AppConfig(),
    )

    yield processing_service


@pytest_asyncio.fixture
async def llm_integration_app(
    db_engine: AsyncEngine,
    llm_integration_processing_service: ProcessingService,
    attachment_registry_fixture: AttachmentRegistry,
) -> "FastAPI":
    """FastAPI app configured for LLM integration testing."""
    # Configure app state
    app.state.database_engine = db_engine
    app.state.processing_service = llm_integration_processing_service
    app.state.attachment_registry = attachment_registry_fixture
    app.state.web_chat_interface = WebChatInterface(db_engine)
    app.state.debug_mode = True  # Enable debug mode to get full error tracebacks

    return app


@pytest_asyncio.fixture
async def llm_integration_client(
    llm_integration_app: "FastAPI",
) -> AsyncGenerator[AsyncClient]:
    """HTTP client for LLM integration testing."""
    async with AsyncClient(
        transport=ASGITransport(app=llm_integration_app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.llm_integration
async def test_multiturn_conversation_with_tool_calls_preserves_thought_signatures(
    llm_integration_client: AsyncClient,
) -> None:
    """Test multi-turn conversation with tool calls works correctly.

    User journey:
    1. Ask a question requiring calculation
    2. Get answer (tool is used automatically)
    3. Ask another question
    4. Should work without errors

    This makes real API calls to verify the end-to-end flow works.
    """
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping test - GEMINI_API_KEY not available")

    # Turn 1: Ask question requiring tool use
    response1 = await llm_integration_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Use Python to calculate 5 + 5. Use the execute_script tool."},
    )
    assert response1.status_code == 200, f"Turn 1 failed: {response1.text}"
    data1 = response1.json()
    assert "tool_calls" in data1, "Expected tool_calls in Turn 1"

    # Turn 2: Ask follow-up question
    response2 = await llm_integration_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Now calculate 10 + 10, also using Python."},
    )
    assert response2.status_code == 200, f"Turn 2 failed: {response2.text}"


@pytest.mark.llm_integration
@pytest.mark.asyncio
async def test_streaming_multiturn_with_tool_calls_reproduces_bug(
    llm_integration_client: AsyncClient,
) -> None:
    """Test STREAMING multi-turn conversation - should reproduce the bug.

    The web UI uses streaming, and this should fail with "Corrupted thought signature"
    on turn 2 because the streaming code path still has the base64 encoding bug.
    """
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping test - GEMINI_API_KEY not available")

    # Turn 1: Ask question requiring tool use (streaming)
    response1_chunks = []
    tool_call_seen = False
    async with llm_integration_client.stream(
        "POST",
        "/api/v1/chat/send_message_stream",
        json={"prompt": "Use Python to calculate 5 + 5. Use the execute_script tool."},
    ) as response1:
        assert response1.status_code == 200, f"Turn 1 failed: {await response1.aread()}"

        async for line in response1.aiter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() and data_str != "[DONE]":
                    response1_chunks.append(data_str)
                    # Check if this chunk indicates a tool call
                    if "tool_calls" in data_str or "execute_script" in data_str:
                        tool_call_seen = True

    # Verify that turn 1 actually used a tool call
    print(f"\nTurn 1: Received {len(response1_chunks)} chunks")
    print(f"Tool call seen: {tool_call_seen}")
    if not tool_call_seen:
        pytest.skip("LLM didn't use tool in turn 1 - can't test thought signature bug")

    # Turn 2: Ask follow-up (streaming) - THIS SHOULD FAIL WITH CORRUPTED SIGNATURE
    async with llm_integration_client.stream(
        "POST",
        "/api/v1/chat/send_message_stream",
        json={"prompt": "Now calculate 10 + 10 using Python."},
    ) as response2:
        # This is where the bug manifests
        assert response2.status_code == 200, (
            f"Turn 2 failed (BUG REPRODUCED - corrupted thought signature): "
            f"{await response2.aread()}"
        )


@pytest.mark.llm_integration
async def test_multiturn_conversation_non_streaming_preserves_thought_signatures(
    llm_integration_client: AsyncClient,
) -> None:
    """Test multi-turn conversation via non-streaming HTTP API.

    This tests the full ProcessingService stack including database round-trip,
    catching bugs that direct LLM client tests miss.

    Note: Even though the HTTP endpoint is non-streaming (returns full JSON),
    ProcessingService uses streaming internally, so VCR cannot be used.
    Requires a real GEMINI_API_KEY to run.

    If thought_signature is not preserved through the database, the second
    request would fail with: "Function call is missing a thought_signature"
    """
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Skipping test - GEMINI_API_KEY not available")

    # Turn 1: Send message requiring tool call
    response1 = await llm_integration_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "use Python to calculate 1 + 1"},
    )
    assert response1.status_code == 200

    # Turn 2: Continue conversation - would fail without provider_metadata fix
    # This verifies thought signatures survived the database round-trip
    response2 = await llm_integration_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "thanks!"},
    )
    assert response2.status_code == 200
