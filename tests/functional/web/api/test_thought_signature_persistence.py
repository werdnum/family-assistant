"""Integration test for thought signature persistence through HTTP API.

This test verifies that thought signatures are preserved through the entire
user journey via the HTTP API using Gemini SDK record/replay.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import (
    CalendarContextProvider,
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools import (
    LOCAL_TOOL_REGISTRATIONS,
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.utils.clock import MockClock
from family_assistant.web.app_creator import app
from family_assistant.web.web_chat_interface import WebChatInterface

if TYPE_CHECKING:
    from fastapi import FastAPI

# Stable tool set for replay-backed tests. Using an explicit allowlist prevents
# cassette breakage when unrelated tools are added to the global registry.
_REPLAY_TOOL_NAMES = frozenset({"execute_script"})
_REPLAY_REGISTRATIONS = [
    r for r in LOCAL_TOOL_REGISTRATIONS if r.name in _REPLAY_TOOL_NAMES
]


GEMINI_REPLAY_DIR = "tests/cassettes/gemini"


def _replay_file_path(module_name: str, test_name: str) -> Path:
    return Path(GEMINI_REPLAY_DIR) / module_name / test_name / "mldev.json"


@pytest.fixture
def gemini_http_api_debug_config(
    request: pytest.FixtureRequest, llm_record_mode: str
) -> dict[str, str | None]:
    """Build Google SDK replay config for HTTP API tests."""
    module_name = request.node.module.__name__.replace("tests.", "")
    test_name = request.node.name
    replay_path = _replay_file_path(module_name, test_name)
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if llm_record_mode == "replay" and not replay_path.exists():
        pytest.fail(
            f"Replay file missing for {test_name}. Record with LLM_RECORD_MODE=record."
        )

    if llm_record_mode == "record" and not api_key:
        pytest.skip(
            "Recording Gemini replays requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    if llm_record_mode == "auto" and not replay_path.exists() and not api_key:
        pytest.skip(
            "Auto-recording missing Gemini replays requires GEMINI_API_KEY or GOOGLE_API_KEY."
        )

    return {
        "client_mode": llm_record_mode,
        "replay_id": f"{module_name}/{test_name}/mldev",
        "replays_directory": GEMINI_REPLAY_DIR,
    }


@pytest_asyncio.fixture
async def llm_integration_processing_service(
    db_engine: AsyncEngine,
    gemini_http_api_debug_config: dict[str, str | None],
) -> AsyncGenerator[ProcessingService]:
    """ProcessingService with replay-backed Gemini LLM client for integration testing."""
    llm_client = GoogleGenAIClient(
        api_key=os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or "test-key",
        model="gemini-3.8-flash",  # V3 model with thought signatures, cheaper than pro
        debug_config=gemini_http_api_debug_config,
    )

    # Use a stable, explicit tool set so replay cassettes don't break when
    # unrelated tools are added to the global registry.
    local_tools = LocalToolsProvider(registrations=_REPLAY_REGISTRATIONS)
    composite_tools = CompositeToolsProvider(providers=[local_tools])
    tools_provider = PolicyEnforcingToolsProvider(
        wrapped_provider=composite_tools,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )

    # Create processing service config
    config = ProcessingServiceConfig(
        prompts={"system_prompt": "You are a helpful assistant."},
        timezone=ZoneInfo("UTC"),
        max_history_messages=20,
        history_max_age_hours=72,
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        tools_config=ToolsConfig(),
        id="test",
    )

    # Set up context providers
    # Define async function for notes provider
    def get_db_context_for_notes() -> Database:
        return Database(engine=db_engine)

    calendar_provider = CalendarContextProvider(
        calendar_config={},  # type: ignore[arg-type]
        timezone=config.timezone,
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
        # Record/replay matches on the request body, and every request carries a
        # <turn_context> block stamping the current time. A real clock makes the
        # body differ from the recording on every run, so no cassette can ever
        # replay.
        clock=MockClock(datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)),
    )

    try:
        yield processing_service
    finally:
        await llm_client.close()


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

    Runs against recorded Gemini responses so PR CI stays deterministic.
    """
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
    # Turn 1: Ask question requiring tool use (streaming).
    # Resumable-streaming flow: POST /turns to start, then stream the
    # conversation's event stream.
    turn1_id = str(uuid.uuid4())
    conversation_id = f"thought-sig-{uuid.uuid4().hex[:8]}"
    post1 = await llm_integration_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn1_id,
            "conversation_id": conversation_id,
            "prompt": "Use Python to calculate 5 + 5. Use the execute_script tool.",
        },
    )
    assert post1.status_code == 200, f"Turn 1 start failed: {post1.text}"

    response1_chunks = []
    tool_call_seen = False
    async with llm_integration_client.stream(
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
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
    turn2_id = str(uuid.uuid4())
    post2 = await llm_integration_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn2_id,
            "conversation_id": conversation_id,
            "prompt": "Now calculate 10 + 10 using Python.",
        },
    )
    assert post2.status_code == 200, f"Turn 2 start failed: {post2.text}"
    async with llm_integration_client.stream(
        "GET",
        f"/api/v1/chat/conversations/{conversation_id}/stream",
        params={"from_seq": 0},
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
    ProcessingService uses streaming internally, so this test uses the Google
    SDK's native replay support instead of VCR.py.

    If thought_signature is not preserved through the database, the second
    request would fail with: "Function call is missing a thought_signature"
    """
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
