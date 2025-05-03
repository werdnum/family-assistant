import logging
import json
import asyncio
import uuid  # Added for unique task IDs
from datetime import datetime, timezone  # Added timezone
from typing import List, Dict, Any, Optional, Callable, Tuple, Union, Awaitable # Added Union, Awaitable

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
        server_url: Optional[str], # Added server_url
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
            server_url: The base URL of the web server.
            history_max_age_hours: Max age of history messages to fetch (in hours). Recommended: 1.
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider
        self.prompts = prompts
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.server_url = server_url or "http://localhost:8000" # Default if not provided
        self.history_max_age_hours = history_max_age_hours
        # Store the confirmation callback function if provided at init? No, get from context.

    # Removed _execute_function_call method (if it was previously here)
        self.calendar_config = calendar_config
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.server_url = server_url or "http://localhost:8000" # Default if not provided
        self.history_max_age_hours = history_max_age_hours

    # Removed _execute_function_call method (if it was previously here)

    async def process_message(
        self,
        db_context: DatabaseContext,  # Added db_context
        messages: List[Dict[str, Any]],
        chat_id: int,
        application: Application,
        # Update callback signature: It now expects (prompt_text, tool_name, tool_args)
        request_confirmation_callback: Optional[
            Callable[[str, str, Dict[str, Any]], Awaitable[bool]]
        ] = None,
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
        final_reasoning_info: Optional[Dict[str, Any]] = None
        final_content: Optional[str] = None # Store final text response from LLM
        max_iterations = 5 # Safety limit for tool call loops
        current_iteration = 1

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

            # --- Tool Call Loop ---
            while current_iteration <= max_iterations:
                logger.debug(f"Starting LLM interaction loop iteration {current_iteration}/{max_iterations}")

                # --- Log messages being sent to LLM at INFO level ---
                try:
                    logger.info(f"Sending {len(messages)} messages to LLM (iteration {current_iteration}):\n{json.dumps(messages, indent=2, default=str)}") # Use default=str for non-serializable types
                except Exception as json_err:
                    logger.info(f"Sending {len(messages)} messages to LLM (iteration {current_iteration}) - JSON dump failed: {json_err}. Raw list snippet: {str(messages)[:1000]}...") # Log snippet on failure

                # --- LLM Call ---
                llm_output: LLMOutput = await self.llm_client.generate_response(
                    messages=messages,
                    tools=all_tools,
                    # Allow tools on all iterations except the last forced one?
                    # Or force 'none' if the previous call didn't request tools?
                    # Let's allow 'auto' for now, unless we hit max iterations.
                    tool_choice="auto" if all_tools and current_iteration < max_iterations else "none",
                )

                # Store content and reasoning from the latest call
                # Content will be overwritten in the next iteration if there are tool calls,
                # so only the final iteration's content persists.
                final_content = llm_output.content.strip() if llm_output.content else None
                final_reasoning_info = llm_output.reasoning_info
                if final_content:
                    logger.debug(f"LLM provided text content in iteration {current_iteration}: {final_content[:100]}...")
                else:
                    logger.debug(f"LLM provided no text content in iteration {current_iteration}.")

                tool_calls = llm_output.tool_calls

                # --- Loop Condition: Break if no tool calls requested ---
                if not tool_calls:
                    logger.info("LLM response received with no further tool calls.")
                    # Final content is built after the loop
                    break # Exit the loop

                # --- Handle Tool Calls ---
                logger.info(f"LLM requested {len(tool_calls)} tool call(s) in iteration {current_iteration}.")

                # Append the assistant's response message (containing the tool calls)
                assistant_message_with_calls = {
                    "role": "assistant",
                    "content": llm_output.content, # Include any text content LLM provided along with calls
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_message_with_calls)

                # --- Execute Tool Calls using ToolsProvider ---
                tool_response_messages = []
                # Create execution context (can be reused if dependencies don't change)
                tool_execution_context = ToolExecutionContext(
                    chat_id=chat_id,
                    db_context=db_context,
                    calendar_config=self.calendar_config,
                    application=application,
                    timezone_str=self.timezone_str,
                    request_confirmation_callback=request_confirmation_callback,
                )

                for tool_call_dict in tool_calls:
                    call_id = tool_call_dict.get("id")
                    function_info = tool_call_dict.get("function", {})
                    function_name = function_info.get("name")
                    function_args_str = function_info.get("arguments", "{}")

                    if not call_id or not function_name:
                        logger.error(f"Skipping invalid tool call dict in iteration {current_iteration}: {tool_call_dict}")
                        tool_response_messages.append({"tool_call_id": call_id or f"missing_id_{uuid.uuid4()}", "role": "tool", "name": function_name or "unknown_function", "content": "Error: Invalid tool call structure."})
                        continue

                    try: arguments = json.loads(function_args_str)
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse arguments for tool call {function_name} (iteration {current_iteration}): {function_args_str}")
                        arguments = {"error": "Failed to parse arguments"}
                        tool_response_content = f"Error: Invalid arguments format for {function_name}."
                    else:
                        try:
                            tool_response_content = await self.tools_provider.execute_tool(
                                name=function_name,
                                arguments=arguments,
                                context=tool_execution_context,
                            )
                        except ToolNotFoundError as tnfe:
                            logger.error(f"Tool execution failed (iteration {current_iteration}): {tnfe}")
                            tool_response_content = f"Error: {tnfe}"
                        except Exception as exec_err:
                            logger.error(f"Unexpected error executing tool {function_name} (iteration {current_iteration}): {exec_err}", exc_info=True)
                            tool_response_content = f"Error: Unexpected error executing {function_name}."

                    # Store details for history across all iterations
                    executed_tool_info.append({"tool_call_id": call_id, "function_name": function_name, "arguments": arguments, "response_content": tool_response_content})
                    # Append the response message for the LLM for the *next* iteration
                    tool_response_messages.append({"tool_call_id": call_id, "role": "tool", "name": function_name, "content": tool_response_content})

                # Append all tool response messages for this iteration
                messages.extend(tool_response_messages)

                # Increment iteration counter
                current_iteration += 1
                # --- Loop continues to next LLM call ---

            # --- After Loop ---
            if current_iteration > max_iterations:
                logger.warning(f"Reached maximum tool call iterations ({max_iterations}). Returning current state.")
                # If we hit the limit, the last LLM call was forced with tool_choice='none',
                # so final_content should hold its response. We might add a note.
                if final_content:
                    final_content += "\n\n(Note: Reached maximum processing depth.)"
                else:
                    final_content = "(Note: Reached maximum processing depth.)"

            # final_content now holds the content from the *last* LLM call
            if not final_content:
                 logger.warning("Final LLM response content was empty.")
                 # Optionally set a fallback message, or leave as None
                 # final_content = "Processing complete."

            # Return the final content, accumulated tool info, and reasoning from the last LLM call
            return final_content, executed_tool_info if executed_tool_info else None, final_reasoning_info

        except Exception as e:
            logger.error(
                f"Error during LLM interaction or tool handling loop in ProcessingService: {e}",
                exc_info=True,
            )
            # Ensure tuple is returned even on error
            return None, None, None
    def _format_history_for_llm(
        self, history_messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Formats message history retrieved from the database into the list structure
        expected by the LLM, handling assistant tool calls correctly.

        Args:
            history_messages: List of message dictionaries from storage.get_recent_history.

        Returns:
            A list of message dictionaries formatted for the LLM API.
        """
        messages: List[Dict[str, Any]] = []
        # Process history messages, formatting assistant tool calls correctly
        for msg in history_messages:
            # Use .get for safer access to potentially missing keys
            role = msg.get("role")
            content = msg.get("content") or '' # Content can be None for assistant messages with tool calls
            tool_calls_info = msg.get("tool_calls_info_raw") # Raw tool calls from LLM output (list of dicts)
            tool_call_id = msg.get("tool_call_id") # tool_call_id for role 'tool' messages
            # reasoning_info = msg.get("reasoning_info") # Reasoning info not needed for LLM history format
            # error_traceback = msg.get("error_traceback") # Error info not needed for LLM history format

            if role == "assistant":
                # Check if there's actual tool call data (not None, not empty list)
                if tool_calls_info and isinstance(tool_calls_info, list):
                    # --- Format assistant message WITH tool calls ---
                    # The tool_calls_info stored in DB is expected to be the list of dicts
                    # originally returned by the LLM, including the 'id'.
                    reformatted_tool_calls = []
                    for raw_call in tool_calls_info:
                        if isinstance(raw_call, dict):
                            # Extract the parts needed for the LLM format
                            # Ensure the ID from the LLM ('id') is used.
                            call_id = raw_call.get("id") # Use 'id' from the original LLM response
                            function_info = raw_call.get("function")
                            if call_id and isinstance(function_info, dict):
                                # Append in the standard OpenAI/LiteLLM format
                                reformatted_tool_calls.append(
                                    {
                                        "id": call_id,
                                        "type": "function", # Assuming only function calls for now
                                        "function": function_info, # Pass the whole function dict (name, arguments str)
                                    }
                                )
                            else:
                                logger.warning(f"Skipping malformed raw tool call info in history: {raw_call}")
                        else:
                            logger.warning(f"Skipping non-dict item in raw_tool_calls_info: {raw_call}")

                    if reformatted_tool_calls:
                        # Content might be None or empty string for assistant messages that only contain tool calls
                        messages.append(
                            {"role": "assistant", "content": content, "tool_calls": reformatted_tool_calls}
                        )
                    else: # If no valid tool calls found in the raw data, treat as regular message
                        messages.append({"role": "assistant", "content": content})
                else:
                    # --- This block handles assistant messages WITHOUT tool calls ---
                    messages.append({"role": "assistant", "content": content})
            elif role == "tool":
                # --- Format tool response messages ---
                if tool_call_id: # Only include if tool_call_id is present (retrieved from DB)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": content, # Content is the tool's response string
                        }
                    )
                else:
                    # Log a warning if a tool message is found without an ID (indicates logging issue)
                    logger.warning(f"Found 'tool' role message in history without a tool_call_id: {msg}")
                    # Skip adding malformed tool message to history to avoid LLM errors
            elif role != "error": # Don't include previous error messages in history sent to LLM
                # Append other non-error messages directly
                messages.append({"role": role, "content": content})

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages

    async def generate_llm_response_for_chat(
        self,
        db_context: DatabaseContext,  # Added db_context
        application: Application,
        chat_id: int,
        trigger_content_parts: List[Dict[str, Any]],
        user_name: str,
        # Update callback signature: It now expects (prompt_text, tool_name, tool_args)
        request_confirmation_callback: Optional[
            Callable[[str, str, Dict[str, Any]], Awaitable[bool]]
        ] = None,
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

        # Format the raw history using the new helper method
        messages = self._format_history_for_llm(history_messages)

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
                # Pass timezone string to fetch_upcoming_events
                upcoming_events = await calendar_integration.fetch_upcoming_events(
                    calendar_config=self.calendar_config, # Use self.calendar_config
                    timezone_str=self.timezone_str # Pass timezone
                )
                # Pass timezone string to format_events_for_prompt
                today_events_str, future_events_str = (
                    calendar_integration.format_events_for_prompt(
                        events=upcoming_events,
                        prompts=self.prompts, # Use self.prompts
                        timezone_str=self.timezone_str # Pass timezone
                    )
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
            server_url=self.server_url, # Add server URL
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
            # Capture traceback on error
            import traceback
            error_traceback = traceback.format_exc()
            # Return None for content/tools/reasoning, but include the traceback
            return None, None, None, error_traceback # Return traceback here
