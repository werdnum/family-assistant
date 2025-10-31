"""
Tests for Home Assistant state history download tool.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from homeassistant_api.models.history import History
from homeassistant_api.models.states import State

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from family_assistant.tools.home_assistant import download_state_history_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.llm import LLMInterface


TEST_CHAT_ID = "ha_history_test_chat"
TEST_USER_NAME = "ha_history_test_user"
TEST_TIMEZONE_STR = "UTC"


async def create_processing_service_for_history_tests(
    llm_client: LLMInterface, profile_id: str
) -> ProcessingService:
    """Helper function to create a ProcessingService for history tests."""
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["download_state_history"]
    filtered_definitions = [
        tool
        for tool in local_tools_definition
        if tool.get("function", {}).get("name") in enabled_tools
    ]
    filtered_implementations = {
        name: impl
        for name, impl in local_tool_implementations.items()
        if name in enabled_tools
    }

    local_provider = LocalToolsProvider(
        definitions=filtered_definitions,
        implementations=filtered_implementations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    service_config = ProcessingServiceConfig(
        id=profile_id,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )

    return ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config={},
    )


@pytest.mark.asyncio
async def test_download_state_history_success(
    db_engine: AsyncEngine,
) -> None:
    """
    Test successful download of state history.
    """
    entity_ids = ["sensor.temperature", "sensor.humidity"]
    start_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    end_time = datetime.now(UTC).isoformat()

    # Create mock Home Assistant client wrapper
    mock_ha_client = MagicMock()

    # Mock async generator for history data using library's models
    async def mock_history_generator() -> AsyncGenerator[History]:
        # Mock history for temperature sensor
        temp_states = [
            State(
                entity_id="sensor.temperature",
                state="22.5",
                attributes={
                    "unit_of_measurement": "°C",
                    "friendly_name": "Temperature",
                },
                last_changed=datetime.now(UTC) - timedelta(hours=1),
                last_updated=datetime.now(UTC) - timedelta(hours=1),
                context=None,
            ),
            State(
                entity_id="sensor.temperature",
                state="23.0",
                attributes={
                    "unit_of_measurement": "°C",
                    "friendly_name": "Temperature",
                },
                last_changed=datetime.now(UTC) - timedelta(minutes=30),
                last_updated=datetime.now(UTC) - timedelta(minutes=30),
                context=None,
            ),
        ]
        yield History(states=tuple(temp_states))

        # Mock history for humidity sensor
        humidity_states = [
            State(
                entity_id="sensor.humidity",
                state="65",
                attributes={"unit_of_measurement": "%", "friendly_name": "Humidity"},
                last_changed=datetime.now(UTC) - timedelta(hours=1),
                last_updated=datetime.now(UTC) - timedelta(hours=1),
                context=None,
            ),
        ]
        yield History(states=tuple(humidity_states))

    # Set the mock to return the generator when called
    # Mock the inner _client's async_get_entity_histories method
    mock_ha_client._client = MagicMock()
    mock_ha_client._client.async_get_entity_histories = MagicMock(
        return_value=mock_history_generator()
    )

    # Create proper async mock for async_get_entity
    async def mock_get_entity(entity_id: str) -> MagicMock:
        mock_entity = MagicMock()
        mock_entity.entity_id = entity_id
        return mock_entity

    mock_ha_client._client.async_get_entity = mock_get_entity

    tool_call_id = f"call_history_{uuid.uuid4()}"

    # --- LLM Rules ---
    def history_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "history" in last_text
            and "temperature" in last_text
            and kwargs.get("tools") is not None
        )

    history_response = MockLLMOutput(
        content="I'll download the state history for those sensors.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="download_state_history",
                    arguments=json.dumps({
                        "entity_ids": entity_ids,
                        "start_time": start_time,
                        "end_time": end_time,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        """Verify the LLM receives the tool result with JSON attachment."""
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]

        # Check basic tool message structure
        if not (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
        ):
            return False

        # Check that the LLM receives the JSON attachment
        attachments = last_message.get("_attachments")
        if not attachments or len(attachments) == 0:
            return False

        # Verify the attachment is JSON with expected structure
        attachment = attachments[0]
        if attachment.mime_type != "application/json":
            return False

        # Parse and verify JSON structure
        try:
            data = json.loads(attachment.content.decode("utf-8"))
            return (
                "entities" in data
                and len(data["entities"]) == 2
                and "start_time" in data
                and "end_time" in data
            )
        except (json.JSONDecodeError, AttributeError):
            return False

    final_llm_response = MockLLMOutput(
        content="I've downloaded the state history data for you.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (history_matcher, history_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    # --- Setup ProcessingService ---
    processing_service = await create_processing_service_for_history_tests(
        llm_client, "test_ha_history_profile"
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "Can you download the history for temperature and humidity sensors?"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_history_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "history" in final_reply.lower(), (
        f"Expected 'history' not in reply: '{final_reply}'"
    )


@pytest.mark.asyncio
async def test_download_state_history_direct_call() -> None:
    """
    Test direct call to download_state_history_tool function.
    """
    # Create mock Home Assistant client wrapper
    mock_ha_client = MagicMock()

    # Mock async generator for history data using library's models
    async def mock_history_generator() -> AsyncGenerator[History]:
        states = [
            State(
                entity_id="light.living_room",
                state="on",
                attributes={"friendly_name": "Living Room Light"},
                last_changed=datetime.now(UTC) - timedelta(hours=1),
                last_updated=datetime.now(UTC) - timedelta(hours=1),
                context=None,
            ),
        ]
        yield History(states=tuple(states))

    # Set the mock to return the generator when called
    # Mock the inner _client's async_get_entity_histories method
    mock_ha_client._client = MagicMock()
    mock_ha_client._client.async_get_entity_histories = MagicMock(
        return_value=mock_history_generator()
    )

    # Create proper async mock for async_get_entity that returns an object with entity_id
    async def mock_get_entity(entity_id: str) -> MagicMock:
        mock_entity = MagicMock()
        mock_entity.entity_id = entity_id
        return mock_entity

    mock_ha_client._client.async_get_entity = mock_get_entity

    # Create tool execution context
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=MagicMock(),
        processing_service=None,
        clock=None,
        home_assistant_client=mock_ha_client,
        event_sources=None,
        attachment_registry=None,
    )

    # Call the tool with a specific entity_id (common case)
    result = await download_state_history_tool(
        exec_context=exec_context,
        entity_ids=["light.living_room"],
        start_time=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        end_time=datetime.now(UTC).isoformat(),
    )

    # Verify result
    assert result.attachments is not None
    assert len(result.attachments) == 1
    assert result.attachments[0].mime_type == "application/json"

    # Parse and verify JSON content
    attachment_content = result.attachments[0].content
    assert attachment_content is not None
    json_data = json.loads(attachment_content.decode("utf-8"))
    assert "entities" in json_data
    assert len(json_data["entities"]) == 1
    assert json_data["entities"][0]["entity_id"] == "light.living_room"
    assert len(json_data["entities"][0]["states"]) == 1


@pytest.mark.asyncio
async def test_download_state_history_no_client() -> None:
    """
    Test download_state_history_tool when no HA client is available.
    """
    # Create tool execution context without HA client
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=MagicMock(),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
    )

    # Call the tool
    result = await download_state_history_tool(
        exec_context=exec_context,
        entity_ids=["light.living_room"],
    )

    # Verify error message
    assert result.text is not None
    assert "Error: Home Assistant integration is not configured" in result.text


@pytest.mark.asyncio
async def test_download_state_history_empty() -> None:
    """
    Test download_state_history_tool when no history data is returned.
    """
    mock_ha_client = MagicMock()

    # Mock empty async generator that yields nothing
    async def mock_empty_generator() -> AsyncGenerator[History]:
        if False:  # pylint: disable=using-constant-test
            yield History(states=())

    # Set the mock to return the generator when called
    # Mock the inner _client's async_get_entity_histories method
    mock_ha_client._client = MagicMock()
    mock_ha_client._client.async_get_entity_histories = MagicMock(
        return_value=mock_empty_generator()
    )

    # Create proper async mock for async_get_entity
    async def mock_get_entity(entity_id: str) -> MagicMock:
        mock_entity = MagicMock()
        mock_entity.entity_id = entity_id
        return mock_entity

    mock_ha_client._client.async_get_entity = mock_get_entity

    # Create tool execution context
    exec_context = ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="test_user",
        turn_id=None,
        db_context=MagicMock(),
        processing_service=None,
        clock=None,
        home_assistant_client=mock_ha_client,
        event_sources=None,
        attachment_registry=None,
    )

    # Call the tool
    result = await download_state_history_tool(
        exec_context=exec_context,
        entity_ids=["sensor.nonexistent"],
    )

    # Verify result
    assert result.text is not None
    assert "No history data found" in result.text
