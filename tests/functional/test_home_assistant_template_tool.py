"""Test Home Assistant template rendering tool."""

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.llm import ToolCallFunction, ToolCallItem
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

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

TEST_CHAT_ID = "ha_template_test_123"
TEST_USER_NAME = "HATemplateTestUser"
TEST_TIMEZONE_STR = "UTC"


@pytest.mark.asyncio
async def test_render_home_assistant_template_success(
    test_db_engine: AsyncEngine,
) -> None:
    """
    Test successful rendering of a Home Assistant template.
    1. User asks for current temperature from a sensor
    2. LLM decides to use render_home_assistant_template tool
    3. Tool executes with mocked HA client
    4. LLM receives result and responds to user
    """
    logger.info("\n--- Test: Render Home Assistant Template Success ---")

    # The template we'll render
    template_str = "{{ states('sensor.living_room_temperature') }}"
    expected_result = "22.5"

    # Create mock Home Assistant client
    mock_ha_client = MagicMock()
    mock_ha_client.async_get_rendered_template = AsyncMock(return_value=expected_result)

    tool_call_id = f"call_ha_template_{uuid.uuid4()}"

    # --- LLM Rules ---
    def render_template_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "temperature" in last_text
            and "living room" in last_text
            and kwargs.get("tools") is not None
        )

    render_template_response = MockLLMOutput(
        content="I'll check the living room temperature for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="render_home_assistant_template",
                    arguments=json.dumps({"template": template_str}),
                ),
            )
        ],
    )

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and expected_result in last_message.get("content", "")
        )

    final_llm_response = MockLLMOutput(
        content=f"The living room temperature is {expected_result}°C.", tool_calls=None
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (render_template_matcher, render_template_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    # --- Setup ProcessingService ---
    dummy_prompts = {
        "system_prompt": "You are a helpful assistant. Current time: {current_time}"
    }

    # Filter tools to only include our HA template tool
    enabled_tools = ["render_home_assistant_template"]
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
        id="test_ha_template_profile",
        prompts=dummy_prompts,
        calendar_config={},
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
    user_message = "What's the current temperature in the living room?"
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_template_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and expected_result in final_reply, (
        f"Expected temperature '{expected_result}' not in reply: '{final_reply}'"
    )

    # Verify the mock was called correctly
    mock_ha_client.async_get_rendered_template.assert_awaited_once_with(
        template=template_str
    )

    logger.info("Test Render Home Assistant Template Success PASSED.")


@pytest.mark.asyncio
async def test_render_home_assistant_template_no_client(
    test_db_engine: AsyncEngine,
) -> None:
    """
    Test error handling when Home Assistant client is not available.
    """
    logger.info("\n--- Test: Render Home Assistant Template No Client ---")

    tool_call_id = f"call_ha_no_client_{uuid.uuid4()}"

    # --- LLM Rules ---
    def check_sensor_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "sensor status" in last_text and kwargs.get("tools") is not None

    check_sensor_response = MockLLMOutput(
        content="I'll check the sensor status.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="render_home_assistant_template",
                    arguments=json.dumps({"template": "{{ states('sensor.test') }}"}),
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
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "Error:" in last_message.get("content", "")
            and "not configured" in last_message.get("content", "")
        )

    error_llm_response = MockLLMOutput(
        content="I'm sorry, but Home Assistant integration is not currently available.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (check_sensor_matcher, check_sensor_response),
            (error_response_matcher, error_llm_response),
        ]
    )

    # --- Setup ProcessingService without HA client ---
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["render_home_assistant_template"]
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
        calendar_config={},
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
    user_message = "Check the sensor status"
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_no_client_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "not currently available" in final_reply, (
        f"Expected error message not in reply: '{final_reply}'"
    )

    logger.info("Test Render Home Assistant Template No Client PASSED.")


@pytest.mark.asyncio
async def test_render_home_assistant_template_complex(
    test_db_engine: AsyncEngine,
) -> None:
    """
    Test rendering a complex Home Assistant template with multiple entities and calculations.
    """
    logger.info("\n--- Test: Render Complex Home Assistant Template ---")

    # Complex template with calculations
    complex_template = """
{%- set temp = states('sensor.outside_temperature') | float -%}
{%- set humidity = states('sensor.outside_humidity') | float -%}
{%- set feels_like = temp - 0.55 * (1 - humidity/100) * (temp - 14) -%}
Temperature: {{ temp }}°C
Humidity: {{ humidity }}%
Feels like: {{ feels_like | round(1) }}°C
Status: {% if temp > 25 %}Hot{% elif temp < 10 %}Cold{% else %}Comfortable{% endif %}
"""

    expected_result = """Temperature: 18.5°C
Humidity: 65%
Feels like: 17.4°C
Status: Comfortable"""

    # Create mock Home Assistant client
    mock_ha_client = MagicMock()
    mock_ha_client.async_get_rendered_template = AsyncMock(return_value=expected_result)

    tool_call_id = f"call_ha_complex_{uuid.uuid4()}"

    # --- LLM Rules ---
    def weather_info_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "weather" in last_text
            and "feels like" in last_text
            and kwargs.get("tools") is not None
        )

    weather_info_response = MockLLMOutput(
        content="I'll calculate the weather information including the 'feels like' temperature.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="render_home_assistant_template",
                    arguments=json.dumps({"template": complex_template}),
                ),
            )
        ],
    )

    def weather_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "18.5°C" in last_message.get("content", "")
            and "Comfortable" in last_message.get("content", "")
        )

    weather_result_response = MockLLMOutput(
        content="Here's the current weather information:\n\n"
        "- Temperature: 18.5°C\n"
        "- Humidity: 65%\n"
        "- Feels like: 17.4°C\n"
        "- Status: Comfortable\n\n"
        "The weather is quite pleasant right now!",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (weather_info_matcher, weather_info_response),
            (weather_result_matcher, weather_result_response),
        ]
    )

    # --- Setup ProcessingService ---
    dummy_prompts = {"system_prompt": "You are a helpful weather assistant."}

    enabled_tools = ["render_home_assistant_template"]
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
        id="test_ha_complex_profile",
        prompts=dummy_prompts,
        calendar_config={},
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
    user_message = (
        "What's the weather like outside? Include the feels like temperature."
    )
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_complex_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply, "No reply received"
    assert "18.5°C" in final_reply, "Temperature not in reply"
    assert "17.4°C" in final_reply, "Feels like temperature not in reply"
    assert "Comfortable" in final_reply, "Status not in reply"

    # Verify the template was passed correctly
    mock_ha_client.async_get_rendered_template.assert_awaited_once()
    call_args = mock_ha_client.async_get_rendered_template.call_args
    assert call_args[1]["template"].strip() == complex_template.strip()

    logger.info("Test Render Complex Home Assistant Template PASSED.")


@pytest.mark.asyncio
async def test_render_home_assistant_template_api_error(
    test_db_engine: AsyncEngine,
) -> None:
    """
    Test handling of Home Assistant API errors.
    """
    logger.info("\n--- Test: Render Home Assistant Template API Error ---")

    # Import the error type
    from homeassistant_api.errors import HomeassistantAPIError

    # Create mock Home Assistant client that raises an error
    mock_ha_client = MagicMock()
    mock_ha_client.async_get_rendered_template = AsyncMock(
        side_effect=HomeassistantAPIError("Connection timeout")
    )

    tool_call_id = f"call_ha_error_{uuid.uuid4()}"

    # --- LLM Rules ---
    def check_state_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "alarm status" in last_text and kwargs.get("tools") is not None

    check_state_response = MockLLMOutput(
        content="I'll check the alarm status for you.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="render_home_assistant_template",
                    arguments=json.dumps({
                        "template": "{{ states('alarm_control_panel.home') }}"
                    }),
                ),
            )
        ],
    )

    def api_error_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "Error:" in last_message.get("content", "")
            and "Connection timeout" in last_message.get("content", "")
        )

    api_error_response = MockLLMOutput(
        content="I'm having trouble connecting to Home Assistant right now. "
        "There seems to be a connection timeout. Please try again later.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (check_state_matcher, check_state_response),
            (api_error_matcher, api_error_response),
        ]
    )

    # --- Setup ProcessingService ---
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    enabled_tools = ["render_home_assistant_template"]
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
        id="test_ha_api_error_profile",
        prompts=dummy_prompts,
        calendar_config={},
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
    user_message = "What's the alarm status?"
    async with DatabaseContext(engine=test_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_api_error_test",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "connection timeout" in final_reply.lower(), (
        f"Expected timeout error message not in reply: '{final_reply}'"
    )

    logger.info("Test Render Home Assistant Template API Error PASSED.")
