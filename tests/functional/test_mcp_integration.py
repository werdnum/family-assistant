import pytest
import uuid
import asyncio
import logging
import json
from typing import List, Dict, Any, Optional, Callable, Tuple
from unittest.mock import MagicMock, AsyncMock

# Import necessary components from the application
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.processing import ProcessingService
from family_assistant.llm import LLMInterface, LLMOutput
from family_assistant.tools import (
    LocalToolsProvider,
    MCPToolsProvider,
    CompositeToolsProvider,
    TOOLS_DEFINITION as local_tools_definition,
    AVAILABLE_FUNCTIONS as local_tool_implementations,
    ToolExecutionContext,
)
from family_assistant import storage # For direct task checking

from tests.mocks.mock_llm import (
    RuleBasedMockLLMClient,
    Rule,
    MatcherFunction,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_CHAT_ID = 65432
TEST_USER_NAME = "MCPTester"

# --- Time Conversion Details ---
SOURCE_TIME = "14:30"
SOURCE_TZ = "America/New_York"
TARGET_TZ = "Europe/London"
EXPECTED_CONVERTED_TIME_FRAGMENT = "19:30" # Assuming a 5-hour difference

# Assume MCP server ID 'time' maps to tool 'convert_time_zone'
MCP_TIME_TOOL_NAME = "mcp_time_convert_time_zone"

@pytest.mark.asyncio
async def test_mcp_time_conversion(test_db_engine, test_mcp_config_path):
    """
    Tests the end-to-end flow involving an MCP tool call:
    1. User asks to convert time between timezones.
    2. Mock LLM identifies the request and calls the MCP time tool.
    3. MCPToolsProvider executes the call against a running MCP time server.
    4. Mock LLM receives the tool result and generates the final response.
    5. Verify the final response containing the converted time is sent.
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running MCP Time Conversion Test ({test_run_id}) ---")

    # --- Define Rules for Mock LLM ---
    mcp_tool_call_id = f"call_mcp_time_{test_run_id}"

    # Rule 1: Match request to convert time
    def time_conversion_matcher(messages, tools, tool_choice):
        last_text = get_last_message_text(messages).lower()
        # Loosely match keywords
        return (
            "convert" in last_text
            and SOURCE_TIME in last_text
            and "new york" in last_text # Allow fuzzy matching
            and "london" in last_text
            and tools is not None
            and any(tool.get("function", {}).get("name") == MCP_TIME_TOOL_NAME for tool in tools)
        )

    tool_call_response = LLMOutput(
        content=f"OK, I will convert {SOURCE_TIME} from {SOURCE_TZ} to {TARGET_TZ} using the MCP time tool.",
        tool_calls=[
            {
                "id": mcp_tool_call_id,
                "type": "function",
                "function": {
                    "name": MCP_TIME_TOOL_NAME,
                    "arguments": json.dumps(
                        {
                            "time_str": SOURCE_TIME,
                            "source_tz": SOURCE_TZ,
                            "target_tz": TARGET_TZ,
                        }
                    ),
                },
            }
        ],
    )
    tool_call_rule: Rule = (time_conversion_matcher, tool_call_response)

    # Rule 2: Match the context after the MCP tool returns its result
    def tool_result_matcher(messages, tools, tool_choice):
        # Check for the tool result message associated with the tool call ID
        tool_message = next((m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == mcp_tool_call_id), None)
        # Check if the result contains the expected converted time (flexible check)
        return (
            tool_message is not None
            and EXPECTED_CONVERTED_TIME_FRAGMENT in tool_message.get("content", "")
        )

    final_response_text = f"Rule-based mock: {SOURCE_TIME} in {SOURCE_TZ} is approximately {EXPECTED_CONVERTED_TIME_FRAGMENT} in {TARGET_TZ}."
    final_response = LLMOutput(
        content=final_response_text,
        tool_calls=None, # Final response should not involve more tool calls here
    )
    tool_result_rule: Rule = (tool_result_matcher, final_response)

    # --- Instantiate Mock LLM ---
    # Use default response for unexpected calls during the flow
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP test.")
    )
    logger.info(f"Using RuleBasedMockLLMClient for MCP test.")

    # --- Instantiate Dependencies ---
    # Tool Providers
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )
    # MCP Provider - Assuming test_mcp_config_path points to a valid config
    # and the 'time' server is running.
    mcp_provider = MCPToolsProvider(mcp_config_path=test_mcp_config_path)
    await mcp_provider.initialize() # Connect and fetch definitions

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )

    # Processing Service
    dummy_prompts = {"system_prompt": "Test system prompt for MCP."}
    dummy_calendar_config = {}
    dummy_timezone_str = "UTC"
    dummy_max_history = 5
    dummy_history_age = 24

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
    )

    # Mock Telegram Application and Bot
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=10001)) # Mock sent message ID
    mock_application = MagicMock()
    mock_application.bot = mock_bot

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to London time ({TARGET_TZ})"
    user_request_trigger = [{"type": "text", "text": user_request_text}]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        final_response_content, tool_info, _, _ = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,
            application=mock_application,
            chat_id=TEST_CHAT_ID,
            trigger_content_parts=user_request_trigger,
            user_name=TEST_USER_NAME,
        )

    logger.info(f"Final Response from Processing Service: {final_response_content}")

    # --- Verification ---
    logger.info("--- Verifying MCP tool usage and final response ---")

    # 1. Check if the mock bot sent the *final* response generated by the mock LLM (Rule 2)
    mock_bot.send_message.assert_called_once()
    call_args, call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.get("chat_id") == TEST_CHAT_ID
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    assert EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text, \
        f"Final message sent by bot did not contain the expected converted time. Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    assert final_response_text in sent_text, \
        f"Final message sent did not match the mock LLM's final rule output. Sent: '{sent_text}' Expected: '{final_response_text}'"

    logger.info("Verified mock_bot.send_message was called with the expected final response incorporating MCP tool result.")

    # Note: We don't explicitly mock/verify MCPToolsProvider.execute_tool here.
    # The test relies on the RuleBasedMockLLM's second rule matching only *after*
    # the tool result (presumably fetched by the real MCPToolsProvider) is added
    # to the message history passed back to the LLM by ProcessingService.
    # This makes it an integration test of ProcessingService and MCPToolsProvider.

    logger.info(f"--- MCP Time Conversion Test ({test_run_id}) Passed ---")

