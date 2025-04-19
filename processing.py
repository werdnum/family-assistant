import logging
import json
import asyncio  # Added import
from typing import List, Dict, Any, Optional, Callable

from litellm import acompletion

# Use ChatCompletionMessageParam as suggested by the error, or rely on inference
# Let's try the suggestion first.
# Removed ToolCall import due to ImportError
from litellm.types.completion import ChatCompletionMessageParam

# Import storage function for the tool
import storage

# MCP state (mcp_sessions, tool_name_to_server_id) will be passed as arguments
# Removed: from main import mcp_sessions, tool_name_to_server_id

logger = logging.getLogger(__name__)

# --- Tool Implementation ---

# Map tool names to their actual functions
AVAILABLE_FUNCTIONS = {"add_or_update_note": storage.add_or_update_note}

# Define tools in the format LiteLLM expects (OpenAI format)
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "add_or_update_note",
            "description": "Add a new note or update an existing note with the given title. Use this to remember information provided by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The unique title of the note.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the note.",
                    },
                },
                "required": ["title", "content"],
            },
        },
    }
]


# Changed type hint from ToolCall to Any to resolve ImportError
# Added mcp_sessions and tool_name_to_server_id as parameters
async def execute_function_call(
    tool_call: Any,
    mcp_sessions: Dict[
        str, Any
    ],  # Using Any for ClientSession to avoid MCP import here
    tool_name_to_server_id: Dict[str, str],
) -> Dict[str, Any]:
    """Executes a function call requested by the LLM, checking local and MCP tools."""
    function_name = tool_call.function.name
    try:
        function_args = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError:
        logger.error(
            f"Failed to parse arguments for tool call {function_name}: {tool_call.function.arguments}"
        )
        return {
            "tool_call_id": tool_call.id,
            "role": "tool",
            "name": function_name,
            "content": f"Error: Invalid arguments format for {function_name}.",
        }

    # Check if it's a local tool first
    local_function_to_call = AVAILABLE_FUNCTIONS.get(function_name)
    if local_function_to_call:
        logger.info(
            f"Executing local tool call: {function_name} with args {function_args}"
        )
        try:
            await local_function_to_call(**function_args)
            function_response_content = f"Successfully executed {function_name}."
            logger.info(f"Local tool call {function_name} successful.")
        except Exception as e:
            logger.error(
                f"Error executing local tool {function_name}: {e}", exc_info=True
            )
            function_response_content = (
                f"Error executing function '{function_name}': {e}"
            )
        return {
            "tool_call_id": tool_call.id,
            "role": "tool",
            "name": function_name,
            "content": function_response_content,
        }

    # If not local, check if it's an MCP tool
    server_id = tool_name_to_server_id.get(function_name)
    if server_id:
        session = mcp_sessions.get(server_id)
        if session:
            logger.info(
                f"Executing MCP tool call: {function_name} on server '{server_id}' with args {function_args}"
            )
            try:
                # Progress reporting can be added here if needed using _meta/progressToken
                mcp_result = await session.call_tool(
                    name=function_name, arguments=function_args
                )

                # Process MCP result content (list of TextContent, ImageContent, etc.)
                # For now, just concatenate text parts. Handle other types as needed.
                response_parts = []
                if mcp_result.content:
                    for content_item in mcp_result.content:
                        if hasattr(content_item, "text") and content_item.text:
                            response_parts.append(content_item.text)
                        # Add handling for other content types (image, audio, resource) if necessary
                        # elif hasattr(content_item, 'uri'): # Example for resource
                        #    response_parts.append(f"Resource available at {content_item.uri}")

                function_response_content = (
                    "\n".join(response_parts)
                    if response_parts
                    else "Tool executed successfully."
                )

                if mcp_result.isError:
                    logger.error(
                        f"MCP tool call {function_name} on server '{server_id}' returned an error: {function_response_content}"
                    )
                    # Prepend error indication for clarity to LLM
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
                "tool_call_id": tool_call.id,
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
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": function_response_content,
            }

    # If not local and not MCP, it's unknown
    logger.error(f"Unknown function requested by LLM: {function_name}")
    return {
        "tool_call_id": tool_call.id,
        "role": "tool",
        "name": function_name,
        "content": f"Error: Function or tool '{function_name}' not found.",
    }


# Added mcp_sessions and tool_name_to_server_id as parameters
async def get_llm_response(
    messages: List[Dict[str, Any]],
    model: str,
    all_tools: List[Dict[str, Any]],  # Accept the combined tool list
    mcp_sessions: Dict[
        str, Any
    ],  # Using Any for ClientSession to avoid MCP import here
    tool_name_to_server_id: Dict[str, str],
) -> str | None:
    """
    Sends the conversation history (and tools) to the LLM,
    handles potential tool calls, and returns the final response content.

    Args:
        messages: A list of message dictionaries.
        model: The identifier of the LLM model.

    Returns:
        The final response content string from the LLM, or None if an error occurs.
    """
    logger.info(
        f"Sending {len(messages)} messages to LLM ({model}). Last message: {messages[-1]['content'][:100]}..."
    )
    # Use the provided list of all tools
    if all_tools:
        logger.info(f"Providing {len(all_tools)} tools to LLM.")

    try:
        # --- First LLM Call ---
        response = await acompletion(
            model=model,
            messages=messages,
            tools=all_tools,  # Use combined list
            tool_choice=(
                "auto" if all_tools else None
            ),  # Let LLM decide if/which tool to use
        )

        # Adjust type hint based on the import change
        response_message: Optional[ChatCompletionMessageParam] = (
            response.choices[0].message if response.choices else None
        )

        if not response_message:
            logger.warning(f"LLM response structure unexpected or empty: {response}")
            return None

        tool_calls = response_message.tool_calls

        # --- Handle Tool Calls (if any) ---
        if tool_calls:
            logger.info(f"LLM requested {len(tool_calls)} tool call(s).")
            # Append the assistant's response message (containing the tool calls)
            messages.append(
                response_message.model_dump()
            )  # Use model_dump for pydantic v2+

            # Execute all tool calls, passing the MCP state
            tool_responses = await asyncio.gather(
                *(
                    execute_function_call(tc, mcp_sessions, tool_name_to_server_id)
                    for tc in tool_calls
                )
            )

            # Append tool responses to the message history
            messages.extend(tool_responses)

            # --- Second LLM Call ---
            logger.info("Sending updated messages back to LLM after tool execution.")
            # Send tool list again in case the LLM needs to chain calls, though less common
            second_response = await acompletion(
                model=model,
                messages=messages,
                tools=all_tools,
                tool_choice="auto" if all_tools else None,
            )
            second_response_message = (
                second_response.choices[0].message if second_response.choices else None
            )

            # --- Handle potential second-level tool calls (optional, adds complexity) ---
            # If second_response_message contains tool_calls, you'd repeat the
            # tool execution logic here. For simplicity, we assume the second call
            # is primarily for summarizing the first tool's result.
            # If second_response_message.tool_calls:
            #    logger.info("LLM requested further tool calls after initial execution.")
            #    # ... recursive call or loop ...

            if second_response_message and second_response_message.content:
                final_content = second_response_message.content.strip()
                logger.info(
                    f"Received final LLM response after tool call: {final_content[:100]}..."
                )
                return final_content
            else:
                logger.warning(
                    f"Second LLM response after tool call was empty or unexpected: {second_response}"
                )
                # Fallback: maybe return the tool execution status directly?
                return "Tool execution finished, but I couldn't generate a summary."

        # --- No Tool Calls ---
        elif response_message.content:
            response_content = response_message.content.strip()
            logger.info(
                f"Received LLM response (no tool call): {response_content[:100]}..."
            )
            return response_content
        else:
            logger.warning(
                f"LLM response had neither content nor tool calls: {response}"
            )
            return None

    except Exception as e:
        logger.error(
            f"Error during LLM completion or tool handling: {e}", exc_info=True
        )
        return None
