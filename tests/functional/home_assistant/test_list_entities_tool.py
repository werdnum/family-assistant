"""Test Home Assistant list entities tool."""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import LLMInterface, ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
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
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

TEST_CHAT_ID = "ha_list_entities_test_123"
TEST_USER_NAME = "HAListTestUser"
TEST_TIMEZONE_STR = "UTC"


@pytest.mark.asyncio
async def test_list_home_assistant_entities_with_filter(
    db_engine: AsyncEngine,
) -> None:
    """
    Test listing Home Assistant entities with entity_id filter.
    1. User asks for temperature sensors
    2. LLM decides to use list_home_assistant_entities tool
    3. Tool executes with mocked HA client, returns filtered entities
    4. LLM receives results and responds to user
    """
    logger.info("\n--- Test: List Home Assistant Entities With Filter ---")

    # Create mock entity list
    mock_entities = [
        {
            "entity_id": "sensor.living_room_temperature",
            "name": "Living Room Temperature",
            "area_name": "Living Room",
            "device_id": "abc123",
            "device_name": "Climate Sensor",
        },
        {
            "entity_id": "sensor.bedroom_temperature",
            "name": "Bedroom Temperature",
            "area_name": "Bedroom",
            "device_id": "def456",
            "device_name": "Climate Sensor",
        },
        {
            "entity_id": "light.kitchen",
            "name": "Kitchen Light",
            "area_name": "Kitchen",
            "device_id": "ghi789",
            "device_name": "Smart Light",
        },
    ]

    # Create mock Home Assistant client
    mock_ha_client = MagicMock()
    mock_ha_client.async_get_entity_list_with_metadata = AsyncMock(
        return_value=mock_entities
    )

    tool_call_id = f"call_ha_list_{uuid.uuid4()}"

    # --- LLM Rules ---
    def list_entities_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "temperature" in last_text
            and "sensors" in last_text
            and kwargs.get("tools") is not None
        )

    list_entities_response = MockLLMOutput(
        content="I'll find the temperature sensors for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="list_home_assistant_entities",
                    arguments=json.dumps({"entity_id_filter": "temperature"}),
                ),
            )
        ],
    )

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        content = last_message.content or ""
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id
            and "sensor.living_room_temperature" in content
            and "sensor.bedroom_temperature" in content
        )

    final_llm_response = MockLLMOutput(
        content="I found 2 temperature sensors:\n"
        "1. Living Room Temperature (sensor.living_room_temperature)\n"
        "2. Bedroom Temperature (sensor.bedroom_temperature)",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (list_entities_matcher, list_entities_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    # --- Setup ProcessingService ---
    dummy_prompts = {
        "system_prompt": "You are a helpful assistant. Current time: {current_time}"
    }

    enabled_tools = ["list_home_assistant_entities"]
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
        id="test_ha_list_entities_profile",
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "Show me all temperature sensors"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_list_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during interaction: {error}"
    assert final_reply, "No reply received"
    assert "Living Room Temperature" in final_reply, "Expected entity not in reply"
    assert "Bedroom Temperature" in final_reply, "Expected entity not in reply"

    # Verify the mock was called correctly
    mock_ha_client.async_get_entity_list_with_metadata.assert_awaited()

    logger.info("Test List Home Assistant Entities With Filter PASSED.")


@pytest.mark.asyncio
async def test_list_home_assistant_entities_with_area_filter(
    db_engine: AsyncEngine,
) -> None:
    """
    Test listing Home Assistant entities filtered by area.
    """
    logger.info("\n--- Test: List Home Assistant Entities With Area Filter ---")

    # Create mock entity list
    mock_entities = [
        {
            "entity_id": "sensor.pool_temperature",
            "name": "Pool Temperature",
            "area_name": "Pool",
            "device_id": "pool123",
            "device_name": "Pool Sensor",
        },
        {
            "entity_id": "switch.pool_pump",
            "name": "Pool Pump",
            "area_name": "Pool",
            "device_id": "pool456",
            "device_name": "Pool Controller",
        },
        {
            "entity_id": "light.living_room",
            "name": "Living Room Light",
            "area_name": "Living Room",
            "device_id": "light123",
            "device_name": "Smart Light",
        },
    ]

    mock_ha_client = MagicMock()
    mock_ha_client.async_get_entity_list_with_metadata = AsyncMock(
        return_value=mock_entities
    )

    tool_call_id = f"call_ha_area_{uuid.uuid4()}"

    # --- LLM Rules ---
    def area_filter_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "pool" in last_text and kwargs.get("tools") is not None

    area_filter_response = MockLLMOutput(
        content="I'll find devices in the pool area.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="list_home_assistant_entities",
                    arguments=json.dumps({"area_filter": "pool"}),
                ),
            )
        ],
    )

    def pool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        content = last_message.content or ""
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id
            and "sensor.pool_temperature" in content
            and "switch.pool_pump" in content
        )

    pool_result_response = MockLLMOutput(
        content="I found 2 devices in the pool area:\n"
        "1. Pool Temperature sensor\n"
        "2. Pool Pump switch",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (area_filter_matcher, area_filter_response),
            (pool_result_matcher, pool_result_response),
        ]
    )

    # --- Setup ProcessingService ---
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["list_home_assistant_entities"]
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
        id="test_ha_area_filter_profile",
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # Inject the mock HA client
    processing_service.home_assistant_client = mock_ha_client

    # --- Simulate User Interaction ---
    user_message = "What devices are in the pool area?"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_area_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during interaction: {error}"
    assert final_reply, "No reply received"
    assert "pool" in final_reply.lower(), "Expected pool devices in reply"

    logger.info("Test List Home Assistant Entities With Area Filter PASSED.")


@pytest.mark.asyncio
async def test_list_home_assistant_entities_no_client(
    db_engine: AsyncEngine,
) -> None:
    """
    Test error handling when Home Assistant client is not available.
    """
    logger.info("\n--- Test: List Home Assistant Entities No Client ---")

    tool_call_id = f"call_ha_no_client_{uuid.uuid4()}"

    # --- LLM Rules ---
    def list_entities_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "entities" in last_text and kwargs.get("tools") is not None

    list_entities_response = MockLLMOutput(
        content="I'll list the entities.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="list_home_assistant_entities",
                    arguments=json.dumps({}),
                ),
            )
        ],
    )

    def error_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id
            and "Error:" in (last_message.content or "")
            and "not configured" in (last_message.content or "")
        )

    error_llm_response = MockLLMOutput(
        content="I'm sorry, but Home Assistant integration is not currently available.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (list_entities_matcher, list_entities_response),
            (error_response_matcher, error_llm_response),
        ]
    )

    # --- Setup ProcessingService without HA client ---
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["list_home_assistant_entities"]
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
        id="test_ha_no_client_profile",
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # Don't set home_assistant_client - it should be None

    # --- Simulate User Interaction ---
    user_message = "List all entities"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_no_client_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "not currently available" in final_reply, (
        f"Expected error message not in reply: '{final_reply}'"
    )

    logger.info("Test List Home Assistant Entities No Client PASSED.")
