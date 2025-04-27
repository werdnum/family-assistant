import logging
import json
import asyncio
import uuid  # Added for unique task IDs
from datetime import datetime, timezone  # Added timezone
from typing import List, Dict, Any, Optional, Callable, Tuple  # Added Tuple

from dateutil.parser import isoparse  # Added for parsing datetime strings

import logging
import json
import asyncio
import uuid
from typing import List, Dict, Any, Optional, Tuple

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput

# Import ToolsProvider interface and context
from .tools import ToolsProvider, ToolExecutionContext, ToolNotFoundError

# Import Application type hint
from telegram.ext import Application

logger = logging.getLogger(__name__)


# --- Processing Service Class ---
# Tool definitions and implementations are now moved to tools.py


class ProcessingService:
    """
    Encapsulates the logic for processing messages, interacting with the LLM,
    and handling tool calls.
    """

    def __init__(self, llm_client: LLMInterface, tools_provider: ToolsProvider):
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            tools_provider: An object implementing the ToolsProvider protocol.
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider

    # Removed _execute_function_call method

    async def process_message(
        self,
        messages: List[Dict[str, Any]],
        chat_id: int,
        application: Application,  # Added application instance for context
        # all_tools argument removed, will be fetched from provider
    ) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
        """
        Sends the conversation history to the LLM via the injected client,
        handles potential tool calls using the injected tools provider,
        and returns the final response content along with details of any tool calls made.

        Args:
            messages: A list of message dictionaries for the LLM.
            chat_id: The chat ID for context.
            application: The Telegram Application instance for context.

        Returns:
            A tuple containing:
            - The final response content string from the LLM (or None).
            - A list of dictionaries detailing executed tool calls (or None).
        """
        executed_tool_info: List[Dict[str, Any]] = []
        logger.info(
            f"Processing {len(messages)} messages for chat {chat_id}. Last message: {messages[-1]['content'][:100]}..."
        )

        try:
            # --- Get Tool Definitions ---
            all_tools = await self.tools_provider.get_tool_definitions()
            if all_tools:
                logger.info(f"Providing {len(all_tools)} tools to LLM.")
            else:
                logger.info("No tools available from provider.")

            # --- First LLM Call via Injected Client ---
            llm_output: LLMOutput = await self.llm_client.generate_response(
                messages=messages,
                tools=all_tools,
                tool_choice="auto" if all_tools else None,
            )

            tool_calls = (
                llm_output.tool_calls
            )  # Get tool calls from the standardized output

            # --- Handle Tool Calls (if any) ---
            if tool_calls:
                logger.info(f"LLM requested {len(tool_calls)} tool call(s).")

                # Append the assistant's response message (containing the tool calls)
                # Need to reconstruct the message dict format expected by the LLM API
                assistant_message_with_calls = {
                    "role": "assistant",
                    "content": llm_output.content,
                }
                if tool_calls:  # Add tool_calls key only if present
                    assistant_message_with_calls["tool_calls"] = tool_calls
                messages.append(assistant_message_with_calls)

                # --- Execute Tool Calls using ToolsProvider ---
                tool_response_messages = []
                tool_execution_context = ToolExecutionContext(
                    chat_id=chat_id, application=application
                )

                for tool_call_dict in tool_calls:
                    call_id = tool_call_dict.get("id")
                    function_info = tool_call_dict.get("function", {})
                    function_name = function_info.get("name")
                    function_args_str = function_info.get("arguments", "{}")

                    if not call_id or not function_name:
                        logger.error(
                            f"Skipping invalid tool call dict: {tool_call_dict}"
                        )
                        # Add an error response message for this specific call
                        tool_response_messages.append(
                            {
                                "tool_call_id": call_id or f"missing_id_{uuid.uuid4()}",
                                "role": "tool",
                                "name": function_name or "unknown_function",
                                "content": "Error: Invalid tool call structure received from LLM.",
                            }
                        )
                        continue

                    try:
                        arguments = json.loads(function_args_str)
                    except json.JSONDecodeError:
                        logger.error(
                            f"Failed to parse arguments for tool call {function_name}: {function_args_str}"
                        )
                        arguments = {
                            "error": "Failed to parse arguments"
                        }  # Store error for logging
                        tool_response_content = (
                            f"Error: Invalid arguments format for {function_name}."
                        )
                    else:
                        # Arguments parsed successfully, execute the tool
                        try:
                            tool_response_content = (
                                await self.tools_provider.execute_tool(
                                    name=function_name,
                                    arguments=arguments,
                                    context=tool_execution_context,
                                )
                            )
                        except ToolNotFoundError as tnfe:
                            logger.error(f"Tool execution failed: {tnfe}")
                            tool_response_content = f"Error: {tnfe}"
                        except Exception as exec_err:
                            logger.error(
                                f"Unexpected error executing tool {function_name}: {exec_err}",
                                exc_info=True,
                            )
                            tool_response_content = (
                                f"Error: Unexpected error executing {function_name}."
                            )

                    # Store details for history, including the original tool_call_id
                    executed_tool_info.append(
                        {
                            "tool_call_id": call_id, # Add the original ID here
                            "function_name": function_name,
                            "arguments": arguments,  # Store parsed args (or error dict)
                            "response_content": tool_response_content,
                        }
                    )

                    # Append the response message for the LLM
                    tool_response_messages.append(
                        {
                            "tool_call_id": call_id,
                            "role": "tool",
                            "name": function_name,
                            "content": tool_response_content,
                        }
                    )

                # Append all tool response messages to the history for the next LLM call
                messages.extend(tool_response_messages)

                # --- Second LLM Call ---
                logger.info(
                    "Sending updated messages back to LLM after tool execution."
                )
                second_llm_output: LLMOutput = await self.llm_client.generate_response(
                    messages=messages,
                    tools=all_tools,
                    tool_choice=(
                        "auto" if all_tools else None
                    ),  # Allow tools again? Or force "none"? Let's allow for now.
                )

                # --- Handle potential second-level tool calls (optional) ---
                if second_llm_output.tool_calls:
                    logger.warning(
                        "LLM requested further tool calls after initial execution. These are currently ignored."
                    )
                    # Implement recursive call or loop here if needed.

                if second_llm_output.content:
                    final_content = second_llm_output.content.strip()
                    logger.info(
                        f"Received final LLM response after tool call: {final_content[:100]}..."
                    )
                    return final_content, executed_tool_info
                else:
                    logger.warning("Second LLM response after tool call was empty.")
                    fallback_content = (
                        "Tool execution finished, but I couldn't generate a summary."
                    )
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
                f"Error during LLM interaction or tool handling in ProcessingService: {e}",
                exc_info=True,
            )
            # Ensure tuple is returned even on error
            return None, None
