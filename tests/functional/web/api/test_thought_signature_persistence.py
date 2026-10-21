"""Replay-backed HTTP tests for persisted Gemini thought signatures and turns."""

import asyncio
import json
import os
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.context_providers import (
    KnownUsersContextProvider,
    NotesContextProvider,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import AssistantMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.tools.infrastructure import (
    CompositeToolsProvider,
    LocalToolsProvider,
    PolicyEnforcingToolsProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.policy import (
    PolicyEngine,
    ToolPolicyConfig,
    ToolPolicyDecision,
)
from family_assistant.tools.types import ToolDefinition, ToolResult
from family_assistant.utils.clock import MockClock
from family_assistant.web.app_creator import app
from family_assistant.web.web_chat_interface import WebChatInterface

if TYPE_CHECKING:
    from fastapi import FastAPI

# Record/replay matches on the request body, which includes every tool
# definition, so the replay tests use a tool defined here rather than a
# production tool whose description changes whenever that tool is edited.
_REPLAY_TOOL_NAME = "add_numbers"


async def _add_numbers(a: int, b: int) -> ToolResult:
    return ToolResult(text=str(a + b))


_REPLAY_REGISTRATIONS = [
    ToolRegistration(
        definition=cast(
            "ToolDefinition",
            {
                "type": "function",
                "function": {
                    "name": _REPLAY_TOOL_NAME,
                    "description": "Add two integers and return their sum.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {"type": "integer", "description": "First addend."},
                            "b": {"type": "integer", "description": "Second addend."},
                        },
                        "required": ["a", "b"],
                    },
                },
            },
        ),
        implementation=_add_numbers,
        metadata=make_local_tool_metadata([
            ToolTag.READ_ONLY,
            ToolTag.OUTPUT_TRUSTED,
        ]),
    )
]


GEMINI_REPLAY_DIR = "tests/cassettes/gemini"


class _SSEToolFunction(TypedDict):
    name: str


class _SSEToolCall(TypedDict):
    function: _SSEToolFunction


class _SSEData(TypedDict, total=False):
    turn_id: str
    status: str
    content: str
    tool_call: _SSEToolCall


class _SSEEvent(TypedDict):
    type: str
    data: _SSEData


def _replay_file_path(module_name: str, test_name: str) -> Path:
    return Path(GEMINI_REPLAY_DIR) / module_name / test_name / "mldev.json"


@pytest.fixture
def gemini_http_api_debug_config(
    request: pytest.FixtureRequest, llm_record_mode: str
) -> dict[str, str | None]:
    """Build Google SDK replay config for HTTP API tests."""
    module_name = request.node.module.__name__.replace("tests.", "")
    test_name = re.sub(r"\[\d+-(sqlite|postgres)\]$", r"[\1]", request.node.name)
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
        history_budget_chars=100_000,
        history_max_age_hours=72,
        delegation_security_level=DelegationSecurityLevel.CONFIRM,
        tools_config=ToolsConfig(),
        id="test",
    )

    # Set up context providers
    # Define async function for notes provider
    def get_db_context_for_notes() -> Database:
        return Database(engine=db_engine)

    notes_provider = NotesContextProvider(
        get_db_context_func=get_db_context_for_notes,
        prompts=config.prompts,
        read_policy=NoteReadPolicy.UNRESTRICTED,
    )
    users_provider = KnownUsersContextProvider(
        chat_id_to_name_map={},
        prompts=config.prompts,
    )

    context_providers = [notes_provider, users_provider]

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


async def _collect_turn_events(
    client: AsyncClient, conversation_id: str, from_seq: int, turn_id: str
) -> list[_SSEEvent]:
    events: list[_SSEEvent] = []
    event_type: str | None = None
    async with asyncio.timeout(30):
        async with client.stream(
            "GET",
            f"/api/v1/chat/conversations/{conversation_id}/stream",
            params={"from_seq": from_seq},
        ) as response:
            assert response.status_code == 200, await response.aread()
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:") and event_type:
                    event = _SSEEvent(
                        type=event_type, data=cast("_SSEData", json.loads(line[5:]))
                    )
                    events.append(event)
                    if (
                        event_type == "turn_ended"
                        and event["data"].get("turn_id") == turn_id
                    ):
                        return events
                    event_type = None
    raise AssertionError(f"No turn_ended event for {turn_id}: {events}")


@pytest.mark.llm_integration
async def test_multiturn_conversation_with_tool_calls_preserves_thought_signatures(
    llm_integration_client: AsyncClient,
    db_engine: AsyncEngine,
) -> None:
    """An API turn persists Gemini's tool call and its opaque signature."""
    response1 = await llm_integration_client.post(
        "/api/v1/chat/send_message",
        json={"prompt": "Calculate 5 + 5. Use the add_numbers tool."},
    )
    assert response1.status_code == 200, f"Turn 1 failed: {response1.text}"
    data1 = response1.json()
    assert data1["reply"].strip()

    messages = await Database(engine=db_engine).message_history.get_recent(
        interface_type="api",
        conversation_id=data1["conversation_id"],
        limit=20,
        current_time=datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC),
    )
    assert any(
        isinstance(message, AssistantMessage)
        and message.tool_calls
        and any(
            call.function.name == _REPLAY_TOOL_NAME
            and isinstance(call.provider_metadata, GeminiProviderMetadata)
            and call.provider_metadata.thought_signature is not None
            for call in message.tool_calls
        )
        for message in messages
    )


@pytest.mark.llm_integration
@pytest.mark.asyncio
async def test_streaming_multiturn_with_tool_calls_reproduces_bug(
    llm_integration_client: AsyncClient,
) -> None:
    """A streaming follow-up completes after a signed tool call."""
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
            "prompt": "Calculate 5 + 5. Use the add_numbers tool.",
        },
    )
    assert post1.status_code == 200, f"Turn 1 start failed: {post1.text}"

    turn1_events = await _collect_turn_events(
        llm_integration_client, conversation_id, 0, turn1_id
    )
    assert any(
        event["type"] == "tool_call"
        and (tool_call := event["data"].get("tool_call")) is not None
        and tool_call["function"]["name"] == _REPLAY_TOOL_NAME
        for event in turn1_events
    )
    assert (
        next(event for event in turn1_events if event["type"] == "turn_ended")[
            "data"
        ].get("status")
        == "complete"
    )

    # Turn 2: Ask a follow-up in the same conversation.
    turn2_id = str(uuid.uuid4())
    post2 = await llm_integration_client.post(
        "/api/v1/chat/turns",
        json={
            "turn_id": turn2_id,
            "conversation_id": conversation_id,
            "prompt": "Now calculate 10 + 10 the same way.",
        },
    )
    assert post2.status_code == 200, f"Turn 2 start failed: {post2.text}"
    turn2_events = await _collect_turn_events(
        llm_integration_client, conversation_id, post2.json()["first_seq"], turn2_id
    )
    assert (
        next(event for event in turn2_events if event["type"] == "turn_ended")[
            "data"
        ].get("status")
        == "complete"
    )
    assert "".join(
        event["data"].get("content", "")
        for event in turn2_events
        if event["type"] == "text"
    ).strip()
