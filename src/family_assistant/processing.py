import logging
import json
import asyncio
import uuid  # Added for unique task IDs
from datetime import datetime, timezone  # Added timezone
from typing import List, Dict, Any, Optional, Callable, Tuple  # Added Tuple

from dateutil.parser import isoparse  # Added for parsing datetime strings

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput, LiteLLMClient

# Import storage functions for tools
from family_assistant import storage

# from family_assistant.storage import enqueue_task # Example if specific import needed

# MCP state (mcp_sessions, tool_name_to_server_id) will be passed as arguments
# Removed: from main import mcp_sessions, tool_name_to_server_id

from dateutil import rrule  # Added for validating recurrence rule

logger = logging.getLogger(__name__)


# --- Processing Service Class ---
# Tool definitions and implementations are now moved to tools.py

class ProcessingService:
    """
    Encapsulates the logic for processing messages, interacting with the LLM,
    and handling tool calls.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        mcp_sessions: Dict[str, Any], # Using Any for ClientSession to avoid MCP import here
        tool_name_to_server_id: Dict[str, str],
    ):
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            mcp_sessions: Dictionary mapping MCP server IDs to their client sessions.
            tool_name_to_server_id: Dictionary mapping MCP tool names to server IDs.
        """
        self.llm_client = llm_client
        # Dependencies for tool execution are removed, will be handled by injected ToolsProvider later
        # self.mcp_sessions = mcp_sessions
        # self.tool_name_to_server_id = tool_name_to_server_id
        # TODO: Inject ToolsProvider here in the next step

    async def _execute_function_call( # TODO: This method will be removed when ToolsProvider is injected
        self,
        tool_call: Dict[str, Any], # Expecting dict from LLMOutput now
        chat_id: int,
    ) -> Dict[str, Any]:
        """
        Executes a function call requested by the LLM, checking local and MCP tools.
        (Internal method)

        Args:
            tool_call: A dictionary representing the tool call from LLMOutput.
                       Expected keys: 'id', 'type' ('function'), 'function' {'name', 'arguments'}
            chat_id: The chat ID for context.

        Returns:
            A dictionary representing the tool response message for the LLM.
        """
        call_id = tool_call.get("id")
        if not call_id:
            logger.error("Tool call dictionary missing 'id'.")
            # Generate a fallback ID? Or return error immediately?
            call_id = f"missing_id_{uuid.uuid4()}" # Fallback ID

        function_info = tool_call.get("function", {})
        function_name = function_info.get("name")
        function_args_str = function_info.get("arguments", "{}")

        if not function_name:
            logger.error("Tool call dictionary missing function name.")
            return {
                "tool_call_id": call_id,
                "role": "tool",
                "name": "unknown_function",
                "content": "Error: Tool call data missing function name.",
            }

        try:
            function_args = json.loads(function_args_str)
        except json.JSONDecodeError:
            logger.error(
                f"Failed to parse arguments for tool call {function_name}: {function_args_str}"
            )
            return {
                "tool_call_id": call_id,
                "role": "tool",
                "name": function_name,
                "content": f"Error: Invalid arguments format for {function_name}.",
            }

        # Check if it's a local tool first (using module-level AVAILABLE_FUNCTIONS) # TODO: Remove this block
        # This logic will move to LocalToolsProvider.execute_tool
        # local_function_to_call = AVAILABLE_FUNCTIONS.get(function_name) # TODO: Remove
        local_function_to_call = None # Placeholder
        if local_function_to_call: # TODO: Remove this block
            # Inject chat_id if the tool is schedule_future_callback # TODO: Remove
            if function_name == "schedule_future_callback":
                function_args["chat_id"] = chat_id
                logger.info(f"Injected chat_id {chat_id} into args for {function_name}")

            logger.info(
                f"Executing local tool call: {function_name} with args {function_args}"
            )
            try:
                function_response_content = await local_function_to_call(**function_args)
                # Ensure content is stringified for the LLM response
                if not isinstance(function_response_content, str):
                    function_response_content = str(function_response_content)

                if "Error:" not in function_response_content:
                     logger.info(f"Local tool call {function_name} successful.")
                else:
                     logger.warning(f"Local tool call {function_name} reported an error: {function_response_content}")

            except Exception as e:
                logger.error(
                    f"Error executing local tool {function_name}: {e}", exc_info=True
                )
                function_response_content = (
                    f"Error executing function '{function_name}': {e}"
                )
            return {
                "tool_call_id": call_id,
                "role": "tool",
                "name": function_name,
                "content": function_response_content,
            }

        # If not local, check if it's an MCP tool (using injected state) # TODO: Remove this block
        # This logic will move to MCPToolsProvider.execute_tool
        # server_id = self.tool_name_to_server_id.get(function_name) # TODO: Remove
        server_id = None # Placeholder
        if server_id: # TODO: Remove this block
            # session = self.mcp_sessions.get(server_id) # TODO: Remove
            session = None # Placeholder
            if session: # TODO: Remove this block
                logger.info( # TODO: Remove
                    f"Executing MCP tool call: {function_name} on server '{server_id}' with args {function_args}"
                )
                try:
                    mcp_result = await session.call_tool(
                        name=function_name, arguments=function_args
                    )
                    response_parts = []
                    if mcp_result.content:
                        for content_item in mcp_result.content:
                            if hasattr(content_item, "text") and content_item.text:
                                response_parts.append(content_item.text)
                    function_response_content = (
                        "\n".join(response_parts)
                        if response_parts
                        else "Tool executed successfully."
                    )
                    if mcp_result.isError:
                        logger.error(
                            f"MCP tool call {function_name} on server '{server_id}' returned an error: {function_response_content}"
                        )
                        function_response_content = f"Error executing tool '{function_name}': {function_response_content}"
                    else:
                        logger.info(
                            f"MCP tool call {function_name} on server '{server_id}' successful."
                        )
                except Exception as e:
                    logger.error(
                        f"Error calling MCP tool {function_name} on server '{server_id}': {e}",
                        exc_info=True,
                    )
                    function_response_content = (
                        f"Error calling MCP tool '{function_name}': {e}"
                    )
                return {
                    "tool_call_id": call_id,
                    "role": "tool",
                    "name": function_name,
                    "content": function_response_content,
                }
            else:
                logger.error(
                    f"Found server_id '{server_id}' for tool '{function_name}', but no active session found."
                )
                function_response_content = (
                    f"Error: Session for tool '{function_name}' not available."
                )
                return {
                    "tool_call_id": call_id,
                    "role": "tool",
                    "name": function_name,
                    "content": function_response_content,
                }

        # If not local and not MCP, it's unknown
        logger.error(f"Unknown function requested by LLM: {function_name}")
        return {
            "tool_call_id": call_id,
            "role": "tool",
            "name": function_name,
            "content": f"Error: Function or tool '{function_name}' not found.",
        }


    async def process_message(
        self,
        messages: List[Dict[str, Any]],
        chat_id: int,
        all_tools: List[Dict[str, Any]], # Accept combined tool list per call
    ) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """
        Sends the conversation history (and tools) to the LLM via the injected client,
        handles potential tool calls using internal methods, and returns the final
        response content along with details of any tool calls made.

        Args:
            messages: A list of message dictionaries for the LLM.
            chat_id: The chat ID for context, primarily for tool execution.
            all_tools: The combined list of local and MCP tools available for this call.

        Returns:
            A tuple containing:
            - The final response content string from the LLM (or None).
            - A list of dictionaries detailing executed tool calls (or None).
        """
        executed_tool_info: List[Dict[str, Any]] = []
        logger.info(
            f"Processing {len(messages)} messages for chat {chat_id}. Last message: {messages[-1]['content'][:100]}..."
        )
        if all_tools:
            logger.info(f"Providing {len(all_tools)} tools to LLM.")

        try:
            # --- First LLM Call via Injected Client ---
            llm_output: LLMOutput = await self.llm_client.generate_response(
                messages=messages,
                tools=all_tools,
                tool_choice="auto" if all_tools else None,
            )

            tool_calls = llm_output.tool_calls # Get tool calls from the standardized output

            # --- Handle Tool Calls (if any) ---
            if tool_calls:
                logger.info(f"LLM requested {len(tool_calls)} tool call(s).")

                # Append the assistant's response message (containing the tool calls)
                # Need to reconstruct the message dict format expected by the LLM API
                assistant_message_with_calls = {"role": "assistant", "content": llm_output.content}
                if tool_calls: # Add tool_calls key only if present
                    assistant_message_with_calls["tool_calls"] = tool_calls
                messages.append(assistant_message_with_calls)

                # Execute all tool calls using the internal method
                tool_responses = await asyncio.gather(
                    *(self._execute_function_call(tc, chat_id) for tc in tool_calls)
                )

                # Store tool call details before appending responses to history
                for i, tool_call_dict in enumerate(tool_calls):
                    # Arguments are already parsed in _execute_function_call, but we need them here.
                    # Re-parse or extract from the dict if needed for logging/storage.
                    try:
                        arguments = json.loads(tool_call_dict.get("function", {}).get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {"error": "Failed to parse arguments"}

                    executed_tool_info.append(
                        {
                            "call_id": tool_call_dict.get("id"),
                            "function_name": tool_call_dict.get("function", {}).get("name"),
                            "arguments": arguments,
                            "response_content": tool_responses[i].get("content", "Error retrieving response content"),
                        }
                    )

                # Append tool responses to the message history for the next LLM call
                messages.extend(tool_responses)

                # --- Second LLM Call ---
                logger.info("Sending updated messages back to LLM after tool execution.")
                second_llm_output: LLMOutput = await self.llm_client.generate_response(
                    messages=messages,
                    tools=all_tools,
                    tool_choice="auto" if all_tools else None, # Allow tools again? Or force "none"? Let's allow for now.
                )

                # --- Handle potential second-level tool calls (optional) ---
                if second_llm_output.tool_calls:
                    logger.warning("LLM requested further tool calls after initial execution. These are currently ignored.")
                    # Implement recursive call or loop here if needed.

                if second_llm_output.content:
                    final_content = second_llm_output.content.strip()
                    logger.info(
                        f"Received final LLM response after tool call: {final_content[:100]}..."
                    )
                    return final_content, executed_tool_info
                else:
                    logger.warning("Second LLM response after tool call was empty.")
                    fallback_content = "Tool execution finished, but I couldn't generate a summary."
                    return fallback_content, executed_tool_info

            # --- No Tool Calls ---
            elif llm_output.content:
                response_content = llm_output.content.strip()
                logger.info(
                    f"Received LLM response (no tool call): {response_content[:100]}..."
                )
                return response_content, None
            else:
                logger.warning("LLM response had neither content nor tool calls.")
                return None, None

        except Exception as e:
            logger.error(
                f"Error during LLM interaction or tool handling in ProcessingService: {e}", exc_info=True
            )
            # Ensure tuple is returned even on error
            # Ensure tuple is returned even on error
            return None, None
