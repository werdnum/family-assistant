"""Tests for subconversation history isolation during delegation."""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypedDict, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.interfaces import ChatInterface
from family_assistant.llm import (
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.security.taint import TaintMetadata
from family_assistant.storage import delegation_runs_table, message_history_table
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
from tests.helpers import wait_for_condition
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
NESTED_PROFILE_ID = "nested_profile"
TEST_CHAT_ID = "test_chat_123"
TEST_INTERFACE_TYPE = "test_interface"
TEST_USER_NAME = "TestUser"


class RecordedChatMessage(TypedDict):
    """Message sent through the recording chat interface."""

    conversation_id: str
    text: str
    attachment_ids: list[str] | None


class RecordingChatInterface:
    """Small fake chat boundary that records delivered messages."""

    def __init__(self) -> None:
        self.messages: list[RecordedChatMessage] = []

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_interface_id: str | None = None,
        attachment_ids: list[str] | None = None,
        on_behalf_of_user_id: str | None = None,
        taint_metadata: TaintMetadata | None = None,
    ) -> str | None:
        _ = (parse_mode, reply_to_interface_id, on_behalf_of_user_id, taint_metadata)
        self.messages.append(
            RecordedChatMessage(
                conversation_id=conversation_id,
                text=text,
                attachment_ids=attachment_ids,
            )
        )
        return f"recorded-message-{len(self.messages)}"


@pytest.fixture
def dummy_prompts() -> dict[str, str]:
    return {"system_prompt": "You are a {profile_id} assistant."}


@pytest.fixture
def primary_service_config(dummy_prompts: dict[str, str]) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts=dummy_prompts,
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        id=PRIMARY_PROFILE_ID,
    )


@pytest.fixture
def delegated_service_config(dummy_prompts: dict[str, str]) -> ProcessingServiceConfig:
    return ProcessingServiceConfig(
        prompts=dummy_prompts,
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        id=DELEGATED_PROFILE_ID,
    )


def create_tools_provider(_profile_tools_config: ToolsConfig) -> ToolsProvider:
    """Helper to create a ToolsProvider for a profile."""
    local_provider = LocalToolsProvider(
        registrations=local_tool_registrations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )

    return PolicyEnforcingToolsProvider(
        wrapped_provider=composite_provider,
        policy_engine=PolicyEngine.from_policy_config(
            ToolPolicyConfig(default_decision=ToolPolicyDecision.ALLOW)
        ),
    )


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
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
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
    primary_processing_service.processing_services_registry = registry
    delegated_processing_service.processing_services_registry = registry
    task_worker_manager(
        primary_processing_service,
        MagicMock(spec=ChatInterface),
        register_delegation_handler=True,
    )

    # Execute the interaction
    db_context = Database(engine=db_engine)
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
    db_context = Database(engine=db_engine)
    # Get all messages for this conversation
    all_messages = await db_context.fetch_all(
        select(message_history_table)
        .where(message_history_table.c.conversation_id == TEST_CHAT_ID)
        .order_by(message_history_table.c.timestamp.asc())
    )

    # Separate main conversation messages from subconversation messages
    main_messages = [msg for msg in all_messages if msg["subconversation_id"] is None]
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
    assistant_messages = [msg for msg in main_messages if msg["role"] == "assistant"]
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
    ), "Delegated profile's internal messages should not appear in main conversation"

    logger.info("✓ Subconversation isolation verified successfully")


@pytest.mark.asyncio
async def test_async_delegation_completion_wakes_source_profile_with_history(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    primary_service_config: ProcessingServiceConfig,
    delegated_service_config: ProcessingServiceConfig,
) -> None:
    """Async delegation completion wakes the source profile through durable history."""
    source_service_config = replace(primary_service_config, max_history_messages=1)
    source_tools_provider = create_tools_provider(source_service_config.tools_config)
    target_tools_provider = create_tools_provider(delegated_service_config.tools_config)
    await source_tools_provider.get_tool_definitions()
    await target_tools_provider.get_tool_definitions()

    source_wakeup_calls: list[list[object]] = []

    def source_system_wakeup_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        has_system_wakeup = any(
            isinstance(message, SystemMessage)
            and isinstance(message.content, str)
            and "System: Delegated profile task completed." in message.content
            and "Delegated task completed successfully." not in message.content
            for message in messages
        )
        has_delegated_result_data = any(
            isinstance(message, UserMessage)
            and isinstance(message.content, str)
            and "Delegated task completed successfully." in message.content
            for message in messages
        )
        return has_system_wakeup and has_delegated_result_data

    def source_system_wakeup_response(kwargs: MatcherArgs) -> MockLLMOutput:
        source_wakeup_calls.append(list(kwargs.get("messages", [])))
        return MockLLMOutput(
            content="Source profile reviewed the delegated result for the user.",
            tool_calls=None,
        )

    def source_tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return any(
            getattr(message, "role", None) == "tool"
            and isinstance(message.content, str)
            and "Delegation handed off" in message.content
            for message in messages
        )

    def source_tool_result_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="I handed this off and will follow up when it finishes.",
            tool_calls=None,
        )

    def source_user_request_matcher(kwargs: MatcherArgs) -> bool:
        return (
            get_last_message_text(kwargs.get("messages", []))
            .lower()
            .startswith("please delegate asynchronously")
        )

    def source_delegate_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="I'll delegate that in the background.",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="delegate_to_service",
                        arguments=json.dumps({
                            "target_service_id": DELEGATED_PROFILE_ID,
                            "user_request": "Process this delegated task",
                            "delivery_hint": "background",
                        }),
                    ),
                )
            ],
        )

    source_llm = RuleBasedMockLLMClient(
        rules=[
            (source_system_wakeup_matcher, source_system_wakeup_response),
            (source_tool_result_matcher, source_tool_result_response),
            (source_user_request_matcher, source_delegate_response),
        ]
    )
    target_llm = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(
            content="Delegated task completed successfully.",
            tool_calls=None,
        ),
    )

    primary_service = ProcessingService(
        llm_client=source_llm,
        tools_provider=source_tools_provider,
        service_config=source_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    delegated_service = ProcessingService(
        llm_client=target_llm,
        tools_provider=target_tools_provider,
        service_config=delegated_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    registry = {
        PRIMARY_PROFILE_ID: primary_service,
        DELEGATED_PROFILE_ID: delegated_service,
    }
    primary_service.processing_services_registry = registry
    delegated_service.processing_services_registry = registry

    chat_interface = RecordingChatInterface()
    task_worker_manager(
        primary_service,
        cast("ChatInterface", chat_interface),
        register_delegation_handler=True,
    )

    conversation_id = "async_completion_wakeup_flow"
    db_context = Database(engine=db_engine)
    initial_result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=conversation_id,
        trigger_content_parts=[
            {"type": "text", "text": "Please delegate asynchronously"}
        ],
        trigger_interface_message_id="msg_async",
        user_name=TEST_USER_NAME,
        chat_interface=cast("ChatInterface", chat_interface),
        request_confirmation_callback=None,
    )

    assert initial_result.error_traceback is None
    assert (
        initial_result.text_reply
        == "I handed this off and will follow up when it finishes."
    )

    await wait_for_condition(
        lambda: chat_interface.messages,
        interval=0.05,
        description="source profile delegation wakeup response",
    )

    assert chat_interface.messages == [
        RecordedChatMessage(
            conversation_id=conversation_id,
            text="Source profile reviewed the delegated result for the user.",
            attachment_ids=None,
        )
    ]
    assert source_wakeup_calls, "Expected source profile to receive wakeup context"
    wakeup_messages = source_wakeup_calls[0]
    assert (
        sum(
            1
            for message in wakeup_messages
            if isinstance(message, UserMessage)
            and isinstance(message.content, str)
            and "Delegated task completed successfully." in message.content
        )
        == 1
    )
    assert not any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Historical delegation completion event from a previous turn."
        in message.content
        for message in wakeup_messages
    )

    async def load_committed_wakeup_state() -> (
        tuple[list[Any], list[Any], list[Any]] | None
    ):
        db_context = Database(engine=db_engine)
        rows = await db_context.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == conversation_id)
            .order_by(message_history_table.c.internal_id)
        )
        main_rows = [row for row in rows if row["subconversation_id"] is None]
        sub_rows = [row for row in rows if row["subconversation_id"] is not None]
        system_rows = [
            row
            for row in main_rows
            if row["role"] == "system"
            and "System: Delegated profile task completed." in row["content"]
        ]
        data_rows = [
            row
            for row in main_rows
            if row["role"] == "user"
            and "Delegated task completed successfully." in row["content"]
        ]
        final_rows = [
            row
            for row in main_rows
            if row["role"] == "assistant"
            and row["content"]
            == "Source profile reviewed the delegated result for the user."
        ]
        if (
            len(system_rows) != 1
            or len(data_rows) != 1
            or len(final_rows) != 1
            or final_rows[0]["interface_message_id"] != "recorded-message-1"
            or not sub_rows
        ):
            return None
        next_turn_history = await db_context.message_history.get_recent(
            interface_type=TEST_INTERFACE_TYPE,
            conversation_id=conversation_id,
            limit=20,
            processing_profile_id=PRIMARY_PROFILE_ID,
            subconversation_id=None,
            current_time=primary_service.clock.now(),
        )
        reply_thread_history = await db_context.message_history.get_by_thread_id(
            thread_root_id=data_rows[0]["internal_id"],
            processing_profile_id=PRIMARY_PROFILE_ID,
            subconversation_id=None,
        )
        return rows, next_turn_history, reply_thread_history

    rows, next_turn_history, reply_thread_history = cast(
        "tuple[list[Any], list[Any], list[Any]]",
        await wait_for_condition(
            load_committed_wakeup_state,
            interval=0.05,
            description="persisted source profile delegation wakeup rows",
        ),
    )
    main_rows = [row for row in rows if row["subconversation_id"] is None]
    sub_rows = [row for row in rows if row["subconversation_id"] is not None]

    system_rows = [
        row
        for row in main_rows
        if row["role"] == "system"
        and "System: Delegated profile task completed." in row["content"]
    ]
    assert len(system_rows) == 1
    assert system_rows[0]["processing_profile_id"] == PRIMARY_PROFILE_ID
    assert "Delegated task completed successfully." not in system_rows[0]["content"]
    assert system_rows[0]["is_internal"] is True

    data_rows = [
        row
        for row in main_rows
        if row["role"] == "user"
        and "Delegated task completed successfully." in row["content"]
    ]
    assert len(data_rows) == 1
    assert data_rows[0]["processing_profile_id"] == PRIMARY_PROFILE_ID
    assert data_rows[0]["is_internal"] is True
    assert system_rows[0]["thread_root_id"] == data_rows[0]["internal_id"]

    raw_completion_rows = [
        row
        for row in main_rows
        if row["role"] == "assistant"
        and row["content"]
        and row["content"].startswith("Delegated task delegation_")
    ]
    assert raw_completion_rows == []

    final_rows = [
        row
        for row in main_rows
        if row["role"] == "assistant"
        and row["content"]
        == "Source profile reviewed the delegated result for the user."
    ]
    assert len(final_rows) == 1
    assert final_rows[0]["interface_message_id"] == "recorded-message-1"
    assert final_rows[0]["is_internal"] is False
    assert final_rows[0]["thread_root_id"] == data_rows[0]["internal_id"]

    assert sub_rows
    assert {row["processing_profile_id"] for row in sub_rows} == {DELEGATED_PROFILE_ID}

    assert not any(
        isinstance(message, SystemMessage)
        and isinstance(message.content, str)
        and "Delegated task completed successfully." in message.content
        for message in next_turn_history
    )
    assert any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Delegated task completed successfully." in message.content
        for message in next_turn_history
    )
    assert any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Delegated task completed successfully." in message.content
        for message in reply_thread_history
    )
    assert not any(
        isinstance(message, UserMessage)
        and isinstance(message.content, str)
        and "Respond to the user with the result." in message.content
        for message in next_turn_history
    )


@pytest.mark.asyncio
async def test_nested_async_delegation_completion_wakes_source_subconversation(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[Any, Any, Any]],
    primary_service_config: ProcessingServiceConfig,
    delegated_service_config: ProcessingServiceConfig,
    dummy_prompts: dict[str, str],
) -> None:
    """Nested async delegation completion resumes the caller subconversation."""
    nested_service_config = ProcessingServiceConfig(
        prompts=dummy_prompts,
        timezone=ZoneInfo("UTC"),
        max_history_messages=10,
        history_max_age_hours=24,
        tools_config=ToolsConfig(delegate_handoff_after_seconds=60.0),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        id=NESTED_PROFILE_ID,
    )
    source_tools_provider = create_tools_provider(primary_service_config.tools_config)
    delegated_tools_provider = create_tools_provider(
        delegated_service_config.tools_config
    )
    nested_tools_provider = create_tools_provider(nested_service_config.tools_config)
    await source_tools_provider.get_tool_definitions()
    await delegated_tools_provider.get_tool_definitions()
    await nested_tools_provider.get_tool_definitions()

    delegated_wakeup_calls: list[list[object]] = []

    def primary_wakeup_matcher(kwargs: MatcherArgs) -> bool:
        return any(
            isinstance(message, SystemMessage)
            and isinstance(message.content, str)
            and "System: Delegated profile task completed." in message.content
            for message in kwargs.get("messages", [])
        )

    def primary_wakeup_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(content="Primary saw the delegated update.")

    def primary_tool_result_matcher(kwargs: MatcherArgs) -> bool:
        return any(
            getattr(message, "role", None) == "tool"
            and isinstance(message.content, str)
            and "Delegation handed off" in message.content
            for message in kwargs.get("messages", [])
        )

    def primary_tool_result_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(content="Primary handed off the parent task.")

    def primary_user_request_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        return (
            get_last_message_text(messages)
            .lower()
            .startswith("start nested delegation")
        )

    def primary_delegate_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="Delegating parent task.",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="delegate_to_service",
                        arguments=json.dumps({
                            "target_service_id": DELEGATED_PROFILE_ID,
                            "user_request": "Process parent task",
                            "delivery_hint": "background",
                        }),
                    ),
                )
            ],
        )

    def delegated_wakeup_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        has_wakeup = any(
            isinstance(message, SystemMessage)
            and isinstance(message.content, str)
            and "System: Delegated profile task completed." in message.content
            for message in messages
        )
        has_nested_result = any(
            isinstance(message, UserMessage)
            and isinstance(message.content, str)
            and "Nested task completed successfully." in message.content
            for message in messages
        )
        has_parent_history = any(
            isinstance(message, UserMessage)
            and isinstance(message.content, str)
            and "Process parent task" in message.content
            for message in messages
        )
        return has_wakeup and has_nested_result and has_parent_history

    def delegated_wakeup_response(kwargs: MatcherArgs) -> MockLLMOutput:
        delegated_wakeup_calls.append(list(kwargs.get("messages", [])))
        return MockLLMOutput(content="Delegated profile reviewed nested result.")

    def delegated_tool_result_matcher(kwargs: MatcherArgs) -> bool:
        return any(
            getattr(message, "role", None) == "tool"
            and isinstance(message.content, str)
            and "Delegation handed off" in message.content
            for message in kwargs.get("messages", [])
        )

    def delegated_tool_result_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(content="Delegated profile handed off nested task.")

    def delegated_parent_request_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        has_tool_messages = any(
            getattr(message, "role", None) == "tool" for message in messages
        )
        return (
            get_last_message_text(messages).startswith("Process parent task")
            and not has_tool_messages
        )

    def delegated_parent_response(_kwargs: MatcherArgs) -> MockLLMOutput:
        return MockLLMOutput(
            content="Delegating nested task.",
            tool_calls=[
                ToolCallItem(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=ToolCallFunction(
                        name="delegate_to_service",
                        arguments=json.dumps({
                            "target_service_id": NESTED_PROFILE_ID,
                            "user_request": "Process nested task",
                            "delivery_hint": "background",
                        }),
                    ),
                )
            ],
        )

    primary_llm = RuleBasedMockLLMClient(
        rules=[
            (primary_wakeup_matcher, primary_wakeup_response),
            (primary_tool_result_matcher, primary_tool_result_response),
            (primary_user_request_matcher, primary_delegate_response),
        ]
    )
    delegated_llm = RuleBasedMockLLMClient(
        rules=[
            (delegated_wakeup_matcher, delegated_wakeup_response),
            (delegated_tool_result_matcher, delegated_tool_result_response),
            (delegated_parent_request_matcher, delegated_parent_response),
        ]
    )
    nested_llm = RuleBasedMockLLMClient(
        rules=[],
        default_response=MockLLMOutput(content="Nested task completed successfully."),
    )

    primary_service = ProcessingService(
        llm_client=primary_llm,
        tools_provider=source_tools_provider,
        service_config=primary_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    delegated_service = ProcessingService(
        llm_client=delegated_llm,
        tools_provider=delegated_tools_provider,
        service_config=delegated_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    nested_service = ProcessingService(
        llm_client=nested_llm,
        tools_provider=nested_tools_provider,
        service_config=nested_service_config,
        context_providers=[],
        server_url="http://test.server",
        app_config=AppConfig(),
    )
    registry = {
        PRIMARY_PROFILE_ID: primary_service,
        DELEGATED_PROFILE_ID: delegated_service,
        NESTED_PROFILE_ID: nested_service,
    }
    primary_service.processing_services_registry = registry
    delegated_service.processing_services_registry = registry
    nested_service.processing_services_registry = registry

    chat_interface = RecordingChatInterface()
    task_worker_manager(
        primary_service,
        cast("ChatInterface", chat_interface),
        register_delegation_handler=True,
    )

    conversation_id = "nested_async_delegation_wakeup_flow"
    db_context = Database(engine=db_engine)
    initial_result = await primary_service.handle_chat_interaction(
        db_context=db_context,
        interface_type=TEST_INTERFACE_TYPE,
        conversation_id=conversation_id,
        trigger_content_parts=[{"type": "text", "text": "Start nested delegation"}],
        trigger_interface_message_id="msg_nested_async",
        user_name=TEST_USER_NAME,
        chat_interface=cast("ChatInterface", chat_interface),
        request_confirmation_callback=None,
    )

    assert initial_result.error_traceback is None
    assert initial_result.text_reply == "Primary handed off the parent task."

    await wait_for_condition(
        lambda: any(
            message["text"] == "Delegated profile reviewed nested result."
            for message in chat_interface.messages
        ),
        interval=0.05,
        description="delegated profile nested wake response",
    )

    assert delegated_wakeup_calls, "Expected delegated profile to receive child wakeup"

    async def load_nested_wakeup_state() -> tuple[Any, Any, str, list[Any]] | None:
        db_context = Database(engine=db_engine)
        run_rows = await db_context.fetch_all(
            select(delegation_runs_table)
            .where(delegation_runs_table.c.conversation_id == conversation_id)
            .order_by(delegation_runs_table.c.created_at)
        )
        parent_run = next(
            (
                row
                for row in run_rows
                if row["target_service_id"] == DELEGATED_PROFILE_ID
            ),
            None,
        )
        child_run = next(
            (row for row in run_rows if row["target_service_id"] == NESTED_PROFILE_ID),
            None,
        )
        if parent_run is None or child_run is None:
            return None
        # The wake commits its data/system rows, the visible response row, and
        # mark_notified(result_message_internal_id=...) atomically in a single
        # transaction. run_rows and message_rows are read as two separate READ
        # COMMITTED snapshots, so a wake that commits between them read-skews: a
        # stale child_run (result_message_internal_id still NULL) against freshly
        # committed message rows. Keep polling until the run row reflects the
        # committed wake so the two snapshots are consistent.
        if child_run["result_message_internal_id"] is None:
            return None
        parent_subconversation_id = parent_run["subconversation_id"]
        message_rows = await db_context.fetch_all(
            select(message_history_table)
            .where(message_history_table.c.conversation_id == conversation_id)
            .order_by(message_history_table.c.internal_id)
        )
        has_child_wake = any(
            row["role"] == "system"
            and row["processing_profile_id"] == DELEGATED_PROFILE_ID
            and f"Delegation reference: {child_run['delegation_id']}" in row["content"]
            for row in message_rows
        )
        if not has_child_wake:
            return None
        return parent_run, child_run, parent_subconversation_id, list(message_rows)

    nested_wakeup_state = await wait_for_condition(
        load_nested_wakeup_state,
        interval=0.05,
        description="persisted delegated profile nested wake rows",
    )
    assert nested_wakeup_state is not None
    _parent_run, child_run, parent_subconversation_id, message_rows = (
        nested_wakeup_state
    )
    assert child_run["source_subconversation_id"] == parent_subconversation_id

    child_wakeup_system_rows = [
        row
        for row in message_rows
        if row["role"] == "system"
        and row["processing_profile_id"] == DELEGATED_PROFILE_ID
        and f"Delegation reference: {child_run['delegation_id']}" in row["content"]
    ]
    assert len(child_wakeup_system_rows) == 1
    assert child_wakeup_system_rows[0]["is_internal"] is True
    assert (
        child_wakeup_system_rows[0]["subconversation_id"] == parent_subconversation_id
    )

    child_wakeup_data_rows = [
        row
        for row in message_rows
        if row["role"] == "user"
        and row["processing_profile_id"] == DELEGATED_PROFILE_ID
        and "Nested task completed successfully." in row["content"]
    ]
    assert len(child_wakeup_data_rows) == 1
    assert child_wakeup_data_rows[0]["is_internal"] is True
    assert child_wakeup_data_rows[0]["subconversation_id"] == parent_subconversation_id
    assert (
        child_wakeup_system_rows[0]["thread_root_id"]
        == child_wakeup_data_rows[0]["internal_id"]
    )

    child_wakeup_response_rows = [
        row
        for row in message_rows
        if row["role"] == "assistant"
        and row["processing_profile_id"] == DELEGATED_PROFILE_ID
        and row["content"] == "Delegated profile reviewed nested result."
    ]
    assert len(child_wakeup_response_rows) == 2
    source_response_row, visible_response_row = child_wakeup_response_rows
    assert source_response_row["is_internal"] is False
    assert source_response_row["subconversation_id"] == parent_subconversation_id
    assert (
        source_response_row["thread_root_id"]
        == child_wakeup_data_rows[0]["internal_id"]
    )
    assert visible_response_row["is_internal"] is False
    assert visible_response_row["subconversation_id"] is None
    assert visible_response_row["thread_root_id"] is None
    assert (
        child_run["result_message_internal_id"] == visible_response_row["internal_id"]
    )
