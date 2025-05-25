import asyncio
import json
import logging
import os  # Added os import
import signal  # Import the signal module
import socket
import uuid  # Added for turn_id
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock  # Keep mocks for LLM

import pytest
import pytest_asyncio  # Import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface  # LLMOutput will come from mocks
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig

# Import necessary components from the application
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
    LLMOutput,  # Use LLMOutput from mocks for rules
    MatcherArgs,
    Rule,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

# --- Test Configuration ---
TEST_CHAT_ID = 65432
TEST_USER_ID = 19876  # Added user ID
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


def find_free_port() -> int:
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Fixture to manage mcp-proxy subprocess for SSE tests ---
@pytest_asyncio.fixture(scope="function")  # Use pytest_asyncio.fixture
async def mcp_proxy_server() -> AsyncGenerator[str, None]:
    """
    Starts mcp-proxy listening for SSE and forwarding to mcp-server-time via stdio.
    Yields the SSE URL.
    """
    host = "127.0.0.1"
    port = find_free_port()
    sse_url = f"http://{host}:{port}/sse"
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
    process = await asyncio.create_subprocess_exec(*command, preexec_fn=os.setpgrp)
    await asyncio.sleep(5)  # Increased wait time for server and proxy to start reliably

    yield sse_url  # Provide the SSE URL to the test

    logger.info("Stopping MCP proxy server...")
    if process.returncode is None:  # Check if process is still running
        try:
            # Terminate the process group first
            # Ensure process.pid is available; it should be after creation
            if process.pid is not None:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            # Also send SIGINT to the main process, as mcp-proxy might trap it
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, PermissionError) as e:
            logger.warning(
                f"Error sending SIGTERM/SIGINT to process group or process: {e}"
            )
        except asyncio.TimeoutError:
            logger.warning(
                "MCP proxy did not terminate after SIGINT/SIGTERM, sending SIGKILL."
            )
            if process.returncode is None:  # Check again before kill
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.error("MCP proxy did not terminate after SIGKILL.")
                except Exception as e:
                    logger.error(f"Error during SIGKILL: {e}")
    else:
        logger.info(
            f"MCP proxy server already terminated with code {process.returncode}."
        )
    logger.info("MCP proxy server stopped.")


@pytest.mark.asyncio
async def test_mcp_time_conversion_stdio(test_db_engine: AsyncEngine) -> None:
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

    user_message_id = 101  # Added message ID for the user request

    # Rule 1: Match request to convert time
    def time_conversion_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")

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
            ToolCallItem(
                id=mcp_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=MCP_TIME_TOOL_NAME,
                    arguments=json.dumps({
                        "time": (
                            SOURCE_TIME
                        ),  # Argument name from mcp-server-time docs
                        "source_timezone": SOURCE_TZ,
                        "target_timezone": TARGET_TZ,
                    }),
                ),
            )
        ],
    )
    tool_call_rule: Rule = (time_conversion_matcher, tool_call_response)

    # Rule 2: Match the context after the MCP tool returns its result
    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])

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
    # Cast is used because the type checker sees tests.mocks.mock_llm.LLMOutput
    # as different from family_assistant.llm.LLMOutput, making RuleBasedMockLLMClient
    # not strictly assignable to LLMInterface.
    _mock_llm_impl = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP test."),
    )
    llm_client: LLMInterface = cast("LLMInterface", _mock_llm_impl)
    logger.info("Using RuleBasedMockLLMClient for MCP test.")

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
    dummy_app_config = {}  # Add dummy app_config

    test_service_config_obj_stdio = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_stdio,
        app_config=dummy_app_config,
        server_url=None,
        context_providers=[],
    )

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to Los Angeles time ({TARGET_TZ})"
    user_request_trigger = [{"type": "text", "text": user_request_text}]
    user_message_id = 101

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Call generate_llm_response_for_chat directly
        # Unpack the correct return values: generated_turn_messages, final_reasoning_info, processing_error_traceback
        (
            generated_turn_messages,
            final_reasoning_info,
            processing_error_traceback,
        ) = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,
            application=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            turn_id=str(uuid.uuid4()),  # Added turn_id
            trigger_content_parts=user_request_trigger,
            trigger_interface_message_id=str(user_message_id),
            user_name=TEST_USER_NAME,
        )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (stdio) ---")
    logger.info(f"Final response content received (stdio): {generated_turn_messages}")

    # Verify success and extract final message content
    assert processing_error_traceback is None, (
        f"Processing error: {processing_error_traceback}"
    )
    assert generated_turn_messages is not None
    assert len(generated_turn_messages) > 0, "No messages generated during the turn"

    # Find the last assistant message with content
    final_assistant_message = next(
        (
            msg
            for msg in reversed(generated_turn_messages)
            if msg.get("role") == "assistant" and msg.get("content")
        ),
        None,
    )
    assert final_assistant_message is not None, (
        "No final assistant message with content found"
    )
    sent_text = final_assistant_message["content"]

    # Assertions on the final message content
    assert EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text, (
        f"Final response (stdio) did not contain the expected converted time. Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    )

    assert final_response_text in sent_text, (
        f"Final response did not match the mock LLM's final rule output. Sent: '{sent_text}' Expected: '{final_response_text}'"
    )

    logger.info(
        f"Verified MCP tool '{MCP_TIME_TOOL_NAME}' was called and result contained expected fragment."
    )
    logger.info(f"--- MCP Time Conversion Test ({test_run_id}) Passed ---")
    await processing_service.close()


@pytest.mark.asyncio
async def test_mcp_time_conversion_sse(
    test_db_engine: AsyncEngine, mcp_proxy_server: str
) -> None:
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
    user_message_id = 201  # Added message ID for the SSE test user request

    # Rule 1: Match request to convert time
    def time_conversion_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")

        last_text = get_last_message_text(messages).lower()
        tool_names = [t.get("function", {}).get("name") for t in tools or []]
        any(name == MCP_TIME_TOOL_NAME for name in tool_names)
        # Simplified matcher checks for SSE test
        return (
            "convert" in last_text  # Corrected syntax for matcher
            and SOURCE_TIME in last_text
            and "new york" in last_text  # Keep simple checks
            and "los angeles"
            in last_text  # Corrected target TZ for SSE matcher context check
            and tools is not None
            and any(
                tool.get("function", {}).get("name") == MCP_TIME_TOOL_NAME
                for tool in tools
            )
        )  # Added missing closing parenthesis

    tool_call_response = LLMOutput(
        content=f"OK, I will convert {SOURCE_TIME} from {SOURCE_TZ} to {TARGET_TZ} using the MCP time tool (via SSE).",
        tool_calls=[
            ToolCallItem(
                id=mcp_tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name=MCP_TIME_TOOL_NAME,
                    arguments=json.dumps(
                        {
                            "time": SOURCE_TIME,
                            "source_timezone": SOURCE_TZ,
                            "target_timezone": TARGET_TZ,
                        }  # Use correct arg names
                    ),
                ),
            )
        ],
    )
    tool_call_rule: Rule = (time_conversion_matcher, tool_call_response)

    # Rule 2: Match the context after the MCP tool returns its result
    def tool_result_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])

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
    # Cast is used for the same reasons as in the stdio test.
    _mock_llm_impl_sse = RuleBasedMockLLMClient(
        rules=[tool_call_rule, tool_result_rule],
        default_response=LLMOutput(content="Default mock response for MCP SSE test."),
    )
    llm_client: LLMInterface = cast("LLMInterface", _mock_llm_impl_sse)
    logger.info("Using RuleBasedMockLLMClient for MCP SSE test.")

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
    dummy_history_age = 24
    dummy_app_config = {}  # Add dummy app_config

    test_service_config_obj_sse = ProcessingServiceConfig(
        prompts=dummy_prompts,
        calendar_config=dummy_calendar_config,
        timezone_str=dummy_timezone_str,
        max_history_messages=dummy_max_history,
        history_max_age_hours=dummy_history_age,
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        service_config=test_service_config_obj_sse,
        app_config=dummy_app_config,
        server_url=None,
        context_providers=[],
    )

    # --- Execute the Request ---
    logger.info("--- Sending request requiring MCP tool call (SSE) ---")
    user_request_text = f"Please convert {SOURCE_TIME} New York time ({SOURCE_TZ}) to Los Angeles time ({TARGET_TZ}) using SSE"
    # Revert to trigger_content_parts for generate_llm_response_for_chat
    user_request_trigger = [
        {"type": "text", "text": user_request_text}  # Correct input format
    ]

    async with DatabaseContext(engine=test_db_engine) as db_context:
        # Correct unpacking based on function signature
        (
            generated_turn_messages,
            final_reasoning_info,
            processing_error_traceback,
        ) = await processing_service.generate_llm_response_for_chat(
            db_context=db_context,
            application=MagicMock(),
            interface_type="test",
            conversation_id=str(TEST_CHAT_ID),
            turn_id=str(uuid.uuid4()),  # Added turn_id
            trigger_content_parts=user_request_trigger,
            trigger_interface_message_id=str(user_message_id),
            user_name=TEST_USER_NAME,
        )

    # --- Verification (Assert on final response content) ---
    logger.info("--- Verifying final response content (SSE) ---")
    logger.info(f"Final response content received (SSE): {generated_turn_messages}")

    # Verify success and extract final message content
    assert processing_error_traceback is None, (
        f"Processing error: {processing_error_traceback}"
    )
    assert generated_turn_messages is not None
    assert len(generated_turn_messages) > 0, "No messages generated during the turn"

    # Find the last assistant message with content
    final_assistant_message = next(
        (
            msg
            for msg in reversed(generated_turn_messages)
            if msg.get("role") == "assistant" and msg.get("content")
        ),
        None,
    )
    assert final_assistant_message is not None, (
        "No final assistant message with content found"
    )
    sent_text = final_assistant_message["content"]

    assert EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text, (
        f"Final response did not contain the expected converted time (SSE). Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    )
    # Assertions on the final message content
    assert EXPECTED_CONVERTED_TIME_FRAGMENT in sent_text, (
        f"Final response (SSE) did not contain the expected converted time. Sent: '{sent_text}' Expected fragment: '{EXPECTED_CONVERTED_TIME_FRAGMENT}'"
    )

    assert final_response_text in sent_text, (
        f"Final response did not match the mock LLM's final rule output (SSE). Sent: '{sent_text}' Expected: '{final_response_text}'"
    )
    logger.info(
        f"Verified MCP tool '{MCP_TIME_TOOL_NAME}' was called via SSE and final response contained expected fragment."
    )
    await processing_service.close()
