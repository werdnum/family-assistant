import json
import logging
import traceback  # Added for error traceback
import uuid  # Added for unique task IDs
from collections.abc import Awaitable, Callable  # Added Union, Awaitable
from datetime import (  # Added timezone
    datetime,
    timedelta,  # Added
    timezone,
)
from typing import (
    Any,
)

import pytz  # Added

# Import Application type hint
from telegram.ext import Application

# Import storage and calendar integration for context building
from family_assistant import (
    storage,
)  # calendar_integration import removed as context is handled by provider

# --- NEW: Import ContextProvider ---
from .context_providers import ContextProvider

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext

# Import ToolsProvider interface and context
from .tools import ToolExecutionContext, ToolNotFoundError, ToolsProvider

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
        prompts: dict[str, str],
        calendar_config: dict[str, Any],
        context_providers: list[
            ContextProvider
        ],  # NEW: List of context providers (corrected to be single)
        timezone_str: str,
        max_history_messages: int,
        server_url: str | None,  # Added server_url
        history_max_age_hours: int,  # Recommended value is now 1
    ):
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            tools_provider: An object implementing the ToolsProvider protocol.
            prompts: Dictionary containing loaded prompts.
            calendar_config: Dictionary containing calendar configuration.
            context_providers: A list of initialized context provider objects. # Corrected docstring
            timezone_str: The configured timezone string (e.g., "Europe/London").
            max_history_messages: Max number of history messages to fetch.
            server_url: The base URL of the web server.
            history_max_age_hours: Max age of history messages to fetch (in hours). Recommended: 1.
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider
        self.prompts = prompts
        self.calendar_config = calendar_config  # Still needed for ToolExecutionContext
        self.context_providers = context_providers
        self.timezone_str = timezone_str
        self.max_history_messages = max_history_messages
        self.server_url = (
            server_url or "http://localhost:8000"
        )  # Default if not provided
        self.history_max_age_hours = history_max_age_hours
        # Store the confirmation callback function if provided at init? No, get from context.

    async def _aggregate_context_from_providers(self) -> str:
        """Gathers context fragments from all registered providers."""
        all_fragments: list[str] = []
        for provider in self.context_providers:
            try:
                fragments = await provider.get_context_fragments()
                if fragments:  # Only add if provider returned something
                    all_fragments.extend(fragments)
            except Exception as e:
                logger.error(
                    f"Error getting context from provider '{provider.name}': {e}",
                    exc_info=True,
                )
        # Join all non-empty fragments, separated by double newlines for clarity
        return "\n\n".join(filter(None, all_fragments)).strip()

    # Removed _execute_function_call method (if it was previously here)

    async def process_message(
        self,
        db_context: DatabaseContext,  # Added db_context
        messages: list[dict[str, Any]],
        # --- Updated Signature ---
        interface_type: str,
        conversation_id: str,
        application: Application,
        # Update callback signature: It now expects (prompt_text, tool_name, tool_args)
        request_confirmation_callback: (
            Callable[[str, str, dict[str, Any]], Awaitable[bool]] | None
        ) = None,  # Removed comma
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """
        Sends the conversation history to the LLM via the injected client,
        handles potential tool calls using the injected tools provider,
        and returns the list of all messages generated during the turn,
        along with the reasoning info from the final LLM call.

        Args:
            db_context: The database context.
            messages: A list of message dictionaries for the LLM.
            # --- Updated args based on refactoring plan ---
            interface_type: Identifier for the interaction interface (e.g., 'telegram').
            conversation_id: Identifier for the conversation (e.g., chat ID string).
            application: The Telegram Application instance for context.
            request_confirmation_callback: Function to request user confirmation for tools.

        Returns:
            A tuple containing:
            - A list of all message dictionaries generated during this turn
              (assistant requests, tool responses, final answer).
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
        """
        final_content: str | None = None  # Store final text response from LLM
        max_iterations = 5  # Safety limit for tool call loops
        current_iteration = 1

        try:
            # --- Get Tool Definitions ---
            # List to store all messages generated *within this turn*
            # This list will be returned by the function
            turn_messages: list[dict[str, Any]] = []
            all_tools = await self.tools_provider.get_tool_definitions()
            if all_tools:
                logger.info(f"Providing {len(all_tools)} tools to LLM.")
            else:
                logger.info("No tools available from provider.")

            # --- Tool Call Loop ---
            while current_iteration <= max_iterations:
                logger.debug(
                    f"Starting LLM interaction loop iteration {current_iteration}/{max_iterations}"
                )

                # --- Log messages being sent to LLM at INFO level ---
                try:
                    logger.info(
                        f"Sending {len(messages)} messages to LLM (iteration {current_iteration}):\n{json.dumps(messages, indent=2, default=str)}"
                    )  # Use default=str for non-serializable types
                except Exception as json_err:
                    logger.info(
                        f"Sending {len(messages)} messages to LLM (iteration {current_iteration}) - JSON dump failed: {json_err}. Raw list snippet: {str(messages)[:1000]}..."
                    )  # Log snippet on failure

                # --- LLM Call ---
                llm_output: LLMOutput = await self.llm_client.generate_response(
                    messages=messages,
                    tools=all_tools,
                    # Allow tools on all iterations except the last forced one?
                    # Or force 'none' if the previous call didn't request tools?
                    # Let's allow 'auto' for now, unless we hit max iterations.
                    tool_choice=(
                        "auto"
                        if all_tools and current_iteration < max_iterations
                        else "none"
                    ),
                )

                # Store content and reasoning from the latest call
                # Content will be overwritten in the next iteration if there are tool calls,
                # so only the final iteration's content persists.
                final_content = (
                    llm_output.content.strip() if llm_output.content else None
                )
                final_reasoning_info = (
                    llm_output.reasoning_info
                )  # This will hold the reasoning of the *last* LLM call
                if final_content:
                    logger.debug(
                        f"LLM provided text content in iteration {current_iteration}: {final_content[:100]}..."
                    )
                else:
                    logger.debug(
                        f"LLM provided no text content in iteration {current_iteration}."
                    )

                # --- Add Assistant Message to Turn History ---
                # This includes the LLM's text response AND any tool calls it requested
                # This message will be saved to the DB by the caller.
                tool_calls = llm_output.tool_calls
                assistant_message_for_turn = {
                    # Note: turn_id, interface_type, conversation_id, timestamp, thread_root_id added by caller
                    "role": "assistant",
                    "content": final_content,  # May be None if only tool calls
                    "tool_calls": tool_calls,  # LLM's requested calls (OpenAI format)
                    "reasoning_info": (
                        final_reasoning_info
                    ),  # Include reasoning for this step
                    "tool_call_id": None,  # Not applicable for assistant role
                    "error_traceback": None,  # Not applicable for assistant role
                }
                turn_messages.append(assistant_message_for_turn)

                # --- Add Assistant Message to Context for *Next* LLM Call ---
                # Format for the LLM API (content + tool_calls list)
                llm_context_assistant_message = {
                    "role": "assistant",
                    "content": final_content,
                    "tool_calls": tool_calls,
                }
                # If content is None, OpenAI API might ignore it or require empty string depending on version/model.
                # LiteLLM generally handles None content correctly for tool_calls messages.
                messages.append(llm_context_assistant_message)

                # --- Loop Condition: Break if no tool calls requested ---
                if not tool_calls:
                    logger.info("LLM response received with no further tool calls.")
                    break  # Exit the loop
                # --- Execute Tool Calls and Prepare Responses ---
                # --- Execute Tool Calls and Prepare Responses ---
                tool_response_messages_for_llm = []  # For next LLM call context
                for tool_call_dict in tool_calls:
                    call_id = tool_call_dict.get("id")
                    function_info = tool_call_dict.get("function", {})
                    function_name = function_info.get("name")
                    function_args_str = function_info.get("arguments", "{}")

                    if not call_id or not function_name:
                        logger.error(
                            f"Skipping invalid tool call dict in iteration {current_iteration}: {tool_call_dict}"
                        )
                        # Define the error message content
                        error_content = "Error: Invalid tool call structure."
                        error_traceback = (
                            "Invalid tool call structure received from LLM."
                        )
                        safe_call_id = call_id or f"missing_id_{uuid.uuid4()}"
                        safe_function_name = function_name or "unknown_function"

                        # Create the error message dictionary for the turn history
                        tool_response_message_for_turn = (
                            {  # Define the variable before use
                                "role": "tool",
                                "tool_call_id": safe_call_id,
                                "content": error_content,
                                "error_traceback": error_traceback,
                            }
                        )
                        turn_messages.append(tool_response_message_for_turn)
                        # Create the error message for the *next* LLM call context
                        llm_context_error_message = {
                            "tool_call_id": safe_call_id,
                            "role": "tool",
                            "name": safe_function_name,  # Include name for consistency
                            "content": error_content,
                        }
                        # Add this error response to the list for the *next* LLM context
                        tool_response_messages_for_llm.append(llm_context_error_message)
                        continue

                    try:
                        arguments = json.loads(function_args_str)
                    except json.JSONDecodeError:
                        logger.error(
                            f"Failed to parse arguments for tool call {function_name} (iteration {current_iteration}): {function_args_str}"
                        )
                        arguments = {"error": "Failed to parse arguments"}
                        tool_response_content = (
                            f"Error: Invalid arguments format for {function_name}."
                        )
                        # Create the 'tool' response message for the turn history
                        tool_response_message_for_turn = {
                            "role": "tool",
                            "tool_call_id": call_id,  # Use the valid call_id
                            "content": tool_response_content,
                            "error_traceback": f"JSONDecodeError: {function_args_str}",
                            # timestamp etc. added by caller
                        }
                        turn_messages.append(tool_response_message_for_turn)
                        # Create the 'tool' response message for the next LLM context
                        tool_response_messages_for_llm.append(  # Corrected variable name
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": tool_response_content,
                                # name is not typically included for tool responses to LLM
                            }
                        )
                        # Continue to the next tool call in the list
                        continue
                        # Tool execution logic happens after this try-except block for parsing

                    # --- Execute the tool call if parsing succeeded ---
                    logger.info(
                        f"Executing tool '{function_name}' with args: {arguments} (call_id: {call_id}, iteration: {current_iteration})"
                    )

                    # --- Execute the tool ---
                    # Create ToolExecutionContext for this specific call
                    tool_execution_context = ToolExecutionContext(
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        db_context=db_context,
                        calendar_config=self.calendar_config,
                        application=application,
                        timezone_str=self.timezone_str,
                        request_confirmation_callback=request_confirmation_callback,
                        processing_service=self,  # Pass self
                    )

                    # Initialize tool response content and error traceback for this call
                    tool_response_content = None
                    tool_error_traceback = None

                    try:
                        tool_response_content = await self.tools_provider.execute_tool(
                            name=function_name,
                            arguments=arguments,
                            context=tool_execution_context,
                        )
                        logger.debug(
                            f"Tool '{function_name}' executed successfully, result: {str(tool_response_content)[:200]}..."
                        )
                    except ToolNotFoundError as tnfe:
                        logger.error(
                            f"Tool execution failed (iteration {current_iteration}): {tnfe}"
                        )
                        tool_response_content = f"Error: {tnfe}"
                        tool_error_traceback = str(tnfe)
                    except Exception as exec_err:
                        logger.error(
                            f"Unexpected error executing tool {function_name} (iteration {current_iteration}): {exec_err}",
                            exc_info=True,
                        )
                        tool_response_content = (
                            f"Error: Unexpected error executing {function_name}."
                        )
                        tool_error_traceback = (
                            traceback.format_exc()
                        )  # Capture full traceback for unexpected errors

                    # --- Add Tool Response to Turn History ---
                    # This includes the result or error message.
                    tool_response_message_for_turn = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tool_response_content,  # Result or error message
                        "error_traceback": tool_error_traceback,  # Null if successful
                        # Other fields added by caller
                    }
                    turn_messages.append(tool_response_message_for_turn)

                    # --- Add Tool Response to Context for *Next* LLM Call ---
                    # Format for the LLM API (needs tool_call_id and content)
                    tool_response_messages_for_llm.append(  # Corrected variable name
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": (
                                tool_response_content
                            ),  # Send result/error back to LLM
                        }
                    )
                    # --- End of Tool Call Processing Loop ---

                # --- After processing all tool calls for this iteration ---
                # Append all tool response messages gathered in tool_response_messages_for_llm
                # to the main messages list for the *next* LLM iteration.
                messages.extend(tool_response_messages_for_llm)

                # Increment iteration counter
                current_iteration += 1
                # --- Loop continues to next LLM call ---

            # --- After Loop ---
            if current_iteration > max_iterations:
                logger.warning(
                    f"Reached maximum tool call iterations ({max_iterations}). Returning current state."
                )
                # The last message in turn_messages should be the final assistant response
                # (which was forced with tool_choice='none'). We can optionally add a note to its content.
                if turn_messages and turn_messages[-1]["role"] == "assistant":
                    note = "\n\n(Note: Reached maximum processing depth.)"
                    if turn_messages[-1]["content"]:
                        turn_messages[-1]["content"] += note
                    else:
                        turn_messages[-1]["content"] = note.strip()

            # Check if the *last* message generated was an assistant message with no content
            # (This might happen if the final LLM call produced nothing, e.g., after tool errors)
            if (
                turn_messages
                and turn_messages[-1]["role"] == "assistant"
                and not turn_messages[-1].get("content")
            ):
                logger.warning("Final LLM response content was empty.")
                # Optionally set a fallback message, or leave as None
                # turn_messages[-1]["content"] = "Processing complete."

            # Return the complete list of messages generated in this turn, and the reasoning from the final LLM call
            return turn_messages, final_reasoning_info

        except Exception as e:
            logger.error(
                f"Error during LLM interaction or tool handling loop in ProcessingService: {e}",
                exc_info=True,
            )
            # Ensure tuple is returned even on error
            return [], None  # Return empty list, no reasoning info

    def _format_history_for_llm(
        self, history_messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Formats message history retrieved from the database into the list structure
        expected by the LLM, handling assistant tool calls correctly.

        Args:
            history_messages: List of message dictionaries from storage.get_recent_history.

        Returns:
            A list of message dictionaries formatted for the LLM API.
        """
        messages: list[dict[str, Any]] = []
        # Process history messages, formatting assistant tool calls correctly
        for msg in history_messages:
            # Use .get for safer access to potentially missing keys
            role = msg.get("role")
            content = msg.get("content")  # Keep as None if null in DB
            tool_calls = msg.get("tool_calls")  # Get tool calls if present
            tool_call_id = msg.get(
                "tool_call_id"
            )  # tool_call_id for role 'tool' messages
            # reasoning_info = msg.get("reasoning_info") # Reasoning info not needed for LLM history format
            # error_traceback = msg.get("error_traceback") # Error info not needed for LLM history format

            if role == "assistant":
                # Format assistant message, including content and tool_calls if they exist
                assistant_msg_for_llm = {"role": "assistant"}
                if content:
                    assistant_msg_for_llm["content"] = content
                # Check if tool_calls exists and is a non-empty list/dict
                if tool_calls:
                    assistant_msg_for_llm["tool_calls"] = tool_calls
                # Only add the message if it has content or tool calls
                if content or tool_calls:
                    messages.append(assistant_msg_for_llm)
            elif role == "tool":
                # --- Format tool response messages ---
                if (
                    tool_call_id
                ):  # Only include if tool_call_id is present (retrieved from DB)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": (
                                tool_call_id
                            ),  # The ID linking to the assistant request
                            "content": (
                                content or ""
                            ),  # Ensure content is a string, default empty
                        }
                    )
                else:
                    # Log a warning if a tool message is found without an ID (indicates logging issue)
                    logger.warning(
                        f"Found 'tool' role message in history without a tool_call_id: {msg}"
                    )
                    # Skip adding malformed tool message to history to avoid LLM errors
            elif (
                role != "error"
            ):  # Don't include previous error messages in history sent to LLM
                # Append other non-error messages directly
                messages.append(
                    {"role": role, "content": content or ""}
                )  # Ensure content is string

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages

    async def generate_llm_response_for_chat(  # Marked line 404
        self,
        db_context: DatabaseContext,  # Added db_context
        application: Application,
        # --- Refactored Parameters ---
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[dict[str, Any]],
        trigger_interface_message_id: str | None,  # Added trigger message ID
        user_name: str,
        turn_id: str | None = None,  # Made turn_id optional, moved after non-defaults
        replied_to_interface_id: str | None = None,  # Added for reply context
        # Update callback signature: It now expects (prompt_text, tool_name, tool_args)
        request_confirmation_callback: (
            Callable[[str, str, dict[str, Any]], Awaitable[bool]] | None
        ) = None,
    ):
        """Prepares context, message history, calls the LLM processing logic,
        and returns the response, tool info, reasoning info, and any processing error traceback.

        Args:
            db_context: The database context to use for storage operations.
            application: The Telegram application instance.
            interface_type: Identifier for the interaction interface.
            conversation_id: Identifier for the conversation.
            trigger_content_parts: List of content parts for the triggering message.
            turn_id: The pre-generated ID for this entire interaction turn.
            trigger_interface_message_id: The interface-specific ID of the triggering message.
            user_name: The user name to format into the system prompt.
            replied_to_interface_id: Optional interface-specific ID of the message being replied to.
            request_confirmation_callback: Optional callback for tool confirmations.

        Returns:
            A tuple: (List of generated turn messages, Final reasoning info dict or None, Error traceback string or None).
        """
        # If turn_id is not provided, generate one.
        if turn_id is None:
            turn_id = str(uuid.uuid4())
            logger.info(f"turn_id not provided, generated new one: {turn_id}")
        logger.debug(
            f"generate_llm_response_for_chat called with max_history_messages={self.max_history_messages}, history_max_age_hours={self.history_max_age_hours}"
        )
        try:
            raw_history_messages = (
                await storage.get_recent_history(  # Use storage directly with context
                    db_context=db_context,  # Pass context
                    interface_type=interface_type,  # Pass interface_type
                    conversation_id=conversation_id,  # Pass conversation_id
                    limit=self.max_history_messages,  # Use self attribute
                    max_age=timedelta(
                        hours=self.history_max_age_hours
                    ),  # Use self attribute
                )
            )
        except Exception as hist_err:
            logger.error(
                f"Failed to get message history for {interface_type}:{conversation_id}: {hist_err}",
                exc_info=True,
            )
            raw_history_messages = []  # Continue with empty history on error
        logger.debug(f"Raw history messages fetched ({len(raw_history_messages)}):")
        for i, msg in enumerate(raw_history_messages):
            logger.debug(
                f"  RawHist[{i}]: internal_id={msg.get('internal_id')}, role={msg.get('role')}, content_snippet='{str(msg.get('content'))[:50]}...', ts={msg.get('timestamp')}"
            )

        # --- Filter out the triggering message from the fetched history ---
        filtered_history_messages = []
        if trigger_interface_message_id:
            for msg in raw_history_messages:
                if msg.get("interface_message_id") != trigger_interface_message_id:
                    filtered_history_messages.append(msg)
            logger.debug(
                f"Filtered history: {len(raw_history_messages)} -> {len(filtered_history_messages)} messages after removing trigger ID {trigger_interface_message_id}"
            )
        else:
            filtered_history_messages = (
                raw_history_messages  # No trigger ID to filter by
            )
        logger.debug(f"Filtered history messages ({len(filtered_history_messages)}):")
        for i, msg in enumerate(filtered_history_messages):
            logger.debug(
                f"  FiltHist[{i}]: internal_id={msg.get('internal_id')}, role={msg.get('role')}, content_snippet='{str(msg.get('content'))[:50]}...', ts={msg.get('timestamp')}"
            )

        # Format the raw history using the new helper method
        initial_messages_for_llm = self._format_history_for_llm(
            filtered_history_messages
        )
        logger.debug(
            f"Initial messages for LLM after formatting ({len(initial_messages_for_llm)}): {json.dumps(initial_messages_for_llm, default=str)}"
        )

        # --- Handle Reply Thread Context ---
        thread_root_id_for_saving: int | None = (
            None  # Store the root ID for saving later
        )
        if replied_to_interface_id:
            try:
                replied_to_db_msg = await storage.get_message_by_interface_id(
                    db_context=db_context,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=replied_to_interface_id,
                )
                if replied_to_db_msg:
                    # Determine thread root ID for saving
                    thread_root_id_for_saving = replied_to_db_msg.get(
                        "thread_root_id"
                    ) or replied_to_db_msg.get("internal_id")
                    logger.info(
                        f"Determined thread_root_id {thread_root_id_for_saving} from replied-to message {replied_to_interface_id}"
                    )

                    # Fetch the full thread history if a root ID exists
                    if thread_root_id_for_saving:
                        logger.info(
                            f"Fetching full thread history for root ID {thread_root_id_for_saving}"
                        )
                        full_thread_messages_db = (
                            await storage.get_messages_by_thread_id(
                                db_context=db_context,
                                thread_root_id=thread_root_id_for_saving,
                            )
                        )
                        # Format thread messages and replace the initial limited history
                        formatted_thread_messages = self._format_history_for_llm(
                            full_thread_messages_db
                        )
                        initial_messages_for_llm = formatted_thread_messages
                        logger.info(
                            f"Using {len(initial_messages_for_llm)} messages from full thread history for LLM context."
                        )
                    else:
                        # If the replied-to message had no root (was the first), the initial history fetch is sufficient.
                        logger.info(
                            f"Replied-to message {replied_to_interface_id} is the start of the thread. Using standard history."
                        )

                else:
                    logger.warning(
                        f"Could not find replied-to message {replied_to_interface_id} in DB to determine thread root or fetch full thread."
                    )
                    # Fallback: Use the initially fetched recent history

            except Exception as thread_err:
                logger.error(
                    f"Error handling reply context: {thread_err}", exc_info=True
                )
                # Fallback: Use the initially fetched recent history

        # Use the potentially updated message list
        messages = initial_messages_for_llm

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

        # --- NEW: Aggregate context from providers --- This replaces the direct fetching of calendar and notes context.
        aggregated_other_context_str = ""
        try:
            aggregated_other_context_str = (
                await self._aggregate_context_from_providers()
            )
        except Exception as agg_err:
            logger.error(
                f"Failed to aggregate context from providers: {agg_err}", exc_info=True
            )
            aggregated_other_context_str = (
                "Error retrieving extended context."  # Keep fallback
            )

        # --- System Prompt and final message preparation happens *before* calling self.process_message ---
        # This was misplaced in the provided file structure. It should be done on the 'messages' list
        # that is then passed to self.process_message.
        # The `messages` list at this point contains the history. Now add system prompt and trigger message.

        final_system_prompt = system_prompt_template.format(
            user_name=user_name,
            current_time=current_time_str,
            aggregated_other_context=aggregated_other_context_str,  # Use new aggregated context
            server_url=self.server_url,  # Add server URL
        ).strip()

        if final_system_prompt:
            messages.insert(0, {"role": "system", "content": final_system_prompt})
            logger.debug("Prepended system prompt to LLM messages.")
        else:
            logger.warning("Generated empty system prompt.")

        # --- Add the triggering message content ---
        trigger_content: str | list[dict[str, Any]]
        if (
            isinstance(trigger_content_parts, list)
            and len(trigger_content_parts) == 1
            and isinstance(trigger_content_parts[0], dict)
            and trigger_content_parts[0].get("type") == "text"
            and "text" in trigger_content_parts[0]
        ):
            trigger_content = trigger_content_parts[0]["text"]
        else:
            trigger_content = trigger_content_parts

        trigger_message = {
            "role": "user",
            "content": trigger_content,
        }
        messages.append(trigger_message)

        # --- Now, call the processing logic with the fully prepared messages list ---
        try:
            (
                generated_turn_messages,
                final_reasoning_info,
            ) = await self.process_message(  # Call the other method in this class
                db_context=db_context,  # Pass context
                messages=messages,
                interface_type=interface_type,  # Pass interface_type
                conversation_id=conversation_id,  # Pass conversation_id
                application=application,
                request_confirmation_callback=request_confirmation_callback,
            )
            # Add context info (turn_id, etc.) to each generated message *before* returning # Marked line 641
            timestamp_now = datetime.now(timezone.utc)  # Marked line 642
            for msg_dict in generated_turn_messages:
                msg_dict["turn_id"] = turn_id
                msg_dict["interface_type"] = interface_type
                msg_dict["conversation_id"] = conversation_id
                msg_dict["timestamp"] = (
                    timestamp_now  # Approximate timestamp for the whole turn
                )
                msg_dict["thread_root_id"] = (
                    thread_root_id_for_saving  # Use determined root ID
                )
                # interface_message_id will be None initially for agent messages
                msg_dict["interface_message_id"] = None

            # Return the list of fully populated turn messages, reasoning info, and None for error
            return generated_turn_messages, final_reasoning_info, None
        # Moved exception handling outside the process_message call
        except Exception:  # Marked line 650
            # Return None for content/tools/reasoning, but include the traceback
            error_traceback = traceback.format_exc()
            return (
                [],
                None,
                error_traceback,
            )  # Return empty list, None reasoning, traceback
