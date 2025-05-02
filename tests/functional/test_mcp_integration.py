import pytest
import uuid
import asyncio
import logging
import json
from typing import List, Dict, Any, Optional, Callable, Tuple
import subprocess
import time

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

import socket
from unittest.mock import MagicMock, AsyncMock
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
TARGET_TZ = "America/Los_Angeles"
EXPECTED_CONVERTED_TIME_FRAGMENT = "11:30"

# Assume MCP server ID 'time' maps to tool 'convert_time_zone'
MCP_TIME_TOOL_NAME = "convert_time"

def find_free_port():
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

# --- Fixture to manage mcp-proxy subprocess for SSE tests ---
@pytest.fixture(scope="function")
async def mcp_proxy_server():
    """
    Starts mcp-proxy listening for SSE and forwarding to mcp-server-time via stdio.
    Yields the SSE URL.
    """
    host = "127.0.0.1"
    port = find_free_port()
    sse_url = f"http://{host}:{port}/sse"
    # Command: mcp-proxy --sse-port <port> --sse-host <host> mcp-server-time
    command = [
        "mcp-proxy",
        "--sse-port", str(port),
        "--sse-host", host,
        "mcp-server-time" # The stdio command mcp-proxy should run
    ]

    logger.info(f"Starting MCP proxy server: {' '.join(command)}")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3) # Give server and proxy time to start up

    yield sse_url # Provide the SSE URL to the test

    logger.info("Stopping MCP proxy server...")
    process.terminate()
    process.wait(timeout=5) # Wait for graceful shutdown
    logger.info("MCP proxy server stopped.")

@pytest.mark.asyncio
async def test_mcp_time_conversion_stdio(test_db_engine):
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
            and "los angeles" in last_text
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
                            "time": SOURCE_TIME,
                            "source_timezone": SOURCE_TZ,
                            "target_timezone": TARGET_TZ,
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

    # Hard-coded MCP configuration using stdio transport.
    # Assumes 'mcp-server-time' command is available via dev dependencies.
    mcp_config = {
        "time": {
            "command": "mcp-server-time" # Command to execute for stdio
        }
    }
    # Instantiate MCP provider with the in-memory config dictionary
    mcp_provider = MCPToolsProvider(mcp_server_configs=mcp_config)
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

    # --- Instantiate Mocks ---
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=10001)) # Mock sent message ID
    mock_application = MagicMock()
    mock_application.bot = mock_bot

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to Los Angeles time ({TARGET_TZ})"
    # Construct message history for process_message
    initial_messages = [
        {"role": "user", "content": user_request_text}
    ]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call process_message which includes sending the response
        await processing_service.process_message(
            db_context=db_context,
            chat_id=TEST_CHAT_ID,
            messages=initial_messages,
            application=mock_application, # Pass the mock application
        )

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


@pytest.mark.asyncio
async def test_mcp_time_conversion_sse(test_db_engine, mcp_proxy_server):
    """
    Tests the end-to-end flow involving an MCP tool call via SSE transport,
    using mcp-proxy to forward to mcp-server-time (stdio).
    1. User asks to convert time between timezones.
    2. Mock LLM identifies the request and calls the MCP time tool.
    3. MCPToolsProvider connects via SSE to mcp-proxy.
    4. mcp-proxy forwards the request via stdio to mcp-server-time.
    5. mcp-proxy forwards the result back via SSE.
    6. Mock LLM receives the tool result and generates the final response.
    7. Verify the final response containing the converted time is sent.
    """
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running MCP Time Conversion SSE Test ({test_run_id}) ---")

    # --- Define Rules for Mock LLM (Identical to stdio test) ---
    mcp_tool_call_id = f"call_mcp_time_sse_{test_run_id}"

    # Rule 1: Match request to convert time
    def time_conversion_matcher(messages, tools, tool_choice):
        last_text = get_last_message_text(messages).lower()
        return (
            "convert" in last_text
            and SOURCE_TIME in last_text
            and "new york" in last_text
            and "london" in last_text
            and tools is not None
            and any(tool.get("function", {}).get("name") == MCP_TIME_TOOL_NAME for tool in tools)
        )

    tool_call_response = LLMOutput(
        content=f"OK, I will convert {SOURCE_TIME} from {SOURCE_TZ} to {TARGET_TZ} using the MCP time tool (via SSE).",
        tool_calls=[
            {
                "id": mcp_tool_call_id,
                "type": "function",
                "function": {
                    "name": MCP_TIME_TOOL_NAME,
                    "arguments": json.dumps(
                        {"time": SOURCE_TIME, "source_timezone": SOURCE_TZ, "target_timezone": TARGET_TZ}
                    ),
                },
            }
        ],
    )
    tool_call_rule: Rule = (time_conversion_matcher, tool_call_response)

    # Rule 2: Match the context after the MCP tool returns its result
    def tool_result_matcher(messages, tools, tool_choice):
        tool_message = next((m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == mcp_tool_call_id), None)
        return tool_message is not None and EXPECTED_CONVERTED_TIME_FRAGMENT in tool_message.get("content", "")

    final_response_text = f"Rule-based mock: {SOURCE_TIME} in {SOURCE_TZ} is approximately {EXPECTED_CONVERTED_TIME_FRAGMENT} in {TARGET_TZ} (via SSE)."
    final_response = LLMOutput(content=final_response_text, tool_calls=None)
    tool_result_rule: Rule = (tool_result_matcher, final_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP SSE test.")
    )
    logger.info(f"Using RuleBasedMockLLMClient for MCP SSE test.")

    # --- Instantiate Dependencies ---
    local_provider = LocalToolsProvider(definitions=local_tools_definition, implementations=local_tool_implementations)

    # Hard-coded MCP configuration using SSE transport pointing to the proxy.
    mcp_config = {
        "time_sse": { # Use a different server ID to avoid clashes if needed
            "transport": "sse",
            "url": mcp_proxy_server # URL provided by the fixture
        }
    }
    mcp_provider = MCPToolsProvider(mcp_server_configs=mcp_config)
    await mcp_provider.initialize()

    composite_provider = CompositeToolsProvider(providers=[local_provider, mcp_provider])

    # Processing Service (reuse settings)
    dummy_prompts = {"system_prompt": "Test system prompt for MCP SSE."}
    dummy_calendar_config = {}
    dummy_timezone_str = "UTC"
    dummy_max_history = 5
    dummy_history_age = 24
    processing_service = ProcessingService(llm_client=llm_client, tools_provider=composite_provider, prompts=dummy_prompts, calendar_config=dummy_calendar_config, timezone_str=dummy_timezone_str, max_history_messages=dummy_max_history, history_max_age_hours=dummy_history_age)

    # --- Instantiate Mocks ---
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=10002)) # Mock sent message ID
    mock_application = MagicMock()
    mock_application.bot = mock_bot

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call (SSE) ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to London time ({TARGET_TZ}) using SSE"
    initial_messages = [
        {"role": "user", "content": user_request_text}
    ]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        await processing_service.process_message(
            db_context=db_context,
            chat_id=TEST_CHAT_ID,
            messages=initial_messages,
            application=mock_application,
        )

    # --- Verification (Identical logic to stdio test) ---
    logger.info("--- Verifying MCP tool usage and final response (SSE) ---")
    mock_bot.send_message.assert_called_once()
    call_args, call_kwargs = mock_bot.send_message.call_args
    assert call_kwargs.get("chat_id") == TEST_CHAT_ID
    sent_text = call_kwargs.get("text")
    assert sent_text is not None
    assert EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
    assert final_response_text in sent_text
    logger.info("Verified mock_bot.send_message was called with the expected final response incorporating MCP tool result (SSE).")
    logger.info(f"--- MCP Time Conversion SSE Test ({test_run_id}) Passed ---")
