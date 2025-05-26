import asyncio  # Add asyncio for Event type hint
import json
import logging
import traceback  # Added for error traceback
import uuid  # Added for unique task IDs
from collections.abc import Awaitable, Callable  # Added Union, Awaitable
from dataclasses import dataclass  # Added
from datetime import (  # Added timezone
    datetime,
    timedelta,  # Added
    timezone,
)
from typing import (
    Any,
)

import pytz  # Added

# Import storage and calendar integration for context building
from family_assistant import (
    storage,
)  # calendar_integration import removed as context is handled by provider

# --- NEW: Import ContextProvider ---
from .context_providers import ContextProvider
from .interfaces import ChatInterface  # Import ChatInterface

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMOutput

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext

# Import ToolsProvider interface and context
from .tools import ToolExecutionContext, ToolNotFoundError, ToolsProvider

logger = logging.getLogger(__name__)


# --- Configuration for ProcessingService ---
@dataclass
class ProcessingServiceConfig:
    """Configuration specific to a ProcessingService instance."""

    prompts: dict[str, str]
    calendar_config: dict[str, Any]
    timezone_str: str
    max_history_messages: int
    history_max_age_hours: int
    tools_config: dict[
        str, Any
    ]  # Added to hold tool configurations like 'confirm_tools'


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
        service_config: ProcessingServiceConfig,  # Updated to use service_config
        context_providers: list[ContextProvider],  # NEW: List of context providers
        server_url: str | None,
        app_config: dict[str, Any],  # Keep app_config for now
    ) -> None:
        """
        Initializes the ProcessingService.

        Args:
            llm_client: An object implementing the LLMInterface protocol.
            tools_provider: An object implementing the ToolsProvider protocol.
            service_config: Configuration specific to this service instance.
            context_providers: A list of initialized context provider objects.
            server_url: The base URL of the web server.
            app_config: The main application configuration dictionary (global settings).
        """
        self.llm_client = llm_client
        self.tools_provider = tools_provider
        self.service_config = service_config  # Store the config object
        self.context_providers = context_providers
        self.server_url = (
            server_url or "http://localhost:8000"
        )  # Default if not provided
        self.app_config = app_config  # Store app_config
        # Store the confirmation callback function if provided at init? No, get from context.

    # --- Expose relevant parts of service_config as properties for convenience ---
    # This maintains current internal access patterns while centralizing config.
    @property
    def prompts(self) -> dict[str, str]:
        return self.service_config.prompts

    @property
    def calendar_config(self) -> dict[str, Any]:
        return self.service_config.calendar_config

    @property
    def timezone_str(self) -> str:
        return self.service_config.timezone_str

    @property
    def max_history_messages(self) -> int:
        return self.service_config.max_history_messages

    @property
    def history_max_age_hours(self) -> int:
        return self.service_config.history_max_age_hours

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
        turn_id: str,  # Added turn_id
        chat_interface: ChatInterface | None,  # Added chat_interface
        new_task_event: asyncio.Event | None,  # Added new_task_event
        # Callback signature updated to match ToolExecutionContext's expectation
        request_confirmation_callback: (
            Callable[
                [str, str, str | None, str, str, dict[str, Any], float],
                Awaitable[bool],  # Changed int to str
            ]
            | None
        ) = None,
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
            turn_id: The ID for the current processing turn.
            chat_interface: The interface for sending messages back to the chat.
            new_task_event: Event to notify task worker.
            request_confirmation_callback: Function to request user confirmation for tools.

        Returns:
            A tuple containing:
            - A list of all message dictionaries generated during this turn
              (assistant requests, tool responses, final answer).
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
        """
        final_content: str | None = None  # Store final text response from LLM
        final_reasoning_info: dict[str, Any] | None = (
            None  # Initialize to ensure it's always bound
        )
        max_iterations = 5  # Safety limit for tool call loops
        current_iteration = 1

        try:
            # --- Get Tool Definitions ---
            # List to store all messages generated *within this turn*
            # This list will be returned by the function
            turn_messages: list[dict[str, Any]] = []
            all_tool_definitions = await self.tools_provider.get_tool_definitions()
            tools_for_llm = all_tool_definitions

            if request_confirmation_callback is None:
                # If no confirmation mechanism is available, filter out tools that require it.
                confirmable_tool_names = self.service_config.tools_config.get(
                    "confirm_tools", []
                )
                if confirmable_tool_names:
                    logger.info(
                        f"No confirmation callback available. Filtering out tools requiring confirmation: {confirmable_tool_names}"
                    )
                    tools_for_llm = [
                        tool_def
                        for tool_def in all_tool_definitions
                        if tool_def.get("function", {}).get("name")
                        not in confirmable_tool_names
                    ]
                    logger.info(
                        f"Providing {len(tools_for_llm)} tools to LLM after filtering (originally {len(all_tool_definitions)})."
                    )
                else:
                    logger.info(
                        "No confirmation callback, but no tools are listed in 'confirm_tools'. All tools will be available."
                    )

            if tools_for_llm:
                logger.info(f"Providing {len(tools_for_llm)} tools to LLM.")
            else:
                logger.info(
                    "No tools available to provide to LLM (either none defined or all filtered out)."
                )

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
                    tools=tools_for_llm,  # Use the potentially filtered list
                    # Allow tools on all iterations except the last forced one?
                    # Or force 'none' if the previous call didn't request tools?
                    # Let's allow 'auto' for now, unless we hit max iterations.
                    tool_choice=(
                        "auto"
                        if tools_for_llm
                        and current_iteration < max_iterations  # Check tools_for_llm
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

                # --- Convert ToolCallItem objects to dicts for storage/LLM API ---
                # raw_tool_calls_from_llm is now list[ToolCallItem] | None
                raw_tool_call_items_from_llm = llm_output.tool_calls

                # serialized_tool_calls_for_turn will be list[dict[str, Any]] | None
                # This is what gets stored in DB and sent to next LLM call.
                serialized_tool_calls_for_turn = None
                if raw_tool_call_items_from_llm:
                    serialized_tool_calls_for_turn = []
                    for tool_call_item in raw_tool_call_items_from_llm:
                        # Convert ToolCallItem and its ToolCallFunction to dicts
                        # This uses dataclasses.asdict implicitly if ToolCallItem is a dataclass
                        # or requires manual conversion if it's a NamedTuple.
                        # Since we chose dataclass, asdict is the way.
                        # However, asdict is recursive. We want a specific structure.
                        tool_call_dict = {
                            "id": tool_call_item.id,
                            "type": tool_call_item.type,
                            "function": {
                                "name": tool_call_item.function.name,
                                "arguments": tool_call_item.function.arguments,
                            },
                        }
                        serialized_tool_calls_for_turn.append(tool_call_dict)
                # --- End of ToolCallItem to dict conversion ---

                # --- Add Assistant Message to Turn History ---
                # This includes the LLM's text response AND any tool calls it requested
                # This message will be saved to the DB by the caller.
                assistant_message_for_turn = {
                    # Note: turn_id, interface_type, conversation_id, timestamp, thread_root_id added by caller
                    "role": "assistant",
                    "content": final_content,  # May be None if only tool calls
                    "tool_calls": serialized_tool_calls_for_turn,  # Use serialized version
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
                    "tool_calls": serialized_tool_calls_for_turn,  # Use serialized version
                }
                # If content is None, OpenAI API might ignore it or require empty string depending on version/model.
                # LiteLLM generally handles None content correctly for tool_calls messages.
                messages.append(llm_context_assistant_message)

                # --- Loop Condition: Break if no tool calls requested ---
                if not serialized_tool_calls_for_turn:  # Check the serialized version
                    logger.info(
                        "LLM response received with no further tool calls (after serialization)."
                    )
                    break  # Exit the loop
                # --- Execute Tool Calls and Prepare Responses ---
                # --- Execute Tool Calls and Prepare Responses ---
                tool_response_messages_for_llm = []  # For next LLM call context
                # Iterate over the ToolCallItem objects if they exist
                if raw_tool_call_items_from_llm:
                    for tool_call_item_obj in raw_tool_call_items_from_llm:
                        call_id = tool_call_item_obj.id
                        function_name = tool_call_item_obj.function.name
                        function_args_str = tool_call_item_obj.function.arguments

                        # Note: Validation for presence of id, type, function.name, function.arguments
                        # should ideally be handled during ToolCallItem creation in LiteLLMClient.
                        # Here, we assume they are valid if the object was created.

                        # The following error handling for missing call_id or function_name
                        # might be redundant if LiteLLMClient guarantees valid objects.
                        # However, keeping a light check for robustness.
                        if not call_id or not function_name:
                            logger.error(
                                f"Skipping invalid tool call object in iteration {current_iteration}: id='{call_id}', name='{function_name}'"
                            )
                            # Define the error message content for invalid tool call structure
                            error_content_invalid_struct = (
                                "Error: Invalid tool call structure."
                            )
                            error_traceback_invalid_struct = (
                                "Invalid tool call structure received from LLM."
                            )
                            safe_call_id = (
                                call_id or f"missing_id_{uuid.uuid4()}"
                            )  # Use original call_id if available
                            safe_function_name = function_name or "unknown_function"

                            # Create the error message dictionary for the turn history
                            tool_response_message_for_turn_invalid_struct = {
                                "role": "tool",
                                "tool_call_id": safe_call_id,
                                "content": error_content_invalid_struct,
                                "error_traceback": error_traceback_invalid_struct,
                            }
                            turn_messages.append(
                                tool_response_message_for_turn_invalid_struct
                            )
                            # Create the error message for the *next* LLM call context
                            llm_context_error_message_invalid_struct = {
                                "tool_call_id": safe_call_id,
                                "role": "tool",
                                "name": safe_function_name,  # LLM expects name for tool role message
                                "content": error_content_invalid_struct,
                            }
                            tool_response_messages_for_llm.append(
                                llm_context_error_message_invalid_struct
                            )
                            continue  # Skip to the next tool_call_item_obj

                        # If call_id and function_name are valid, proceed here.

                        # --- Argument Parsing ---
                        try:
                            arguments = json.loads(function_args_str)
                        except json.JSONDecodeError:
                            logger.error(
                                f"Failed to parse arguments for tool call {function_name} (call_id: {call_id}, iteration {current_iteration}): {function_args_str}"
                            )
                            # Prepare error response for this specific tool call
                            error_content_args = (
                                f"Error: Invalid arguments format for {function_name}."
                            )
                            error_traceback_args = (
                                f"JSONDecodeError: {function_args_str}"
                            )

                            turn_messages.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": error_content_args,
                                "error_traceback": error_traceback_args,
                            })
                            tool_response_messages_for_llm.append({
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": error_content_args,
                            })
                            continue  # Skip to the next tool_call_item_obj

                        # --- Tool Execution ---
                        logger.info(
                            f"Executing tool '{function_name}' with args: {arguments} (call_id: {call_id}, iteration: {current_iteration})"
                        )
                        tool_execution_context = ToolExecutionContext(
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            db_context=db_context,
                            chat_interface=chat_interface,
                            new_task_event=new_task_event,  # Pass new_task_event
                            timezone_str=self.timezone_str,
                            request_confirmation_callback=request_confirmation_callback,
                            processing_service=self,
                        )

                        tool_response_content_val = None  # Renamed to avoid conflict
                        tool_error_traceback_val = None  # Renamed to avoid conflict

                        try:
                            tool_response_content_val = (
                                await self.tools_provider.execute_tool(
                                    name=function_name,
                                    arguments=arguments,
                                    context=tool_execution_context,
                                )
                            )
                            logger.debug(
                                f"Tool '{function_name}' (call_id: {call_id}) executed. Result type: {type(tool_response_content_val)}. Result (first 200 chars): {str(tool_response_content_val)[:200]}"
                            )
                        except ToolNotFoundError as tnfe:
                            logger.error(
                                f"Tool execution failed (iteration {current_iteration}): {tnfe}"
                            )
                            tool_response_content_val = f"Error: {tnfe}"
                            tool_error_traceback_val = str(tnfe)
                        except Exception as exec_err:
                            logger.error(
                                f"Unexpected error executing tool {function_name} (iteration {current_iteration}): {exec_err}",
                                exc_info=True,
                            )
                            tool_response_content_val = (
                                f"Error: Unexpected error executing {function_name}."
                            )
                            tool_error_traceback_val = traceback.format_exc()

                        # Add successful or error tool response to turn history
                        turn_messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_response_content_val,
                            "error_traceback": tool_error_traceback_val,
                        })
                        # Add successful or error tool response to LLM context for next call
                        tool_response_messages_for_llm.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_response_content_val,
                        })
                        # End of processing for this specific tool_call_item_obj
                # --- End of loop over raw_tool_call_items_from_llm ---

                # --- After processing all tool calls for this iteration (if any) ---
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": (
                            tool_call_id
                        ),  # The ID linking to the assistant request
                        "content": (
                            content or ""
                        ),  # Ensure content is a string, default empty
                    })
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
                messages.append({
                    "role": role,
                    "content": content or "",
                })  # Ensure content is string

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages

    async def handle_chat_interaction(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[dict[str, Any]],
        trigger_interface_message_id: str | None,
        user_name: str,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        new_task_event: asyncio.Event | None = None,
        request_confirmation_callback: (
            Callable[
                [str, str, str | None, str, str, dict[str, Any], float],
                Awaitable[bool],
            ]
            | None
        ) = None,
    ) -> tuple[str | None, int | None, dict[str, Any] | None, str | None]:
        """
        Handles a complete chat interaction turn.

        This method orchestrates:
        1. Generating a unique turn ID.
        2. Determining conversation thread context.
        3. Saving the initial user trigger message.
        4. Preparing full context for the LLM (history, system prompt, etc.).
        5. Calling the core LLM processing logic (`self.process_message`).
        6. Saving all messages generated during the LLM interaction turn.
        7. Extracting the final textual reply and assistant message ID.

        Args:
            db_context: The database context.
            interface_type: Identifier for the interaction interface (e.g., 'telegram', 'api').
            conversation_id: Identifier for the conversation (e.g., chat ID string).
            trigger_content_parts: List of content parts for the triggering message (e.g., text, image).
            trigger_interface_message_id: The interface-specific ID of the triggering message (if any).
            user_name: The name of the user initiating the interaction.
            replied_to_interface_id: Optional interface-specific ID of a message being replied to.
            chat_interface: Optional interface for sending messages (e.g., for tool confirmations).
            new_task_event: Optional event to notify the task worker of new tasks.
            request_confirmation_callback: Optional callback for requesting user confirmation for tools.

        Returns:
            A tuple containing:
            - final_text_reply (str | None): The textual content of the assistant's final response.
            - final_assistant_message_internal_id (int | None): The internal DB ID of the assistant's final message.
            - final_reasoning_info (dict | None): Reasoning/usage info from the final LLM call.
            - error_traceback (str | None): A string containing the traceback if an error occurred.
        """
        turn_id = str(uuid.uuid4())
        logger.info(
            f"Handling chat interaction for {interface_type}:{conversation_id}, Turn ID: {turn_id}"
        )
        processing_error_traceback: str | None = None
        final_text_reply: str | None = None
        final_assistant_message_internal_id: int | None = None
        final_reasoning_info: dict[str, Any] | None = None

        try:
            # --- 1. Determine Thread Root ID & Save User Trigger Message ---
            thread_root_id_for_turn: int | None = None
            user_message_timestamp = datetime.now(
                timezone.utc
            )  # Timestamp for user message

            if replied_to_interface_id:
                try:
                    replied_to_db_msg = await storage.get_message_by_interface_id(
                        db_context=db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        interface_message_id=replied_to_interface_id,
                    )
                    if replied_to_db_msg:
                        thread_root_id_for_turn = replied_to_db_msg.get(
                            "thread_root_id"
                        ) or replied_to_db_msg.get("internal_id")
                        logger.info(
                            f"Determined thread_root_id {thread_root_id_for_turn} from replied-to message {replied_to_interface_id}"
                        )
                    else:
                        logger.warning(
                            f"Could not find replied-to message {replied_to_interface_id} in DB to determine thread root."
                        )
                except Exception as thread_err:
                    logger.error(
                        f"Error determining thread root ID from reply: {thread_err}",
                        exc_info=True,
                    )

            # Prepare user message content for saving (simplified for now, can be expanded)
            # For simplicity, taking the first text part if available, or a placeholder.
            user_content_for_history = "[User message content]"
            if trigger_content_parts:
                first_text_part = next(
                    (
                        part.get("text")
                        for part in trigger_content_parts
                        if part.get("type") == "text"
                    ),
                    None,
                )
                if first_text_part:
                    user_content_for_history = str(first_text_part)
                elif trigger_content_parts[0].get("type") == "image_url":
                    user_content_for_history = "[Image Attached]"

            saved_user_msg_record = await storage.add_message_to_history(
                db_context=db_context,
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=trigger_interface_message_id,
                turn_id=turn_id,  # User message is part of the turn
                thread_root_id=thread_root_id_for_turn,  # Use determined root ID
                timestamp=user_message_timestamp,
                role="user",
                content=user_content_for_history,  # Store the textual part or placeholder
                tool_calls=None,
                reasoning_info=None,
                error_traceback=None,
                tool_call_id=None,
            )

            if saved_user_msg_record and not thread_root_id_for_turn:
                # If it was the first message in a thread, its own ID is the root.
                thread_root_id_for_turn = saved_user_msg_record.get("internal_id")
                if thread_root_id_for_turn:
                    logger.info(
                        f"Established new thread_root_id: {thread_root_id_for_turn}"
                    )
                    # Update the user message record itself if its thread_root_id was initially None
                    # This is usually handled by add_message_to_history if thread_root_id is passed as None initially
                    # and then set, but double-checking or an explicit update might be needed if that's not the case.
                    # For now, assuming add_message_to_history handles setting its own ID as root if None is passed.

            # --- 2. Prepare LLM Context (History, System Prompt) ---
            try:
                raw_history_messages = await storage.get_recent_history(
                    db_context=db_context,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    limit=self.max_history_messages,
                    max_age=timedelta(hours=self.history_max_age_hours),
                )
            except Exception as hist_err:
                logger.error(
                    f"Failed to get message history for {interface_type}:{conversation_id}: {hist_err}",
                    exc_info=True,
                )
                raw_history_messages = []  # Continue with empty history on error

            logger.debug(f"Raw history messages fetched ({len(raw_history_messages)}).")
            # (Detailed logging of raw history messages can be added here if needed for debugging)

            # Filter out the *current* user trigger message if it somehow got included in history
            filtered_history_messages = []
            if (
                trigger_interface_message_id
            ):  # This ID is of the message *being processed*
                for msg_from_db in raw_history_messages:
                    if (
                        msg_from_db.get("interface_message_id")
                        != trigger_interface_message_id
                    ):
                        filtered_history_messages.append(msg_from_db)
                if len(raw_history_messages) != len(filtered_history_messages):
                    logger.debug(
                        f"Filtered out current trigger message (ID: {trigger_interface_message_id}) from fetched history."
                    )
            else:
                filtered_history_messages = raw_history_messages

            initial_messages_for_llm = self._format_history_for_llm(
                filtered_history_messages
            )
            logger.debug(
                f"Initial messages for LLM after formatting history ({len(initial_messages_for_llm)})."
            )

            # Handle reply thread context
            if replied_to_interface_id and thread_root_id_for_turn:
                try:
                    logger.info(
                        f"Fetching full thread history for root ID {thread_root_id_for_turn} due to reply."
                    )
                    full_thread_messages_db = await storage.get_messages_by_thread_id(
                        db_context=db_context,
                        thread_root_id=thread_root_id_for_turn,
                    )
                    current_trigger_removed_from_thread = []
                    if trigger_interface_message_id:
                        for msg_in_thread in full_thread_messages_db:
                            if (
                                msg_in_thread.get("interface_message_id")
                                != trigger_interface_message_id
                            ):
                                current_trigger_removed_from_thread.append(
                                    msg_in_thread
                                )
                    else:
                        current_trigger_removed_from_thread = full_thread_messages_db

                    initial_messages_for_llm = self._format_history_for_llm(
                        current_trigger_removed_from_thread
                    )
                    logger.info(
                        f"Using {len(initial_messages_for_llm)} messages from full thread history for LLM context."
                    )
                except Exception as thread_fetch_err:
                    logger.error(
                        f"Error fetching full thread history: {thread_fetch_err}",
                        exc_info=True,
                    )
                    # Fallback to using the initially fetched recent history if thread fetch fails

            messages_for_llm = initial_messages_for_llm

            # Prune leading invalid messages
            pruned_count = 0
            while messages_for_llm:
                first_msg = messages_for_llm[0]
                role = first_msg.get("role")
                has_tool_calls = bool(first_msg.get("tool_calls"))
                if role == "tool" or (role == "assistant" and has_tool_calls):
                    messages_for_llm.pop(0)
                    pruned_count += 1
                else:
                    break
            if pruned_count > 0:
                logger.warning(
                    f"Pruned {pruned_count} leading messages from LLM history."
                )

            # Prepare System Prompt
            system_prompt_template = self.prompts.get(
                "system_prompt",
                "You are a helpful assistant. Current time is {current_time}.",
            )
            try:
                local_tz = pytz.timezone(self.timezone_str)
                current_time_str = datetime.now(local_tz).strftime(
                    "%Y-%m-%d %H:%M:%S %Z"
                )
            except Exception as tz_err:
                logger.error(
                    f"Error applying timezone {self.timezone_str}: {tz_err}. Defaulting time format."
                )
                current_time_str = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )

            aggregated_other_context_str = (
                await self._aggregate_context_from_providers()
            )

            final_system_prompt = system_prompt_template.format(
                user_name=user_name,
                current_time=current_time_str,
                aggregated_other_context=aggregated_other_context_str,
                server_url=self.server_url,
            ).strip()

            if final_system_prompt:
                messages_for_llm.insert(
                    0, {"role": "system", "content": final_system_prompt}
                )

            # Add current user trigger message
            llm_user_content: str | list[dict[str, Any]]
            if (
                len(trigger_content_parts) == 1
                and trigger_content_parts[0].get("type") == "text"
            ):
                llm_user_content = trigger_content_parts[0]["text"]
            else:
                llm_user_content = trigger_content_parts

            messages_for_llm.append({"role": "user", "content": llm_user_content})

            # --- 3. Call Core LLM Processing (self.process_message) ---
            (
                generated_turn_messages,
                final_reasoning_info_from_process_msg,
            ) = await self.process_message(
                db_context=db_context,
                messages=messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                turn_id=turn_id,
                chat_interface=chat_interface,
                new_task_event=new_task_event,
                request_confirmation_callback=request_confirmation_callback,
            )
            final_reasoning_info = final_reasoning_info_from_process_msg

            # --- 4. Save Generated Turn Messages & Extract Final Reply ---
            if generated_turn_messages:
                for msg_dict in generated_turn_messages:
                    msg_to_save = msg_dict.copy()
                    msg_to_save["interface_type"] = interface_type
                    msg_to_save["conversation_id"] = conversation_id
                    msg_to_save["turn_id"] = turn_id
                    msg_to_save["thread_root_id"] = thread_root_id_for_turn
                    msg_to_save["timestamp"] = msg_to_save.get(
                        "timestamp", datetime.now(timezone.utc)
                    )
                    msg_to_save.setdefault("interface_message_id", None)

                    saved_turn_msg_record = await storage.add_message_to_history(
                        db_context=db_context, **msg_to_save
                    )

                    if msg_dict.get("role") == "assistant" and msg_dict.get("content"):
                        final_text_reply = str(msg_dict["content"])
                        if saved_turn_msg_record:
                            final_assistant_message_internal_id = (
                                saved_turn_msg_record.get("internal_id")
                            )
            else:
                logger.warning(
                    f"No messages generated by self.process_message for turn {turn_id}."
                )

            return (
                final_text_reply,
                final_assistant_message_internal_id,
                final_reasoning_info,
                None,
            )

        except Exception:
            processing_error_traceback = traceback.format_exc()
            logger.error(
                f"Exception in handle_chat_interaction for {interface_type}:{conversation_id}, turn {turn_id}: {processing_error_traceback}"
            )
            # Attempt to save the error to the user trigger message if possible
            # Ensure saved_user_msg_record is accessible here; it's defined at the start of the try block.
            if (
                "saved_user_msg_record" in locals()
                and saved_user_msg_record
                and saved_user_msg_record.get("internal_id")
            ):
                try:
                    await storage.update_message_error_traceback(
                        db_context=db_context,
                        internal_id=saved_user_msg_record["internal_id"],
                        error_traceback=processing_error_traceback,
                    )
                except Exception as db_err_update:
                    logger.error(
                        f"Failed to update user message with error traceback: {db_err_update}"
                    )

            return None, None, None, processing_error_traceback
