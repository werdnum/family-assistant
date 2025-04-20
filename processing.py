import logging
import json
import asyncio
import uuid  # Added for unique task IDs
from datetime import datetime, timezone  # Added timezone
from typing import List, Dict, Any, Optional, Callable

from dateutil.parser import isoparse  # Added for parsing datetime strings

from litellm import acompletion

# Use ChatCompletionMessageParam as suggested by the error, or rely on inference
# Let's try the suggestion first.
# Removed ToolCall import due to ImportError
from litellm.types.completion import ChatCompletionMessageParam

# Import storage functions for tools
import storage

# from storage import enqueue_task # Removed specific import

# MCP state (mcp_sessions, tool_name_to_server_id) will be passed as arguments
# Removed: from main import mcp_sessions, tool_name_to_server_id

logger = logging.getLogger(__name__)

# --- Tool Implementation ---


async def schedule_future_callback_tool(callback_time: str, context: str, chat_id: int):
    """
    Schedules a task to trigger an LLM callback in a specific chat at a future time.

    Args:
        callback_time: ISO 8601 formatted datetime string (including timezone).
        context: The context/prompt for the future LLM callback.
        chat_id: The chat ID where the callback should occur.
    """
    try:
        # Parse the ISO 8601 string, ensuring it's timezone-aware
        scheduled_dt = isoparse(callback_time)
        if scheduled_dt.tzinfo is None:
            # Or raise error, forcing LLM to provide timezone
            logger.warning(
                f"Callback time '{callback_time}' lacks timezone. Assuming UTC."
            )
            scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)

        # Ensure it's in the future (optional, but good practice)
        if scheduled_dt <= datetime.now(timezone.utc):
            raise ValueError("Callback time must be in the future.")

        task_id = f"llm_callback_{uuid.uuid4()}"
        payload = {"chat_id": chat_id, "callback_context": context}

        # TODO: Need access to the new_task_event from main.py to notify worker
        # For now, enqueue without immediate notification. Refactor may be needed
        # if immediate notification is desired here.
        await storage.enqueue_task(  # Use storage.enqueue_task
            task_id=task_id,
            task_type="llm_callback",
            payload=payload,
            scheduled_at=scheduled_dt,
            # notify_event=new_task_event # Needs event passed down
        )
        logger.info(
            f"Scheduled LLM callback task {task_id} for chat {chat_id} at {scheduled_dt}"
        )
        return f"OK. Callback scheduled for {callback_time}."
    except ValueError as ve:
        logger.error(f"Invalid callback time format or value: {callback_time} - {ve}")
        return f"Error: Invalid callback time provided. Ensure it's a future ISO 8601 datetime with timezone. {ve}"
    except Exception as e:
        logger.error(f"Failed to schedule callback task: {e}", exc_info=True)
        return "Error: Failed to schedule the callback."


# Map tool names to their actual functions
AVAILABLE_FUNCTIONS = {
    "add_or_update_note": storage.add_or_update_note,
    "schedule_future_callback": schedule_future_callback_tool,
}

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
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_future_callback",
            "description": "Schedule a future trigger for yourself (the assistant) to continue processing or follow up on a topic at a specified time within the current chat context. Use this if the user asks you to do something later, or if a task requires waiting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "callback_time": {
                        "type": "string",
                        "description": "The exact date and time (ISO 8601 format, including timezone, e.g., '2025-05-10T14:30:00+02:00') when the callback should be triggered.",
                    },
                    "context": {
                        "type": "string",
                        "description": "The specific instructions or information you need to remember for the callback (e.g., 'Follow up on the flight booking status', 'Check if the user replied about the weekend plan').",
                    },
                    "chat_id": {
                        "type": "integer",
                        "description": "The specific instructions or information you need to remember for the callback (e.g., 'Follow up on the flight booking status', 'Check if the user replied about the weekend plan').",
                    },
                    # chat_id is removed, it will be inferred from the current context
                },
                "required": ["callback_time", "context"],
            },
        },
    },
]


# Changed type hint from ToolCall to Any to resolve ImportError
# Added mcp_sessions and tool_name_to_server_id as parameters
async def execute_function_call(
    tool_call: Any,
    chat_id: int, # Add chat_id parameter
    mcp_sessions: Dict[
        str, Any
    ],  # Using Any for ClientSession to avoid MCP import here
    tool_name_to_server_id: Dict[str, str],
) -> Dict[str, Any]:
    """
    Executes a function call requested by the LLM, checking local and MCP tools.

    Injects chat_id for specific local tools like schedule_future_callback.
    """
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
        # Inject chat_id if the tool is schedule_future_callback
        if function_name == "schedule_future_callback":
            function_args["chat_id"] = chat_id # Add/overwrite chat_id from context
            logger.info(f"Injected chat_id {chat_id} into args for {function_name}")

        logger.info(
            f"Executing local tool call: {function_name} with args {function_args}"
        )
        try:
            # Call the function and capture its return value
            function_response_content = await local_function_to_call(**function_args)
            # Log based on whether the tool's response indicates success or error
            if "Error:" not in function_response_content:
                 logger.info(f"Local tool call {function_name} successful.")
            else:
                 # The tool function already logged the specific error internally
                 logger.warning(f"Local tool call {function_name} reported an error: {function_response_content}")
        except Exception as e:
            # This catches errors *calling* the function, not errors *within* the function's try/except
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
    chat_id: int, # Add chat_id parameter
    model: str,
    all_tools: List[Dict[str, Any]],  # Accept the combined tool list
    mcp_sessions: Dict[
        str, Any
    ],  # Using Any for ClientSession to avoid MCP import here
    tool_name_to_server_id: Dict[str, str],
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """
    Sends the conversation history (and tools) to the LLM, handles potential tool calls,
    and returns the final response content along with details of any tool calls made.

    Args:
        messages: A list of message dictionaries.
        model: The identifier of the LLM model.

    Returns:
        A tuple containing:
        - The final response content string from the LLM (or None).
        - A list of dictionaries detailing executed tool calls (or None).
    """
    executed_tool_info: List[Dict[str, Any]] = [] # Initialize list to store tool call details
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
            return None, None # Return None for both content and tool info

        tool_calls = response_message.tool_calls

        # --- Handle Tool Calls (if any) ---
        if tool_calls:
            logger.info(f"LLM requested {len(tool_calls)} tool call(s).")
            # Append the assistant's response message (containing the tool calls)
            messages.append(
                response_message.model_dump()
            )  # Use model_dump for pydantic v2+

            # Execute all tool calls, passing the chat_id and MCP state
            tool_responses = await asyncio.gather(
                *(
                    execute_function_call(
                        tc, chat_id, mcp_sessions, tool_name_to_server_id
                    )
                    for tc in tool_calls
                )
            )

            # Store tool call details before appending responses to history
            # This assumes tool_responses is a list matching tool_calls order
            for i, tool_call in enumerate(tool_calls):
                # Parse arguments again here to store them clearly
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    arguments = {"error": "Failed to parse arguments", "raw": tool_call.function.arguments}

                executed_tool_info.append({
                    "call_id": tool_call.id,
                    "function_name": tool_call.function.name,
                    "arguments": arguments,
                    "response_content": tool_responses[i].get("content", "Error retrieving response content"), # Get content from the response dict
                })

            # Append tool responses to the message history for the next LLM call
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
                # Return final content AND the captured tool info
                return final_content, executed_tool_info
            else:
                logger.warning(
                    f"Second LLM response after tool call was empty or unexpected: {second_response}"
                )
                # Fallback: maybe return the tool execution status directly? And the tool info
                fallback_content = "Tool execution finished, but I couldn't generate a summary."
                return fallback_content, executed_tool_info

        # --- No Tool Calls ---
        elif response_message.content:
            response_content = response_message.content.strip()
            logger.info(
                f"Received LLM response (no tool call): {response_content[:100]}..."
            )
            return response_content, None # No tool calls, return None for tool info
        else:
            logger.warning(
                f"LLM response had neither content nor tool calls: {response}"
            )
            return None, None # Return None for both

    except Exception as e:
        logger.error(
            f"Error during LLM completion or tool handling: {e}", exc_info=True
        )
        return None, None # Return None for both on error
