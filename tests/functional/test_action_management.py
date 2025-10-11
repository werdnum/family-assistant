"""Tests for action management tools (list/modify/cancel).

This module tests the renamed action management tools that work with both
llm_callback and script_execution task types.
"""

import json
import logging
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import select

from family_assistant.interfaces import ChatInterface
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.tasks import tasks_table
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
from family_assistant.utils.clock import MockClock
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

TEST_USER_NAME = "ActionTester"


def get_unique_test_chat_id() -> int:
    """Generate a unique chat ID for each test to prevent interference."""
    return int(str(uuid.uuid4().int)[:8])


@pytest.mark.asyncio
async def test_list_pending_actions_shows_both_types(db_engine: AsyncEngine) -> None:
    """
    Tests that list_pending_actions tool returns both llm_callback and script_execution tasks.
    """
    test_run_id = uuid.uuid4()
    test_chat_id = str(get_unique_test_chat_id())
    logger.info(
        f"\n--- Running List Pending Actions Test ({test_run_id}) with chat_id={test_chat_id} ---"
    )

    # Setup
    mock_clock = MockClock()
    initial_time = mock_clock.now()

    callback_dt = initial_time + timedelta(hours=1)
    script_dt = initial_time + timedelta(hours=2)

    # --- Define Rules for Mock LLM ---
    def list_actions_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "list my actions" in last_text

    list_actions_response = MockLLMOutput(
        content="Let me check your pending actions.",
        tool_calls=[
            ToolCallItem(
                id=f"call_list_{test_run_id}",
                type="function",
                function=ToolCallFunction(
                    name="list_pending_actions",
                    arguments=json.dumps({"limit": 10}),
                ),
            )
        ],
    )

    llm_client = RuleBasedMockLLMClient(
        rules=[(list_actions_matcher, list_actions_response)],
        default_response=MockLLMOutput(content="Here are your pending actions."),
    )

    # --- Setup dependencies ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, MCPToolsProvider(mcp_server_configs={})]
    )
    await composite_provider.get_tool_definitions()

    test_service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt."},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="list_actions_test",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config,
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)

    # --- Create test actions ---
    async with DatabaseContext(engine=db_engine) as db_context:
        # Create LLM callback
        await db_context.tasks.enqueue(
            task_id=f"test_callback_{test_run_id}",
            task_type="llm_callback",
            payload={
                "interface_type": "test",
                "conversation_id": test_chat_id,
                "callback_context": "Test callback",
                "scheduling_timestamp": initial_time.isoformat(),
            },
            scheduled_at=callback_dt,
        )

        # Create script execution
        await db_context.tasks.enqueue(
            task_id=f"test_script_{test_run_id}",
            task_type="script_execution",
            payload={
                "script_code": "print('test')",
                "config": {},
                "conversation_id": test_chat_id,
                "interface_type": "test",
                "task_name": "test_script",
            },
            scheduled_at=script_dt,
        )

    # --- Test list_pending_actions ---
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface,
            interface_type="test",
            conversation_id=test_chat_id,
            trigger_content_parts=[{"type": "text", "text": "List my actions"}],
            trigger_interface_message_id="1001",
            user_name=TEST_USER_NAME,
        )

    # Verify the tool was called and returned both action types
    assert result.error_traceback is None
    # The tool should have been called with both tasks visible
    # Check that LLM received the tool response containing both action types
    llm_calls = llm_client.get_calls()
    tool_response_found = False
    for call in llm_calls:
        messages = call.get("kwargs", {}).get("messages", [])
        for msg in messages:
            if msg.get("role") == "tool" and "test_script" in msg.get("content", ""):
                tool_response_found = True
                content = msg.get("content", "")
                assert "LLM Callback" in content or "Test callback" in content
                assert "Script:" in content or "test_script" in content
                break

    assert tool_response_found, (
        "Tool response with both action types should be in LLM messages"
    )

    logger.info(
        "✓ list_pending_actions returns both llm_callback and script_execution tasks"
    )


@pytest.mark.asyncio
async def test_modify_pending_script_action(db_engine: AsyncEngine) -> None:
    """
    Tests that modify_pending_action tool can modify script_execution tasks.
    """
    test_run_id = uuid.uuid4()
    test_chat_id = str(get_unique_test_chat_id())
    logger.info(
        f"\n--- Running Modify Script Action Test ({test_run_id}) with chat_id={test_chat_id} ---"
    )

    mock_clock = MockClock()
    initial_time = mock_clock.now()

    initial_script_dt = initial_time + timedelta(hours=1)
    modified_script_dt = initial_time + timedelta(hours=2)

    initial_script_code = "print('initial')"
    modified_script_code = "print('modified')"

    # Create the script task
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.tasks.enqueue(
            task_id=f"test_script_{test_run_id}",
            task_type="script_execution",
            payload={
                "script_code": initial_script_code,
                "config": {"script_code": initial_script_code},
                "conversation_id": test_chat_id,
                "interface_type": "test",
                "task_name": "test_modify",
            },
            scheduled_at=initial_script_dt,
        )

    # --- Define Rules for Mock LLM ---
    def modify_matcher(kwargs: MatcherArgs) -> bool:
        return (
            "modify the script"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    modify_response = MockLLMOutput(
        content="Modifying the script action.",
        tool_calls=[
            ToolCallItem(
                id=f"call_modify_{test_run_id}",
                type="function",
                function=ToolCallFunction(
                    name="modify_pending_action",
                    arguments=json.dumps({
                        "task_id": f"test_script_{test_run_id}",
                        "new_schedule_time": modified_script_dt.isoformat(),
                        "new_script_code": modified_script_code,
                    }),
                ),
            )
        ],
    )

    llm_client = RuleBasedMockLLMClient(
        rules=[(modify_matcher, modify_response)],
        default_response=MockLLMOutput(content="Action modified."),
    )

    # --- Setup dependencies ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, MCPToolsProvider(mcp_server_configs={})]
    )
    await composite_provider.get_tool_definitions()

    test_service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt."},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="modify_action_test",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config,
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)

    # --- Test modify_pending_action ---
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface,
            interface_type="test",
            conversation_id=test_chat_id,
            trigger_content_parts=[{"type": "text", "text": "Modify the script"}],
            trigger_interface_message_id="1002",
            user_name=TEST_USER_NAME,
        )

    assert result.error_traceback is None

    # Verify the task was modified in the database
    async with DatabaseContext(engine=db_engine) as db_context:
        stmt = select(tasks_table).where(
            tasks_table.c.task_id == f"test_script_{test_run_id}"
        )
        task = await db_context.fetch_one(stmt)

    assert task is not None
    assert task["payload"]["script_code"] == modified_script_code
    assert task["payload"]["config"]["script_code"] == modified_script_code
    assert task["scheduled_at"].replace(tzinfo=None) == modified_script_dt.replace(
        tzinfo=None
    )

    logger.info("✓ modify_pending_action successfully modifies script_execution tasks")


@pytest.mark.asyncio
async def test_cancel_pending_script_action(db_engine: AsyncEngine) -> None:
    """
    Tests that cancel_pending_action tool can cancel script_execution tasks.
    """
    test_run_id = uuid.uuid4()
    test_chat_id = str(get_unique_test_chat_id())
    logger.info(
        f"\n--- Running Cancel Script Action Test ({test_run_id}) with chat_id={test_chat_id} ---"
    )

    mock_clock = MockClock()
    initial_time = mock_clock.now()
    script_dt = initial_time + timedelta(hours=1)

    # Create the script task
    async with DatabaseContext(engine=db_engine) as db_context:
        await db_context.tasks.enqueue(
            task_id=f"test_script_{test_run_id}",
            task_type="script_execution",
            payload={
                "script_code": "print('to be cancelled')",
                "config": {},
                "conversation_id": test_chat_id,
                "interface_type": "test",
                "task_name": "test_cancel",
            },
            scheduled_at=script_dt,
        )

    # --- Define Rules for Mock LLM ---
    def cancel_matcher(kwargs: MatcherArgs) -> bool:
        return (
            "cancel the script"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    cancel_response = MockLLMOutput(
        content="Cancelling the script action.",
        tool_calls=[
            ToolCallItem(
                id=f"call_cancel_{test_run_id}",
                type="function",
                function=ToolCallFunction(
                    name="cancel_pending_action",
                    arguments=json.dumps({"task_id": f"test_script_{test_run_id}"}),
                ),
            )
        ],
    )

    llm_client = RuleBasedMockLLMClient(
        rules=[(cancel_matcher, cancel_response)],
        default_response=MockLLMOutput(content="Action cancelled."),
    )

    # --- Setup dependencies ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
    )
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, MCPToolsProvider(mcp_server_configs={})]
    )
    await composite_provider.get_tool_definitions()

    test_service_config = ProcessingServiceConfig(
        prompts={"system_prompt": "Test system prompt."},
        timezone_str="UTC",
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={},
        delegation_security_level="confirm",
        id="cancel_action_test",
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config,
        app_config={},
        context_providers=[],
        server_url=None,
        clock=mock_clock,
    )

    mock_chat_interface = AsyncMock(spec=ChatInterface)

    # --- Test cancel_pending_action ---
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=mock_chat_interface,
            interface_type="test",
            conversation_id=test_chat_id,
            trigger_content_parts=[{"type": "text", "text": "Cancel the script"}],
            trigger_interface_message_id="1003",
            user_name=TEST_USER_NAME,
        )

    assert result.error_traceback is None

    # Verify the task was cancelled in the database
    async with DatabaseContext(engine=db_engine) as db_context:
        stmt = select(tasks_table).where(
            tasks_table.c.task_id == f"test_script_{test_run_id}"
        )
        task = await db_context.fetch_one(stmt)

    assert task is not None
    assert task["status"] == "failed"
    assert "cancelled by user" in task["error"].lower()

    logger.info("✓ cancel_pending_action successfully cancels script_execution tasks")
