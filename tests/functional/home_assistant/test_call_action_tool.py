"""Test Home Assistant call_home_assistant_action tool."""

import json
import logging
import uuid
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant_api.errors import HomeassistantAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
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
from family_assistant.tools.home_assistant import (
    call_home_assistant_action_tool,
    list_home_assistant_actions_tool,
)
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
    *,
    db_context: DatabaseContext,
    ha_client: HomeAssistantClientWrapper | None,
) -> ToolExecutionContext:
    """Build a minimal ToolExecutionContext for direct tool invocation."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id=TEST_CHAT_ID,
        user_name=TEST_USER_NAME,
        turn_id=None,
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=ha_client,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo(TEST_TIMEZONE_STR),
        credential_resolvers=None,
        api_backend=None,
    )


def _make_mock_ha_client(
    *,
    return_value: object | None = None,
    side_effect: BaseException | None = None,
) -> tuple[HomeAssistantClientWrapper, AsyncMock]:
    """Build a MagicMock that quacks like a HomeAssistantClientWrapper for tests.

    Returns ``(wrapper, async_call_action_mock)``. The first value is type-cast
    to ``HomeAssistantClientWrapper`` so it can be passed where the wrapper is
    expected. The second is the underlying ``AsyncMock`` for ``await`` /
    assertion access (which the wrapper's static type does not expose).
    """
    mock = MagicMock(spec=HomeAssistantClientWrapper)
    call_mock = AsyncMock(return_value=return_value, side_effect=side_effect)
    mock.async_call_action = call_mock
    return cast("HomeAssistantClientWrapper", mock), call_mock


def _make_mock_ha_client_for_catalog(
    *,
    return_value: list[dict[str, object]] | None = None,
    side_effect: BaseException | None = None,
) -> tuple[HomeAssistantClientWrapper, AsyncMock]:
    """Same as ``_make_mock_ha_client`` but stubs ``async_get_action_catalog``."""
    mock = MagicMock(spec=HomeAssistantClientWrapper)
    catalog_mock = AsyncMock(return_value=return_value, side_effect=side_effect)
    mock.async_get_action_catalog = catalog_mock
    return cast("HomeAssistantClientWrapper", mock), catalog_mock


@pytest.mark.asyncio
async def test_list_actions_tool_filters_and_summarizes(
    db_engine: AsyncEngine,
) -> None:
    """The discovery tool forwards ``domain`` to the wrapper and applies
    ``action_filter`` client-side to the returned catalog.
    """
    catalog = [
        {
            "domain": "light",
            "action": "turn_on",
            "name": "Turn on",
            "description": "Turn the light on",
            "fields": {"brightness_pct": {"selector": {"number": {}}}},
            "target": {"entity": [{"domain": ["light"]}]},
            "supports_response": False,
        },
        {
            "domain": "light",
            "action": "turn_off",
            "name": "Turn off",
            "description": "Turn the light off",
            "fields": {},
            "target": None,
            "supports_response": False,
        },
        {
            "domain": "light",
            "action": "toggle",
            "name": "Toggle",
            "description": None,
            "fields": {},
            "target": None,
            "supports_response": False,
        },
    ]
    ha_client, catalog_mock = _make_mock_ha_client_for_catalog(return_value=catalog)

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
        result = await list_home_assistant_actions_tool(
            exec_context=exec_context,
            domain="light",
            action_filter="turn_",
        )

    catalog_mock.assert_awaited_once_with(domain="light")
    data = result.get_data()
    assert isinstance(data, dict)
    assert data["total_matches"] == 2
    returned = {(e["domain"], e["action"]) for e in data["actions"]}
    assert returned == {("light", "turn_on"), ("light", "turn_off")}
    assert data["filters_applied"] == {"domain": "light", "action_filter": "turn_"}

    assert result.text is not None
    assert "light.turn_on" in result.text
    assert "light.turn_off" in result.text
    assert "light.toggle" not in result.text


@pytest.mark.asyncio
async def test_list_actions_tool_marks_response_supporting_actions(
    db_engine: AsyncEngine,
) -> None:
    """Catalog entries with ``supports_response=True`` are flagged in the text
    summary so the LLM knows to set ``return_response=true`` when calling.
    """
    catalog = [
        {
            "domain": "calendar",
            "action": "get_events",
            "name": "Get events",
            "description": "Fetch calendar events",
            "fields": {},
            "target": None,
            "supports_response": True,
        }
    ]
    ha_client, _ = _make_mock_ha_client_for_catalog(return_value=catalog)

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
        result = await list_home_assistant_actions_tool(
            exec_context=exec_context,
            domain="calendar",
        )

    assert result.text is not None
    assert "calendar.get_events" in result.text
    assert "[supports response]" in result.text


@pytest.mark.asyncio
async def test_list_actions_tool_without_ha_client_returns_error(
    db_engine: AsyncEngine,
) -> None:
    """Without an HA client the discovery tool returns the same error string
    as ``call_home_assistant_action`` so the LLM can recover identically.
    """
    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=None)
        result = await list_home_assistant_actions_tool(exec_context=exec_context)

    assert result.text is not None
    assert "not configured" in result.text


@pytest.mark.asyncio
async def test_call_action_tool_invokes_wrapper_with_service_data(
    db_engine: AsyncEngine,
) -> None:
    """Direct tool call passes domain/action/service_data through to the wrapper."""
    ha_client, call_action_mock = _make_mock_ha_client(
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

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
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

    call_action_mock.assert_awaited_once_with(
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen", "brightness_pct": 75},
        return_response=False,
    )


@pytest.mark.asyncio
async def test_call_action_tool_returns_response_when_requested(
    db_engine: AsyncEngine,
) -> None:
    """When return_response=True, the action response payload is included."""
    ha_client, call_action_mock = _make_mock_ha_client(
        return_value={
            "changed_states": [],
            "response": {"events": [{"summary": "Dentist", "start": "10:00"}]},
        }
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
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

    call_action_mock.assert_awaited_once_with(
        domain="calendar",
        action="get_events",
        service_data={"entity_id": "calendar.family"},
        return_response=True,
    )


@pytest.mark.asyncio
async def test_call_action_tool_without_ha_client_returns_error(
    db_engine: AsyncEngine,
) -> None:
    """If no HA client is configured, return a descriptive error."""
    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=None)
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
async def test_call_action_tool_handles_api_error(
    db_engine: AsyncEngine,
) -> None:
    """API errors from Home Assistant are surfaced in the tool result."""
    ha_client, _call_action_mock = _make_mock_ha_client(
        side_effect=HomeassistantAPIError("Bad Request: unknown action"),
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
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
async def test_call_action_tool_propagates_unexpected_exceptions(
    db_engine: AsyncEngine,
) -> None:
    """Unexpected (non-HA) exceptions from the wrapper propagate to the caller."""
    ha_client, _call_action_mock = _make_mock_ha_client(
        side_effect=RuntimeError("boom — wrapper bug"),
    )

    async with DatabaseContext(engine=db_engine) as db_context:
        exec_context = _make_exec_context(db_context=db_context, ha_client=ha_client)
        with pytest.raises(RuntimeError, match="boom — wrapper bug"):
            await call_home_assistant_action_tool(
                exec_context=exec_context,
                domain="light",
                action="turn_on",
                service_data={"entity_id": "light.kitchen"},
            )


@pytest.mark.asyncio
async def test_call_home_assistant_action_via_llm_flow(
    db_engine: AsyncEngine,
) -> None:
    """End-to-end: LLM picks the action tool, the mocked client gets invoked,
    and the LLM receives the changed-state summary in the tool response.
    """
    logger.info("\n--- Test: Call Home Assistant Action via LLM Flow ---")

    ha_client, call_action_mock = _make_mock_ha_client(
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

    processing_service.home_assistant_client = ha_client

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

    call_action_mock.assert_awaited_once_with(
        domain="light",
        action="turn_on",
        service_data={"entity_id": "light.kitchen", "brightness_pct": 75},
        return_response=False,
    )

    logger.info("Test Call Home Assistant Action via LLM Flow PASSED.")
