"""Test Home Assistant call_home_assistant_action tool."""

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant_api.errors import HomeassistantAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
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
from family_assistant.tools.home_assistant import call_home_assistant_action_tool
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

TEST_CHAT_ID = "ha_call_action_test_123"
TEST_USER_NAME = "HACallActionTestUser"
TEST_TIMEZONE_STR = "UTC"


def _make_exec_context(
    ha_client: object | None,
    db_context: DatabaseContext | None = None,
) -> ToolExecutionContext:
    """Build a minimal ToolExecutionContext for direct tool invocation."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id=TEST_CHAT_ID,
        user_name=TEST_USER_NAME,
        turn_id=None,
        db_context=db_context,  # type: ignore[arg-type]
        processing_service=None,
        clock=None,
        home_assistant_client=ha_client,  # type: ignore[arg-type]
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo(TEST_TIMEZONE_STR),
    )


@pytest.mark.asyncio
async def test_call_action_tool_invokes_wrapper_with_service_data() -> None:
    """Direct tool call passes domain/action/service_data through to the wrapper."""
    mock_ha_client = MagicMock()
    mock_ha_client.async_call_action = AsyncMock(
        return_value={
            "changed_states": [
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"brightness": 191},
                }
            ],
            "response": {},
        }
    )

    exec_context = _make_exec_context(mock_ha_client)
    result = await call_home_assistant_action_tool(
        exec_context=exec_context,
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen", "brightness_pct": 75},
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert "light.turn_on" in result.text
    assert "light.kitchen=on" in result.text

    data = result.get_data()
    assert isinstance(data, dict)
    assert data["domain"] == "light"
    assert data["action"] == "turn_on"
    assert data["changed_states"][0]["entity_id"] == "light.kitchen"
    assert "response" not in data  # not requested

    mock_ha_client.async_call_action.assert_awaited_once_with(
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen", "brightness_pct": 75},
        return_response=False,
    )


@pytest.mark.asyncio
async def test_call_action_tool_returns_response_when_requested() -> None:
    """When return_response=True, the action response payload is included."""
    mock_ha_client = MagicMock()
    mock_ha_client.async_call_action = AsyncMock(
        return_value={
            "changed_states": [],
            "response": {"events": [{"summary": "Dentist", "start": "10:00"}]},
        }
    )

    exec_context = _make_exec_context(mock_ha_client)
    result = await call_home_assistant_action_tool(
        exec_context=exec_context,
        domain="calendar",
        action="get_events",
        service_data={"entity_id": "calendar.family"},
        return_response=True,
    )

    assert isinstance(result, ToolResult)
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["response"] == {"events": [{"summary": "Dentist", "start": "10:00"}]}
    assert "no state changes reported" in (result.text or "")

    mock_ha_client.async_call_action.assert_awaited_once_with(
        domain="calendar",
        action="get_events",
        service_data={"entity_id": "calendar.family"},
        return_response=True,
    )


@pytest.mark.asyncio
async def test_call_action_tool_without_ha_client_returns_error() -> None:
    """If no HA client is configured, return a descriptive error."""
    exec_context = _make_exec_context(ha_client=None)
    result = await call_home_assistant_action_tool(
        exec_context=exec_context,
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen"},
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert "not configured" in result.text


@pytest.mark.asyncio
async def test_call_action_tool_handles_api_error() -> None:
    """API errors from Home Assistant are surfaced in the tool result."""
    mock_ha_client = MagicMock()
    mock_ha_client.async_call_action = AsyncMock(
        side_effect=HomeassistantAPIError("Bad Request: unknown action")
    )

    exec_context = _make_exec_context(mock_ha_client)
    result = await call_home_assistant_action_tool(
        exec_context=exec_context,
        domain="light",
        action="bogus_action",
        service_data={"entity_id": "light.kitchen"},
    )

    assert isinstance(result, ToolResult)
    assert result.text is not None
    assert "Error" in result.text
    assert "light.bogus_action" in result.text
    assert "unknown action" in result.text


@pytest.mark.asyncio
async def test_call_home_assistant_action_via_llm_flow(
    db_engine: AsyncEngine,
) -> None:
    """End-to-end: LLM picks the action tool, the mocked client gets invoked,
    and the LLM receives the changed-state summary in the tool response.
    """
    logger.info("\n--- Test: Call Home Assistant Action via LLM Flow ---")

    mock_ha_client = MagicMock()
    mock_ha_client.async_call_action = AsyncMock(
        return_value={
            "changed_states": [
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"brightness": 191},
                }
            ],
            "response": {},
        }
    )

    tool_call_id = f"call_ha_action_{uuid.uuid4()}"

    def call_action_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "turn on the kitchen light" in last_text and kwargs.get("tools") is not None
        )

    call_action_response = MockLLMOutput(
        content="Sure, I'll turn on the kitchen light.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="call_home_assistant_action",
                    arguments=json.dumps({
                        "domain": "light",
                        "action": "turn_on",
                        "service_data": {
                            "entity_id": "light.kitchen",
                            "brightness_pct": 75,
                        },
                    }),
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
            and "light.kitchen" in content
        )

    final_llm_response = MockLLMOutput(
        content="Done — kitchen light is now on.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (call_action_matcher, call_action_response),
            (final_response_matcher, final_llm_response),
        ]
    )

    dummy_prompts = {
        "system_prompt": "You are a helpful assistant. Current time: {current_time}"
    }

    enabled_tools = ["call_home_assistant_action"]
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
        id="test_ha_call_action_profile",
        prompts=dummy_prompts,
        timezone=ZoneInfo(TEST_TIMEZONE_STR),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
    )

    processing_service.home_assistant_client = mock_ha_client

    user_message = "Turn on the kitchen light at 75% brightness"
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg_ha_call_action_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error = result.error_traceback

    assert error is None, f"Error during interaction: {error}"
    assert final_reply and "kitchen light" in final_reply.lower(), (
        f"Expected confirmation in reply: '{final_reply}'"
    )

    mock_ha_client.async_call_action.assert_awaited_once_with(
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen", "brightness_pct": 75},
        return_response=False,
    )

    logger.info("Test Call Home Assistant Action via LLM Flow PASSED.")
