import pytest
import uuid
import asyncio
import logging
import json
from typing import List, Dict, Any, Optional, Callable, Tuple
import os # Added os import
import signal  # Import the signal module
import pytest_asyncio  # Import pytest_asyncio
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
from family_assistant import storage  # For direct task checking

import socket
from unittest.mock import MagicMock, AsyncMock  # Keep mocks for LLM
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
MCP_TIME_TOOL_NAME = (
    "convert_time"  # Use the actual tool name provided by mcp-server-time
)


def find_free_port():
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Fixture to manage mcp-proxy subprocess for SSE tests ---
@pytest_asyncio.fixture(scope="function")  # Use pytest_asyncio.fixture
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
        "--sse-port",
        str(port),
        "--sse-host",
        host,
        "mcp-server-time",  # The stdio command mcp-proxy should run
    ]

    logger.info(f"Starting MCP proxy server: {' '.join(command)}")
    # Let subprocess stdout/stderr go to parent (test runner) to avoid pipe buffers filling up
    # Use preexec_fn to ensure the child process gets its own process group,
    # so signals don't affect the parent pytest process.
    process = subprocess.Popen(command, preexec_fn=os.setpgrp) # type: ignore
    time.sleep(5)  # Increased wait time for server and proxy to start reliably

    yield sse_url  # Provide the SSE URL to the test

    logger.info("Stopping MCP proxy server...")
    try:
        # Terminate the process group first
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.send_signal(signal.SIGINT)
        process.wait(timeout=5)  # Wait for graceful shutdown
    except subprocess.TimeoutExpired:
        logger.warning("MCP proxy did not terminate after SIGINT, sending SIGKILL.")
        process.kill()  # Force kill if SIGINT failed
        process.wait()  # Wait for kill to complete
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
        match_convert = "convert" in last_text
        match_source_time = SOURCE_TIME in last_text  # e.g., "14:30"
        match_source_tz = "new york" in last_text
        match_target_tz = "los angeles" in last_text
        match_tools_exist = tools is not None
        tool_names = [t.get("function", {}).get("name") for t in tools or []]
        match_tool_name = any(name == MCP_TIME_TOOL_NAME for name in tool_names)
        logger.debug(
            f"Matcher Checks: convert={match_convert}, source_time={match_source_time}, source_tz={match_source_tz}, target_tz={match_target_tz}, tools_exist={match_tools_exist}, tool_name_found={match_tool_name} (looking for '{MCP_TIME_TOOL_NAME}' in {tool_names})"
        )
        # Ensure all conditions are met
        return (
            match_convert
            and match_source_time
            and match_source_tz
            and match_target_tz
            and match_tools_exist
            and match_tool_name
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
                            "time": SOURCE_TIME,  # Argument name from mcp-server-time docs
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
        tool_message = next(
            (
                m
                for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") == mcp_tool_call_id
            ),
            None,
        )
        # Check if the result contains the expected converted time (flexible check)
        return (
            tool_message is not None
            and EXPECTED_CONVERTED_TIME_FRAGMENT in tool_message.get("content", "")
        )

    final_response_text = f"Rule-based mock: {SOURCE_TIME} in {SOURCE_TZ} is approximately {EXPECTED_CONVERTED_TIME_FRAGMENT} in {TARGET_TZ}."
    final_response = LLMOutput(
        content=final_response_text,
        tool_calls=None,  # Final response should not involve more tool calls here
    )
    tool_result_rule: Rule = (tool_result_matcher, final_response)

    # --- Instantiate Mock LLM ---
    # Use default response for unexpected calls during the flow
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP test."),
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
        "time": {"command": "mcp-server-time"}  # Command to execute for stdio
    }
    # Instantiate MCP provider with the in-memory config dictionary
    mcp_provider = MCPToolsProvider(mcp_server_configs=mcp_config)
    await mcp_provider.initialize()  # Connect and fetch definitions

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
        server_url=None,  # Added missing argument
    )

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to Los Angeles time ({TARGET_TZ})"
    user_request_trigger = [{"type": "text", "text": user_request_text}]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call generate_llm_response_for_chat directly
        final_response_content, tool_info, _, _ = (
            await processing_service.generate_llm_response_for_chat( # Call updated method
                db_context=db_context,
                application=MagicMock(),
                interface_type="test", # Added interface type
                conversation_id=str(TEST_CHAT_ID), # Added conversation ID as string
                trigger_content_parts=user_request_trigger,
                user_name=TEST_USER_NAME,
            )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}") # Log the structure

-    # --- Verification (Assert on final response content) ---
-    logger.info("--- Verifying final response content (SSE) ---")
-    logger.info(f"Final response content received (SSE): {final_response_content}")
-
-    # Assert directly on the returned content
-    assert final_response_content is not None
-    sent_text = final_response_content  # Use the returned content for checks
+    # Verify success and extract final message content
+    assert processing_error is None, f"Processing error: {processing_error}"
+    assert generated_turn_messages is not None
+    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
+    # Find the last assistant message with content
+    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
+    assert final_assistant_message is not None, "No final assistant message with content found"
+    sent_text = final_assistant_message["content"]
     assert (
         EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
     ), f"Final response did not contain the expected converted time (SSE). Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
        )

+    # --- Verification (Assert on final response content) ---
+    logger.info("--- Verifying final response content ---")
+    logger.info(f"Final response content received: {generated_turn_messages}") # Log the structure
+    # Verify success and extract final message content
+    assert processing_error is None, f"Processing error: {processing_error}"
+    assert generated_turn_messages is not None
+    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
+    # Find the last assistant message with content
+    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
+    assert final_assistant_message is not None, "No final assistant message with content found"
+    sent_text = final_assistant_message["content"]
    assert (
        EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
    ), f"Final response did not contain the expected converted time. Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    assert (
        final_response_text in sent_text
    ), f"Final response did not match the mock LLM's final rule output. Sent: '{sent_text}' Expected: '{final_response_text}'"

    logger.info(
        f"Verified MCP tool '{MCP_TIME_TOOL_NAME}' was called and result contained expected fragment."
    )
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
        tool_names = [t.get("function", {}).get("name") for t in tools or []]
        match_tool_name = any(name == MCP_TIME_TOOL_NAME for name in tool_names)
        # Simplified matcher checks for SSE test
        return (
            "convert" in last_text
            and SOURCE_TIME in last_text
            and "new york" in last_text  # Keep simple checks
            and "london" in last_text # Add logical operator
            and tools is not None
            and any(
                tool.get("function", {}).get("name") == MCP_TIME_TOOL_NAME
                for tool in tools
            )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}") # Log the structure

-    # --- Verification (Assert on final response content) ---
-    logger.info("--- Verifying final response content (SSE) ---")
-    logger.info(f"Final response content received (SSE): {final_response_content}")
-
-    # Assert directly on the returned content
-    assert final_response_content is not None
-    sent_text = final_response_content  # Use the returned content for checks
+    # Verify success and extract final message content
+    assert processing_error is None, f"Processing error: {processing_error}"
+    assert generated_turn_messages is not None
+    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
+    # Find the last assistant message with content
+    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
+    assert final_assistant_message is not None, "No final assistant message with content found"
+    sent_text = final_assistant_message["content"]
     assert (
         EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
     ), f"Final response did not contain the expected converted time (SSE). Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
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
                        {
                            "time": SOURCE_TIME,
                            "source_timezone": SOURCE_TZ,
                            "target_timezone": TARGET_TZ,
                        }  # Use correct arg names
                    ),
                },
            }
        ],
    )
    tool_call_rule: Rule = (time_conversion_matcher, tool_call_response)

    # Rule 2: Match the context after the MCP tool returns its result
    def tool_result_matcher(messages, tools, tool_choice):
        tool_message = next(
            (
                m
                for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") == mcp_tool_call_id
            ),
            None,
        )
        return (
            tool_message is not None
            and EXPECTED_CONVERTED_TIME_FRAGMENT in tool_message.get("content", "")
        )

    final_response_text = f"Rule-based mock: {SOURCE_TIME} in {SOURCE_TZ} is approximately {EXPECTED_CONVERTED_TIME_FRAGMENT} in {TARGET_TZ} (via SSE)."
    final_response = LLMOutput(content=final_response_text, tool_calls=None)
    tool_result_rule: Rule = (tool_result_matcher, final_response)

    # --- Instantiate Mock LLM ---
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP SSE test."),
    )
    logger.info(f"Using RuleBasedMockLLMClient for MCP SSE test.")

    # --- Instantiate Dependencies ---
    local_provider = LocalToolsProvider(
        definitions=local_tools_definition, implementations=local_tool_implementations
    )

    # --- Debugging ---
    # Check the type and value received from the fixture
    logger.info(f"Type of mcp_proxy_server fixture value: {type(mcp_proxy_server)}")
    logger.info(f"Value of mcp_proxy_server fixture value: {mcp_proxy_server}")
    # --- End Debugging ---

    # Define MCP config *inside* the test after the fixture yielded the URL
    mcp_config = {
        "time_sse": {  # Use a different server ID to avoid clashes if needed
            "transport": "sse",
            "url": mcp_proxy_server,  # URL is now a string from the fixture
        }
    }
    mcp_provider = MCPToolsProvider(mcp_server_configs=mcp_config)
    await mcp_provider.initialize()  # Connect and fetch definitions

    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )

    # Processing Service (reuse settings)
    dummy_prompts = {"system_prompt": "Test system prompt for MCP SSE."}
    dummy_calendar_config = {}
    dummy_timezone_str = "UTC"
    dummy_max_history = 5
    dummy_history_age = 24  # Added missing argument
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
        server_url=None,
    )

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call (SSE) ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to London time ({TARGET_TZ}) using SSE"
    # Revert to trigger_content_parts for generate_llm_response_for_chat
    user_request_trigger = [
        {"type": "text", "text": user_request_text}  # Correct input format
    ]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        final_response_content, tool_info, _, _ = (
            await processing_service.generate_llm_response_for_chat( # Call updated method
                db_context=db_context,
                application=MagicMock(),
                interface_type="test", # Added interface type
                conversation_id=str(TEST_CHAT_ID), # Added conversation ID as string
                trigger_content_parts=user_request_trigger,
                user_name=TEST_USER_NAME,
            )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}") # Log the structure

-    # --- Verification (Assert on final response content) ---
-    logger.info("--- Verifying final response content (SSE) ---")
-    logger.info(f"Final response content received (SSE): {final_response_content}")
-
-    # Assert directly on the returned content
-    assert final_response_content is not None
-    sent_text = final_response_content  # Use the returned content for checks
+    # Verify success and extract final message content
+    assert processing_error is None, f"Processing error: {processing_error}"
+    assert generated_turn_messages is not None
+    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
+    # Find the last assistant message with content
+    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
+    assert final_assistant_message is not None, "No final assistant message with content found"
+    sent_text = final_assistant_message["content"]
     assert (
         EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
     ), f"Final response did not contain the expected converted time (SSE). Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
        )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}") # Log the structure
    # Verify success and extract final message content
    assert processing_error is None, f"Processing error: {processing_error}"
    assert generated_turn_messages is not None
    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
    # Find the last assistant message with content
    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
    assert final_assistant_message is not None, "No final assistant message with content found"
    sent_text = final_assistant_message["content"]

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}") # Log the structure

-    # --- Verification (Assert on final response content) ---
-    logger.info("--- Verifying final response content (SSE) ---")
-    logger.info(f"Final response content received (SSE): {final_response_content}")
-
-    # Assert directly on the returned content
-    assert final_response_content is not None
-    sent_text = final_response_content  # Use the returned content for checks
+    # Verify success and extract final message content
+    assert processing_error is None, f"Processing error: {processing_error}"
+    assert generated_turn_messages is not None
+    assert len(generated_turn_messages) > 0, "No messages generated during the turn"
+    # Find the last assistant message with content
+    final_assistant_message = next((msg for msg in reversed(generated_turn_messages) if msg.get("role") == "assistant" and msg.get("content")), None)
+    assert final_assistant_message is not None, "No final assistant message with content found"
+    sent_text = final_assistant_message["content"]
    assert (
        EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text
    ), f"Final response did not contain the expected converted time (SSE). Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    assert (
        final_response_text in sent_text
    ), f"Final response did not match the mock LLM's final rule output (SSE). Sent: '{sent_text}' Expected: '{final_response_text}'"
    logger.info(
        f"Verified MCP tool '{MCP_TIME_TOOL_NAME}' was called via SSE and final response contained expected fragment."
    )
    logger.info(f"--- MCP Time Conversion SSE Test ({test_run_id}) Passed ---")
