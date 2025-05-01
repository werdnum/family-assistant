import logging
import json
import asyncio
import uuid  # Added for unique task IDs
from datetime import datetime, timezone  # Added timezone
from typing import List, Dict, Any, Optional, Callable, Tuple, Union # Added Union

from dateutil.parser import isoparse  # Added for parsing datetime strings

import logging
import json
import asyncio
import uuid
from datetime import datetime, timedelta, timezone  # Added
from typing import List, Dict, Any, Optional, Tuple

import pytz  # Added

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput

# Import ToolsProvider interface and context
from .tools import ToolsProvider, ToolExecutionContext, ToolNotFoundError

# Import Application type hint
from telegram.ext import Application

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext

# Import storage and calendar integration for context building
from family_assistant import storage
from family_assistant import calendar_integration


logger = logging.getLogger(__name__)


# --- Processing Service Class ---
# Tool definitions and implementations are now moved to tools.py


class ProcessingService:
    """
    Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        tools_provider: ToolsProvider,
        prompts: Dict[str, str],
        calendar_config: Dict[str, Any],
        timezone_str: str,
        max_history_messages: int,
        history_max_age_hours: int, # Recommended value is now 1
    ):
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            tools_provider: An object implementing the ToolsProvider protocol.
            prompts: Dictionary containing loaded prompts.
            calendar_config: Dictionary containing calendar configuration.
            timezone_str: The configured timezone string (e.g., "Europe/London").
            max_history_messages: Max number of history messages to fetch.
            history_max_age_hours: Max age of history messages to fetch (in hours). Recommended: 1.
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider
        self.prompts = prompts
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.history_max_age_hours = history_max_age_hours
        # Store the confirmation callback function if provided at init? No, get from context.

    # Removed _execute_function_call method (if it was previously here)
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.history_max_age_hours = history_max_age_hours

    # Removed _execute_function_call method (if it was previously here)

    async def process_message(
        self,
        db_context: DatabaseContext,  # Added db_context
        messages: List[Dict[str, Any]],
        chat_id: int,
        application: Application,
        # Add the confirmation callback function from TelegramService/Handler
        request_confirmation_callback: Optional[Callable[..., Awaitable[bool]]] = None,
    ) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
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
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
        """
        executed_tool_info: List[Dict[str, Any]] = []
        final_reasoning_info: Optional[Dict[str, Any]] = None # Added
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
            # Store reasoning from the first call (might be overwritten if tools are called)
            final_reasoning_info = llm_output.reasoning_info

            tool_calls = llm_output.tool_calls # Get tool calls from the standardized output

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
                # Create execution context including db_context and calendar_config
                tool_execution_context = ToolExecutionContext(
                    chat_id=chat_id,
                    db_context=db_context,
                    calendar_config=self.calendar_config, # Pass calendar config from service
                    application=application,
                    timezone_str=self.timezone_str, # Pass timezone string
                    # Pass the confirmation callback into the context
                    request_confirmation_callback=request_confirmation_callback,
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
                            "tool_call_id": call_id,  # Add the original ID here
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
                    # Store reasoning from the second call
                    final_reasoning_info = second_llm_output.reasoning_info
                    return final_content, executed_tool_info, final_reasoning_info # Return reasoning
                else:
                    logger.warning("Second LLM response after tool call was empty.")
                    fallback_content = (
                        "Tool execution finished, but I couldn't generate a summary."
                    )
                    # Store reasoning from the second call even if content is empty
                    final_reasoning_info = second_llm_output.reasoning_info
                    return fallback_content, executed_tool_info, final_reasoning_info # Return reasoning

            # --- No Tool Calls ---
            elif llm_output.content:
                response_content = llm_output.content.strip()
                logger.info(
                    f"Received LLM response (no tool call): {response_content[:100]}..."
                )
                # Reasoning info already stored from the first call
                return response_content, None, final_reasoning_info # Return reasoning
            else:
                logger.warning("LLM response had neither content nor tool calls.")
                 # Store reasoning info even if response is empty
                final_reasoning_info = llm_output.reasoning_info
                return None, None, final_reasoning_info # Return reasoning

        except Exception as e:
            logger.error(
                f"Error during LLM interaction or tool handling in ProcessingService: {e}",
                exc_info=True,
            )
            # Ensure tuple is returned even on error, include None for reasoning
            return None, None, None

    async def generate_llm_response_for_chat(
        self,
        db_context: DatabaseContext,  # Added db_context
        application: Application,
        chat_id: int,
        trigger_content_parts: List[Dict[str, Any]],
        user_name: str,
        # Add confirmation callback parameter
        request_confirmation_callback: Optional[Callable[..., Awaitable[bool]]] = None,
    ) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]], Optional[str]]:
        """
        Prepares context, message history, calls the LLM processing logic,
        and returns the response, tool info, reasoning info, and any processing error traceback.

        Args:
            db_context: The database context to use for storage operations.
            application: The Telegram application instance.
            chat_id: The target chat ID.
            trigger_content_parts: List of content parts for the triggering message.
            user_name: The user name to format into the system prompt.

        Returns:
            A tuple: (LLM response string or None, List of tool call info dicts or None, Reasoning info dict or None, Error traceback string or None).
        """
        error_traceback: Optional[str] = None # Initialize traceback
        logger.debug(
            f"Generating LLM response for chat {chat_id}, triggered by: {trigger_content_parts[0].get('type', 'unknown')}"
        )

        # --- History and Context Preparation ---
        messages: List[Dict[str, Any]] = []
        try:
            history_messages = (
                await storage.get_recent_history(  # Use storage directly with context
                    db_context=db_context,  # Pass context
                    chat_id=chat_id,
                    limit=self.max_history_messages,  # Use self attribute
                    max_age=timedelta(
                        hours=self.history_max_age_hours
                    ),  # Use self attribute
                )
            )
        except Exception as hist_err:
            logger.error(
                f"Failed to get message history for chat {chat_id}: {hist_err}",
                exc_info=True,
            )
            history_messages = []  # Continue with empty history on error

        # Process history messages, formatting assistant tool calls correctly
        for msg in history_messages:
            if msg["role"] == "assistant" and "tool_calls_info_raw" in msg:
                # Construct the 'tool_calls' structure expected by LiteLLM/OpenAI
                # from the stored 'tool_calls_info_raw'
                reformatted_tool_calls = []
                raw_info_list = msg.get("tool_calls_info_raw", [])
                if isinstance(raw_info_list, list):  # Ensure it's a list
                    for raw_call in raw_info_list:
                        # Assuming raw_call is a dict like:
                        # {'call_id': '...', 'function_name': '...', 'arguments': {...}, 'response_content': '...'}
                        if isinstance(raw_call, dict):
                            reformatted_tool_calls.append(
                                {
                                    "id": raw_call.get(
                                        "call_id", f"call_{uuid.uuid4()}"
                                    ),  # Generate ID if missing
                                    "type": "function",
                                    "function": {
                                        "name": raw_call.get(
                                            "function_name", "unknown_tool"
                                        ),
                                        # Arguments should already be a JSON string or dict from storage
                                        "arguments": (
                                            json.dumps(raw_call.get("arguments", {}))
                                            if isinstance(
                                                raw_call.get("arguments"), dict
                                            )
                                            else raw_call.get("arguments", "{}")
                                        ),
                                    },
                                }
                            )
                            # Also need to append the corresponding tool response message
                            # The history mechanism currently stores this *within* the assistant message's info block.
                            # We need separate 'assistant' (with tool_calls) and 'tool' messages.
                            # Let's adjust history storage/retrieval or formatting here.

                            # OPTION 1 (Simpler formatting here): Append tool response immediately after
                            messages.append(
                                {
                                    "role": "assistant",
                                    "content": msg.get("content") or '',
                                    "tool_calls": reformatted_tool_calls,
                                }
                            )
                            # Now append the corresponding tool result message for each call
                            for raw_call in raw_info_list:
                                if isinstance(raw_call, dict):
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": raw_call.get(
                                                "call_id", "missing_id"
                                            ),
                                            # Ensure content is always a string for tool role
                                            "content": str(raw_call.get(
                                                "response_content",
                                                "Error: Missing tool response content",
                                            )),
                                        }
                                    )
                        else:
                            logger.warning(
                                f"Skipping non-dict item in raw_tool_calls_info: {raw_call}"
                            )
                else:
                    logger.warning(
                        f"Expected list for raw_tool_calls_info, got {type(raw_info_list)}. Skipping tool call reconstruction."
                    )
                # Skip adding the original combined message dictionary 'msg' as we added parts separately
            elif msg["role"] != "error": # Don't include previous error messages in history sent to LLM
                messages.append({"role": msg["role"], "content": msg["content"] or ''})

        # messages.extend(history_messages) # Removed this line, processing is now done above
        logger.debug(
            f"Processed {len(history_messages)} DB history messages into LLM format (excluding 'error' role)."
        )

        # --- Prepare System Prompt Context ---
        system_prompt_template = self.prompts.get(  # Use self.prompts
            "system_prompt",
            "You are a helpful assistant. Current time is {current_time}.",
        )
        try:
            local_tz = pytz.timezone(self.timezone_str)  # Use self.timezone_str
            current_local_time = datetime.now(local_tz)
            current_time_str = current_local_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception as tz_err:
            logger.error(
                f"Error applying timezone {self.timezone_str}: {tz_err}. Defaulting time format."
            )
            current_time_str = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )

        calendar_context_str = ""
        if self.calendar_config:  # Use self.calendar_config
            try:
                upcoming_events = await calendar_integration.fetch_upcoming_events(
                    self.calendar_config  # Use self.calendar_config
                )
                today_events_str, future_events_str = (
                    calendar_integration.format_events_for_prompt(
                        upcoming_events, self.prompts
                    )  # Use self.prompts
                )
                calendar_header_template = self.prompts.get(  # Use self.prompts
                    "calendar_context_header",
                    "{today_tomorrow_events}\n{next_two_weeks_events}",
                )
                calendar_context_str = calendar_header_template.format(
                    today_tomorrow_events=today_events_str,
                    next_two_weeks_events=future_events_str,
                ).strip()
            except Exception as cal_err:
                logger.error(
                    f"Failed to fetch or format calendar events: {cal_err}",
                    exc_info=True,
                )
                calendar_context_str = (
                    f"Error retrieving calendar events: {str(cal_err)}"
                )
        else:
            calendar_context_str = "Calendar integration not configured."

        notes_context_str = ""
        try:
            all_notes = await storage.get_all_notes(
                db_context=db_context
            )  # Use storage directly with context
            if all_notes:
                notes_list_str = ""
                note_item_format = self.prompts.get(
                    "note_item_format", "- {title}: {content}"
                )  # Use self.prompts
                for note in all_notes:
                    notes_list_str += (
                        note_item_format.format(
                            title=note["title"], content=note["content"]
                        )
                        + "\n"
                    )
                notes_context_header_template = self.prompts.get(  # Use self.prompts
                    "notes_context_header", "Relevant notes:\n{notes_list}"
                )
                notes_context_str = notes_context_header_template.format(
                    notes_list=notes_list_str.strip()
                )
            else:
                notes_context_str = self.prompts.get(
                    "no_notes", "No notes available."
                )  # Use self.prompts
        except Exception as note_err:
            logger.error(f"Failed to get notes for context: {note_err}", exc_info=True)
            notes_context_str = "Error retrieving notes."

        final_system_prompt = system_prompt_template.format(
            user_name=user_name,
            current_time=current_time_str,
            calendar_context=calendar_context_str,
            notes_context=notes_context_str,
        ).strip()

        if final_system_prompt:
            messages.insert(0, {"role": "system", "content": final_system_prompt})
            logger.debug("Prepended system prompt to LLM messages.")
        else:
            logger.warning("Generated empty system prompt.")

        # --- Add the triggering message content ---
        # Note: For callbacks, the role might ideally be 'system' or a specific 'callback' role,
        # but using 'user' is often necessary for the LLM to properly attend to it as the primary input.
        # If LLM behavior is odd, consider experimenting with the role here.
        # Simplify content if it's just a single text part, otherwise keep the list structure
        trigger_content: Union[str, List[Dict[str, Any]]]
        if (
            isinstance(trigger_content_parts, list)
            and len(trigger_content_parts) == 1
            and isinstance(trigger_content_parts[0], dict)
            and trigger_content_parts[0].get("type") == "text"
            and "text" in trigger_content_parts[0]
        ):
            trigger_content = trigger_content_parts[0]["text"]
            logger.debug("Simplified single text part content to string.")
        else:
            trigger_content = trigger_content_parts # Keep as list for multi-part/non-text
            logger.debug("Keeping trigger content as list (multi-part or non-text).")

        trigger_message = {
            "role": "user",  # Treat trigger as user input for processing flow
            "content": trigger_content,
        }
        messages.append(trigger_message)
        logger.debug(f"Appended trigger message to LLM history: {trigger_message}")

        # --- Call Processing Logic ---
        # Tool definitions are now fetched inside process_message
        try:
            # Call the process_message method of the *same* instance (self)
            # Unpack all three return values: content, tool_info, reasoning_info
            llm_response_content, tool_info, reasoning_info = await self.process_message(
                db_context=db_context,  # Pass context
                messages=messages,
                chat_id=chat_id,
                application=application,
                # Pass the confirmation callback down
                request_confirmation_callback=request_confirmation_callback,
            )
            # Return content, tool info, and reasoning info
            return llm_response_content, tool_info, reasoning_info, None # No error traceback
        except Exception as e:
            logger.error(
                f"Error during ProcessingService interaction for chat {chat_id}: {e}",
                exc_info=True,
            )
            # Capture traceback on error
            import traceback
            error_traceback = traceback.format_exc()
            # Return None for content/tools/reasoning, but include the traceback
            return None, None, None, error_traceback
