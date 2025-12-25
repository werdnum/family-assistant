"""Tests for subconversation history isolation during delegation."""

import json
import logging
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import (
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.storage import message_history_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations_map,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition_list,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    ConfirmingToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
    ToolsProvider,
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

# --- Test Constants ---
PRIMARY_PROFILE_ID = "primary_profile"
DELEGATED_PROFILE_ID = "delegated_profile"
TEST_CHAT_ID = "test_chat_123"
TEST_INTERFACE_TYPE = "test_interface"
TEST_USER_NAME = "TestUser"


@pytest.fixture
def dummy_prompts() -> dict[str, str]:
    return {"system_prompt": "You are a {profile_id} assistant."}


@pytest.fixture
def primary_service_config(dummy_prompts: dict[str, str]) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts=dummy_prompts,
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": ["delegate_to_service"],
            "confirm_tools": [],
        },
        delegation_security_level="unrestricted",
        id=PRIMARY_PROFILE_ID,
    )


@pytest.fixture
def delegated_service_config(dummy_prompts: dict[str, str]) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts=dummy_prompts,
        timezone_str="UTC",
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config={
            "enable_local_tools": [],
            "confirm_tools": [],
        },
        delegation_security_level="unrestricted",
        id=DELEGATED_PROFILE_ID,
    )


# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def create_tools_provider(profile_tools_config: dict[str, Any]) -> ToolsProvider:
    """Helper to create a ToolsProvider for a profile."""
    enabled_local_tool_names = set(profile_tools_config.get("enable_local_tools", []))

    profile_local_definitions = [
        td
        for td in local_tools_definition_list
        if td.get("function", {}).get("name") in enabled_local_tool_names
    ]
    profile_local_implementations = {
        name: func
        for name, func in local_tool_implementations_map.items()
        if name in enabled_local_tool_names
    }

    local_provider = LocalToolsProvider(
        definitions=profile_local_definitions,
        implementations=profile_local_implementations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )

    confirm_tools_set = set(profile_tools_config.get("confirm_tools", []))
    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=composite_provider,
        tools_requiring_confirmation=confirm_tools_set,
    )
    return confirming_provider


@pytest_asyncio.fixture
async def primary_processing_service(
    db_engine: AsyncEngine,
    primary_service_config: ProcessingServiceConfig,
    dummy_prompts: dict[str, str],
) -> ProcessingService:
    tools_provider = create_tools_provider(primary_service_config.tools_config)
    await tools_provider.get_tool_definitions()  # Initialize

    # Create mock LLM that delegates once, then responds
    def delegate_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages).lower()
        # Only delegate on first user message, not after tool results
        has_tool_messages = any(
            getattr(msg, "role", None) == "tool" for msg in messages
        )
        return "delegate" in last_text and not has_tool_messages

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        # After delegation completes, provide final response
        has_tool_messages = any(
            getattr(msg, "role", None) == "tool" for msg in messages
        )
        return has_tool_messages

    def delegate_response(kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="I'll delegate that task.",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="delegate_to_service",
                        arguments=json.dumps({
                            "target_service_id": DELEGATED_PROFILE_ID,
                            "user_request": "Process this delegated task",
                        }),
                    ),
                )
            ],
        )

    def final_response(kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="The task has been delegated and completed successfully.",
            tool_calls=None,
        )

    mock_llm = RuleBasedMockLLMClient(
        rules=[
            (delegate_matcher, delegate_response),
            (final_response_matcher, final_response),
        ]
    )

    return ProcessingService(
        llm_client=mock_llm,
        tools_provider=tools_provider,
        service_config=primary_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )


@pytest_asyncio.fixture
async def delegated_processing_service(
    db_engine: AsyncEngine,
    delegated_service_config: ProcessingServiceConfig,
    dummy_prompts: dict[str, str],
) -> ProcessingService:
    tools_provider = create_tools_provider(delegated_service_config.tools_config)
    await tools_provider.get_tool_definitions()  # Initialize

    # Create mock LLM that responds to delegation
    def delegated_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        last_text = get_last_message_text(messages).lower()
        return "delegated task" in last_text

    delegated_response = MockLLMOutput(
        content="Delegated task completed successfully.",
        tool_calls=None,
    )

    mock_llm = RuleBasedMockLLMClient(rules=[(delegated_matcher, delegated_response)])

    return ProcessingService(
        llm_client=mock_llm,
        tools_provider=tools_provider,
        service_config=delegated_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )


@pytest.mark.asyncio
async def test_subconversation_isolation(
    db_engine: AsyncEngine,
    primary_processing_service: ProcessingService,
    delegated_processing_service: ProcessingService,
) -> None:
    """Test that delegated subconversations have isolated message history."""
    logger.info("--- Test: Subconversation History Isolation ---")

    # Set up service registry
    registry = {
        PRIMARY_PROFILE_ID: primary_processing_service,
        DELEGATED_PROFILE_ID: delegated_processing_service,
    }
    primary_processing_service.set_processing_services_registry(registry)
    delegated_processing_service.set_processing_services_registry(registry)

    # Execute the interaction
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await primary_processing_service.handle_chat_interaction(
            db_context=db_context,
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[
                {
                    "type": "text",
                    "text": "Please delegate this task to the specialist",
                }
            ],
            trigger_interface_message_id="msg1",
            user_name=TEST_USER_NAME,
            chat_interface=MagicMock(spec=ChatInterface),
            request_confirmation_callback=None,
        )

    # Verify no errors
    assert result.error_traceback is None
    assert result.text_reply is not None

    # Check message history isolation
    async with DatabaseContext(engine=db_engine) as db_context:
        # Get all messages for this conversation
        all_messages = await db_context.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == TEST_CHAT_ID)
            .order_by(message_history_table.c.timestamp.asc())
        )

        # Separate main conversation messages from subconversation messages
        main_messages = [
            msg for msg in all_messages if msg["subconversation_id"] is None
        ]
        subconversation_messages = [
            msg for msg in all_messages if msg["subconversation_id"] is not None
        ]

        logger.info(f"Main conversation messages: {len(main_messages)}")
        logger.info(f"Subconversation messages: {len(subconversation_messages)}")

        # Verify main conversation has expected messages
        assert len(main_messages) >= 3, (
            "Expected at least user, assistant, and tool messages in main conversation"
        )

        # Verify main conversation contains user message
        user_messages = [msg for msg in main_messages if msg["role"] == "user"]
        assert len(user_messages) >= 1, "Expected user message in main conversation"
        assert "delegate" in user_messages[0]["content"].lower()

        # Verify main conversation contains delegation tool call
        assistant_messages = [
            msg for msg in main_messages if msg["role"] == "assistant"
        ]
        delegation_tool_call = None
        for msg in assistant_messages:
            if msg["tool_calls"]:
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "delegate_to_service":
                        delegation_tool_call = tc
                        break

        assert delegation_tool_call is not None, (
            "Expected delegation tool call in main conversation"
        )

        # Verify subconversation exists and has its own messages
        assert len(subconversation_messages) > 0, "Expected subconversation messages"

        # Verify all subconversation messages have the same subconversation_id
        subconversation_ids = {
            msg["subconversation_id"] for msg in subconversation_messages
        }
        assert len(subconversation_ids) == 1, (
            "Expected all subconversation messages to have the same subconversation_id"
        )

        # Verify subconversation contains the delegated profile's messages
        subconv_profile_ids = {
            msg["processing_profile_id"] for msg in subconversation_messages
        }
        assert DELEGATED_PROFILE_ID in subconv_profile_ids, (
            "Expected delegated profile messages in subconversation"
        )

        # Verify main conversation does not contain delegated profile's internal messages
        main_profile_ids = {
            msg["processing_profile_id"]
            for msg in main_messages
            if msg["processing_profile_id"]
        }
        # The main conversation should only have messages from the primary profile
        assert DELEGATED_PROFILE_ID not in main_profile_ids or all(
            msg["role"] == "tool"
            for msg in main_messages
            if msg["processing_profile_id"] == DELEGATED_PROFILE_ID
        ), (
            "Delegated profile's internal messages should not appear in main conversation"
        )

        logger.info("✓ Subconversation isolation verified successfully")
