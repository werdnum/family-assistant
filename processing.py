import logging
import json
import asyncio # Added import
from typing import List, Dict, Any, Optional, Callable

from litellm import acompletion
# Use ChatCompletionMessageParam as suggested by the error, or rely on inference
# Let's try the suggestion first.
from litellm.types.completion import ChatCompletionMessageParam

# Import storage function for the tool
import storage

logger = logging.getLogger(__name__)

# --- Tool Implementation ---

# Map tool names to their actual functions
AVAILABLE_FUNCTIONS = {
    "add_or_update_note": storage.add_or_update_note
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
    }
]

async def execute_function_call(tool_call: Any) -> Dict[str, Any]:
    """Executes a function call requested by the LLM."""
    function_name = tool_call.function.name
    function_to_call = AVAILABLE_FUNCTIONS.get(function_name)
    function_args = json.loads(tool_call.function.arguments)

    if not function_to_call:
        logger.error(f"Unknown function requested by LLM: {function_name}")
        return {
            "tool_call_id": tool_call.id,
            "role": "tool",
            "name": function_name,
            "content": f"Error: Function '{function_name}' not found.",
        }

    logger.info(f"Executing tool call: {function_name} with args {function_args}")
    try:
        # Note: Assumes the function is async. If not, adjust execution.
        await function_to_call(**function_args)
        function_response_content = f"Successfully executed {function_name}."
        logger.info(f"Tool call {function_name} successful.")
    except Exception as e:
        logger.error(f"Error executing tool {function_name}: {e}", exc_info=True)
        function_response_content = f"Error executing function '{function_name}': {e}"

    return {
        "tool_call_id": tool_call.id,
        "role": "tool",
        "name": function_name,
        "content": function_response_content,
    }


async def get_llm_response(
    messages: List[Dict[str, Any]],
    model: str
    # tools parameter removed
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
    logger.info(f"Sending {len(messages)} messages to LLM ({model}). Last message: {messages[-1]['content'][:100]}...")
    # Use the locally defined TOOLS_DEFINITION
    if TOOLS_DEFINITION:
        logger.info(f"Providing {len(TOOLS_DEFINITION)} tools to LLM.")

    try:
        # --- First LLM Call ---
        response = await acompletion(
            model=model,
            messages=messages,
            tools=TOOLS_DEFINITION, # Use local definition
            tool_choice="auto" if TOOLS_DEFINITION else None, # Let LLM decide if/which tool to use
        )

        # Adjust type hint based on the import change
        response_message: Optional[ChatCompletionMessageParam] = response.choices[0].message if response.choices else None

        if not response_message:
            logger.warning(f"LLM response structure unexpected or empty: {response}")
            return None

        tool_calls = response_message.tool_calls

        # --- Handle Tool Calls (if any) ---
        if tool_calls:
            logger.info(f"LLM requested {len(tool_calls)} tool call(s).")
            # Append the assistant's response message (containing the tool calls)
            messages.append(response_message.model_dump()) # Use model_dump for pydantic v2+

            # Execute all tool calls
            tool_responses = await asyncio.gather(*(execute_function_call(tc) for tc in tool_calls))

            # Append tool responses to the message history
            messages.extend(tool_responses)

            # --- Second LLM Call ---
            logger.info("Sending updated messages back to LLM after tool execution.")
            second_response = await acompletion(
                model=model,
                messages=messages,
                # No tools needed for the second call, we just want the summary
            )
            second_response_message = second_response.choices[0].message if second_response.choices else None

            if second_response_message and second_response_message.content:
                final_content = second_response_message.content.strip()
                logger.info(f"Received final LLM response after tool call: {final_content[:100]}...")
                return final_content
            else:
                logger.warning(f"Second LLM response after tool call was empty or unexpected: {second_response}")
                # Fallback: maybe return the tool execution status directly?
                return "Tool execution finished, but I couldn't generate a summary."

        # --- No Tool Calls ---
        elif response_message.content:
            response_content = response_message.content.strip()
            logger.info(f"Received LLM response (no tool call): {response_content[:100]}...")
            return response_content
        else:
            logger.warning(f"LLM response had neither content nor tool calls: {response}")
            return None

    except Exception as e:
        logger.error(f"Error during LLM completion or tool handling: {e}", exc_info=True)
        return None
