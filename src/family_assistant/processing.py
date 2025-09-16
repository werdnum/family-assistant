import asyncio
import base64
import json
import logging
import re
import traceback  # Added for error traceback
import uuid  # Added for unique task IDs
from collections.abc import AsyncIterator, Awaitable, Callable  # Added Union, Awaitable
from dataclasses import dataclass  # Added
from datetime import (  # Added timezone
    datetime,
    timedelta,  # Added
    timezone,
)
from typing import (
    Any,
)

import aiofiles
import pytz  # Added

from family_assistant.services.attachments import AttachmentService

# Import storage and calendar integration for context building
# storage import removed - using repository pattern via DatabaseContext
# --- NEW: Import ContextProvider ---
from .context_providers import ContextProvider
from .interfaces import ChatInterface  # Import ChatInterface

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMStreamEvent

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext, get_db_context

# Import ToolsProvider interface and context
from .tools import ToolExecutionContext, ToolNotFoundError, ToolsProvider
from .tools.types import ToolResult
from .utils.clock import Clock, SystemClock

logger = logging.getLogger(__name__)


# --- Configuration for ProcessingService ---
@dataclass
class ProcessingServiceConfig:
    """Configuration specific to a ProcessingService instance."""

    prompts: dict[str, str]
    timezone_str: str
    max_history_messages: int
    history_max_age_hours: int
    tools_config: dict[
        str, Any
    ]  # Added to hold tool configurations like 'confirm_tools'
    delegation_security_level: str  # "blocked", "confirm", "unrestricted"
    id: str  # Unique identifier for this service profile
    description: str = ""  # Human-readable description of this profile
    # Type hint for model_parameters should reflect pattern -> params_dict structure
    model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    fallback_model_id: str | None = None  # Added for LLM fallback
    fallback_model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    # Web-specific history settings
    web_max_history_messages: int | None = None  # If None, uses max_history_messages
    web_history_max_age_hours: int | None = None  # If None, uses history_max_age_hours


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
        clock: Clock | None = None,
        attachment_service: AttachmentService | None = None,  # AttachmentService (optional)
        event_sources: dict[str, Any] | None = None,  # Add event sources
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
            clock: Clock instance for time operations.
            attachment_service: AttachmentService instance for handling file attachments.
            event_sources: Dictionary mapping event source IDs to EventSource instances.
        """
        self.llm_client = (
            llm_client  # This client should be instantiated with fallback info
        )
        self.tools_provider = tools_provider
        self.service_config = service_config  # Store the config object
        self.context_providers = context_providers
        self.server_url = (
            server_url or "http://localhost:8000"
        )  # Default if not provided
        self.app_config = app_config  # Store app_config
        self.clock = (
            clock if clock is not None else SystemClock()
        )  # Store the clock instance
        self.attachment_service = attachment_service  # Store the attachment service
        self.processing_services_registry: dict[str, ProcessingService] | None = None
        # Store the confirmation callback function if provided at init? No, get from context.
        self.home_assistant_client: Any | None = None  # Store HA client if available
        self.event_sources = event_sources  # Store event sources for validation

    # The LiteLLMClient passed to __init__ should already be configured
    # with primary and fallback model details by the caller (e.g., main.py)
    # based on the service_config.

    def set_processing_services_registry(
        self, registry: dict[str, "ProcessingService"]
    ) -> None:
        """Sets the registry of all processing services."""
        self.processing_services_registry = registry

    # --- Expose relevant parts of service_config as properties for convenience ---
    # This maintains current internal access patterns while centralizing config.
    @property
    def prompts(self) -> dict[str, str]:
        return self.service_config.prompts

    @property
    def timezone_str(self) -> str:
        return self.service_config.timezone_str

    @property
    def max_history_messages(self) -> int:
        return self.service_config.max_history_messages

    @property
    def history_max_age_hours(self) -> int:
        return self.service_config.history_max_age_hours

    @property
    def web_max_history_messages(self) -> int:
        # Use web-specific setting if available, otherwise fall back to default
        if self.service_config.web_max_history_messages is not None:
            return self.service_config.web_max_history_messages
        return self.service_config.max_history_messages

    @property
    def web_history_max_age_hours(self) -> int:
        # Use web-specific setting if available, otherwise fall back to default
        if self.service_config.web_history_max_age_hours is not None:
            return self.service_config.web_history_max_age_hours
        return self.service_config.history_max_age_hours

    def _get_history_limits_for_interface(
        self, interface_type: str
    ) -> tuple[int, timedelta]:
        """Get history limits based on interface type.

        Args:
            interface_type: The type of interface (e.g., "web", "telegram", "api")

        Returns:
            Tuple of (max_messages, max_age_timedelta)
        """
        if interface_type == "web":
            return self.web_max_history_messages, timedelta(
                hours=self.web_history_max_age_hours
            )
        else:
            return self.max_history_messages, timedelta(
                hours=self.history_max_age_hours
            )

    async def _aggregate_context_from_providers(self) -> str:
        """Gathers context fragments from all registered providers."""
        all_fragments: list[str] = []
        for provider in self.context_providers:
            try:
                fragments_output = await provider.get_context_fragments()

                if isinstance(fragments_output, list):
                    # If it's a list, extend. This handles empty lists correctly (no-op).
                    all_fragments.extend(fragments_output)
                    if not fragments_output:  # Log if the list was empty
                        logger.debug(
                            f"Context provider '{provider.name}' returned an empty list of fragments."
                        )
                elif fragments_output is None:
                    # Log a warning if a provider violates protocol by returning None
                    logger.warning(
                        f"Context provider '{provider.name}' returned None instead of a list. Skipping."
                    )
                else:
                    # Log an error if a provider returns something other than a list or None
                    logger.error(
                        f"Context provider '{provider.name}' returned an unexpected type: {type(fragments_output)}. Expected list[str]. Skipping."
                    )
            except Exception as e:
                # This catches errors from await provider.get_context_fragments() itself
                logger.error(
                    f"Error calling get_context_fragments() for provider '{provider.name}': {e}",
                    exc_info=True,
                )
        # Join all non-empty fragments (i.e., filter out empty strings from individual providers' lists)
        # separated by double newlines for clarity.
        return "\n\n".join(filter(None, all_fragments)).strip()

    # Removed _execute_function_call method (if it was previously here)

    async def process_message(
        self,
        db_context: DatabaseContext,  # Added db_context
        messages: list[dict[str, Any]],
        # --- Updated Signature ---
        interface_type: str,
        conversation_id: str,
        user_name: str,  # Added user_name
        turn_id: str,  # Added turn_id
        chat_interface: ChatInterface | None,  # Added chat_interface
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
        Non-streaming version of process_message that uses the streaming generator internally.

        This method maintains backward compatibility by collecting all streaming events
        and returning the complete list of messages and final reasoning info.

        Args:
            db_context: The database context.
            messages: A list of message dictionaries for the LLM.
            interface_type: Identifier for the interaction interface (e.g., 'telegram').
            conversation_id: Identifier for the conversation (e.g., chat ID string).
            user_name: The name of the user for context.
            turn_id: The ID for the current processing turn.
            chat_interface: The interface for sending messages back to the chat.
            request_confirmation_callback: Function to request user confirmation for tools.

        Returns:
            A tuple containing:
            - A list of all message dictionaries generated during this turn
              (assistant requests, tool responses, final answer).
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
        """
        # Use the streaming generator and collect all messages
        turn_messages: list[dict[str, Any]] = []
        final_reasoning_info: dict[str, Any] | None = None

        async for event, message_dict in self.process_message_stream(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            request_confirmation_callback=request_confirmation_callback,
        ):
            # Collect messages that should be saved
            if message_dict and message_dict.get("role"):
                turn_messages.append(message_dict)

            # Extract reasoning info from done events
            if event.type == "done" and event.metadata and "message" in event.metadata:
                assistant_msg = event.metadata["message"]
                if assistant_msg.get("reasoning_info"):
                    final_reasoning_info = assistant_msg["reasoning_info"]

        return turn_messages, final_reasoning_info

    async def process_message_stream(
        self,
        db_context: DatabaseContext,
        messages: list[dict[str, Any]],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        request_confirmation_callback: (
            Callable[
                [str, str, str | None, str, str, dict[str, Any], float],
                Awaitable[bool],
            ]
            | None
        ) = None,
    ) -> AsyncIterator[tuple[LLMStreamEvent, dict[str, Any]]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message_dict) where:
        - event: The LLMStreamEvent object
        - message_dict: The message dictionary to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        final_content: str | None = None
        final_reasoning_info: dict[str, Any] | None = None
        max_iterations = 5
        current_iteration = 1

        # Get tool definitions
        all_tool_definitions = await self.tools_provider.get_tool_definitions()
        tools_for_llm = all_tool_definitions
        logger.debug(f"Total available tools: {len(all_tool_definitions)}")

        if request_confirmation_callback is None:
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
                logger.debug(
                    f"Tools after filtering out confirmable tools: {len(tools_for_llm)}"
                )

        # Tool call loop
        while current_iteration <= max_iterations:
            logger.debug(
                f"Starting streaming LLM interaction loop iteration {current_iteration}/{max_iterations}"
            )

            # Stream from LLM
            accumulated_content = []
            tool_calls_from_stream = []

            try:
                async for event in self.llm_client.generate_response_stream(
                    messages=messages,
                    tools=tools_for_llm if current_iteration < max_iterations else None,
                    tool_choice=(
                        "auto"
                        if tools_for_llm and current_iteration < max_iterations
                        else "none"
                    ),
                ):
                    # Yield content events as they come
                    if event.type == "content" and event.content:
                        accumulated_content.append(event.content)
                        yield (event, {})  # No message to save yet

                    # Collect tool calls
                    elif event.type == "tool_call" and event.tool_call:
                        tool_calls_from_stream.append(event.tool_call)
                        yield (event, {})  # No message to save yet

                    # Handle done event
                    elif event.type == "done":
                        final_reasoning_info = event.metadata

                    # Handle errors
                    elif event.type == "error":
                        logger.error(f"Stream error: {event.error}")
                        raise RuntimeError(f"LLM streaming error: {event.error}")

            except Exception as e:
                logger.error(f"Error in LLM streaming: {e}", exc_info=True)
                raise

            # Combine accumulated content
            final_content = (
                "".join(accumulated_content) if accumulated_content else None
            )

            # Convert tool calls to serialized format
            serialized_tool_calls = None
            if tool_calls_from_stream:
                serialized_tool_calls = []
                for tool_call_item in tool_calls_from_stream:
                    tool_call_dict = {
                        "id": tool_call_item.id,
                        "type": tool_call_item.type,
                        "function": {
                            "name": tool_call_item.function.name,
                            "arguments": tool_call_item.function.arguments,
                        },
                    }
                    serialized_tool_calls.append(tool_call_dict)

            # Create assistant message
            assistant_message_for_turn = {
                "role": "assistant",
                "content": final_content,
                "tool_calls": serialized_tool_calls,
                "reasoning_info": final_reasoning_info,
                "tool_call_id": None,
                "error_traceback": None,
            }

            # Yield a synthetic "done" event with the complete assistant message
            yield (
                LLMStreamEvent(
                    type="done", metadata={"message": assistant_message_for_turn}
                ),
                assistant_message_for_turn,
            )

            # Add to context for next iteration
            llm_context_assistant_message = {
                "role": "assistant",
                "content": final_content,
                "tool_calls": serialized_tool_calls,
            }
            messages.append(llm_context_assistant_message)

            # Break if no tool calls
            if not serialized_tool_calls:
                logger.info(
                    "LLM streaming response received with no further tool calls."
                )
                break

            # Execute tool calls in parallel
            tool_response_messages_for_llm = []

            # Create tasks for all tool calls
            tool_tasks = [
                asyncio.create_task(
                    self._execute_single_tool(
                        tool_call,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        user_name=user_name,
                        turn_id=turn_id,
                        db_context=db_context,
                        chat_interface=chat_interface,
                        request_confirmation_callback=request_confirmation_callback,
                    )
                )
                for tool_call in tool_calls_from_stream
            ]

            # Process results as they complete
            for completed_task in asyncio.as_completed(tool_tasks):
                try:
                    event, tool_response_message, llm_message = await completed_task

                    # Yield tool result event
                    yield (event, tool_response_message)

                    # Add to messages for LLM
                    tool_response_messages_for_llm.append(llm_message)

                except Exception as e:
                    # This should not happen since we handle exceptions inside execute_single_tool
                    # But adding as extra safety
                    logger.error(
                        f"Unexpected error in parallel tool execution: {e}",
                        exc_info=True,
                    )
                    error_event = LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=f"error_{uuid.uuid4()}",
                        tool_result=f"Unexpected error: {str(e)}",
                        error=traceback.format_exc(),
                    )
                    error_message = {
                        "role": "tool",
                        "tool_call_id": f"error_{uuid.uuid4()}",
                        "content": f"Unexpected error: {str(e)}",
                        "error_traceback": traceback.format_exc(),
                    }
                    yield (error_event, error_message)
                    tool_response_messages_for_llm.append({
                        "tool_call_id": f"error_{uuid.uuid4()}",
                        "role": "tool",
                        "name": "unknown",
                        "content": f"Unexpected error: {str(e)}",
                    })

            # Add tool responses to messages for next iteration
            messages.extend(tool_response_messages_for_llm)
            current_iteration += 1

        # Check if we hit max iterations
        if current_iteration > max_iterations:
            logger.warning(
                f"Reached maximum iterations ({max_iterations}) in streaming tool loop."
            )

    async def _execute_single_tool(
        self,
        tool_call_item_obj: Any,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        db_context: DatabaseContext,
        chat_interface: ChatInterface | None,
        request_confirmation_callback: Callable[
            [str, str, str | None, str, str, dict[str, Any], float],
            Awaitable[bool],
        ]
        | None,
    ) -> tuple[LLMStreamEvent, dict[str, Any], dict[str, Any]]:
        """Execute a single tool call and return the result.

        Args:
            tool_call_item_obj: The tool call object from LLM
            interface_type: Interface type (e.g., 'telegram')
            conversation_id: Conversation identifier
            user_name: User name for context
            turn_id: Current turn identifier
            db_context: Database context
            chat_interface: Chat interface for sending messages
            request_confirmation_callback: Callback for tool confirmation

        Returns:
            Tuple of (event, tool_response_message, llm_message)
        """
        call_id = tool_call_item_obj.id
        function_name = tool_call_item_obj.function.name
        function_args_str = tool_call_item_obj.function.arguments

        # Validate tool call
        if not call_id or not function_name:
            logger.error(f"Invalid tool call: id='{call_id}', name='{function_name}'")
            error_content = "Error: Invalid tool call structure."
            error_traceback = "Invalid tool call structure received from LLM."

            tool_response_message = {
                "role": "tool",
                "tool_call_id": call_id or f"missing_id_{uuid.uuid4()}",
                "content": error_content,
                "error_traceback": error_traceback,
            }

            return (
                LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                tool_response_message,
                {
                    "tool_call_id": call_id or f"missing_id_{uuid.uuid4()}",
                    "role": "tool",
                    "name": function_name or "unknown_function",
                    "content": error_content,
                },
            )

        # Parse arguments
        try:
            arguments = json.loads(function_args_str)
        except json.JSONDecodeError:
            logger.error(
                f"Failed to parse arguments for {function_name}: {function_args_str}"
            )
            error_content = f"Error: Invalid arguments format for {function_name}."
            error_traceback = f"JSONDecodeError: {function_args_str}"

            tool_response_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": error_content,
                "error_traceback": error_traceback,
            }

            return (
                LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                tool_response_message,
                {
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": error_content,
                },
            )

        # Execute tool
        logger.info(f"Executing tool '{function_name}' with args: {arguments}")
        tool_execution_context = ToolExecutionContext(
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            db_context=db_context,
            chat_interface=chat_interface,
            timezone_str=self.timezone_str,
            request_confirmation_callback=request_confirmation_callback,
            processing_service=self,
            clock=self.clock,
            home_assistant_client=self.home_assistant_client,
            event_sources=self.event_sources,
            indexing_source=(
                self.event_sources.get("indexing") if self.event_sources else None
            ),
        )

        try:
            # Execute the tool
            result = await self.tools_provider.execute_tool(
                function_name, arguments, tool_execution_context, call_id
            )
            logger.info(f"Tool '{function_name}' executed successfully.")

            # Handle both string and ToolResult
            if isinstance(result, ToolResult):
                # Extract attachment for provider handling
                tool_response_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result.text,
                    "error_traceback": None,
                }

                # Add attachment metadata for provider to handle
                if result.attachment:
                    tool_response_message["_attachment"] = result.attachment

                    # Store in history metadata
                    tool_response_message["attachments"] = [
                        {
                            "type": "tool_result",
                            "mime_type": result.attachment.mime_type,
                            "description": result.attachment.description,
                        }
                    ]

                content_for_stream = result.text
            else:
                # Backward compatible string handling
                content_for_stream = str(result)
                tool_response_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content_for_stream,
                    "error_traceback": None,
                }

            # Create history message from tool_response_message but remove _attachment
            history_message = tool_response_message.copy()
            history_message.pop("_attachment", None)  # Remove raw attachment data
            history_message["name"] = function_name  # Ensure tool name is included

            return (
                LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=content_for_stream,
                ),
                tool_response_message,
                history_message,
            )

        except ToolNotFoundError:
            logger.error(f"Tool '{function_name}' not found.")
            error_content = f"Error: Tool '{function_name}' not found."
            error_traceback = traceback.format_exc()

            tool_response_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": error_content,
                "error_traceback": error_traceback,
            }

            return (
                LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                tool_response_message,
                {
                    "tool_call_id": call_id,
                    "role": "tool",
                    "name": function_name,
                    "content": error_content,
                },
            )

        except Exception as e:
            logger.error(f"Error executing tool '{function_name}': {e}", exc_info=True)
            error_content = f"Error executing {function_name}: {str(e)}"
            error_traceback = traceback.format_exc()

            tool_response_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": error_content,
                "error_traceback": error_traceback,
            }

            return (
                LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                tool_response_message,
                {
                    "tool_call_id": call_id,
                    "role": "tool",
                    "name": function_name,
                    "content": error_content,
                },
            )

    async def _convert_attachment_urls_to_data_uris(
        self, content_parts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Convert any attachment server URLs in content parts to data URIs.

        This is necessary because external LLM providers cannot access our internal
        server URLs like /api/attachments/...

        Args:
            content_parts: List of content parts that may contain image_url entries

        Returns:
            Modified content parts with server URLs converted to data URIs
        """
        # If no attachment service is available, return parts unchanged
        if not self.attachment_service:
            return content_parts

        converted_parts = []

        # Check if we need to do any conversions
        has_attachment_urls = any(
            part.get("type") == "image_url"
            and part.get("image_url", {}).get("url", "").startswith("/api/attachments/")
            for part in content_parts
        )

        for part in content_parts:
            if part.get("type") == "image_url":
                image_url = part.get("image_url", {}).get("url", "")

                # Check if it's a server URL that needs conversion
                if image_url.startswith("/api/attachments/") and has_attachment_urls:
                    # Extract attachment ID from URL
                    match = re.match(r"/api/attachments/([a-f0-9-]+)", image_url)
                    if match:
                        attachment_id = match.group(1)

                        # Use AttachmentService to get the file path
                        file_path = self.attachment_service.get_attachment_path(
                            attachment_id
                        )

                        if file_path and file_path.exists():
                            try:
                                # Read file asynchronously
                                async with aiofiles.open(file_path, "rb") as f:
                                    file_bytes = await f.read()

                                # Detect MIME type from file extension
                                content_type = self.attachment_service.get_content_type(
                                    file_path
                                )

                                # Convert to base64
                                base64_data = base64.b64encode(file_bytes).decode(
                                    "utf-8"
                                )
                                data_uri = f"data:{content_type};base64,{base64_data}"

                                # Replace with data URI
                                converted_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": data_uri},
                                })
                                logger.info(
                                    f"Converted attachment URL to data URI for attachment {attachment_id} (type: {content_type})"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to convert attachment URL to data URI: {e}"
                                )
                                # Keep original if conversion fails
                                converted_parts.append(part)
                        else:
                            logger.warning(
                                f"Attachment file not found for ID: {attachment_id}"
                            )
                            converted_parts.append(part)
                    else:
                        # Couldn't parse attachment ID, keep original
                        converted_parts.append(part)
                else:
                    # Already a data URI or external URL, keep as-is
                    converted_parts.append(part)
            else:
                # Not an image_url part, keep as-is
                converted_parts.append(part)

        return converted_parts

    async def _format_history_for_llm(
        self, history_messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Formats message history retrieved from the database into the list structure
        expected by the LLM, handling assistant tool calls correctly.

        Args:
            history_messages: List of message dictionaries from db_context.message_history.get_recent.

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
                # Strip text content from messages with tool calls to avoid partial responses
                if tool_calls and content:
                    # If there are tool calls, don't include the text content to avoid partial responses
                    logger.debug(
                        f"Stripped text content from assistant message with tool calls in LLM history. Original content: {content[:100]}..."
                    )
                elif content:
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
            elif role == "error":
                # Include error messages as assistant messages so LLM knows it responded
                error_traceback = msg.get("error_traceback", "")
                error_content = f"I encountered an error: {content}"
                if error_traceback:
                    error_content += f"\n\nError details: {error_traceback}"
                messages.append({
                    "role": "assistant",
                    "content": error_content,
                })
            else:
                # Handle user messages with potential attachments
                if role == "user":
                    user_message_content = content or ""
                    attachments = msg.get("attachments")

                    if attachments and len(attachments) > 0:
                        # Format user message with attachments as content parts
                        content_parts = []

                        # Add text content if present
                        if user_message_content:
                            content_parts.append({
                                "type": "text",
                                "text": user_message_content,
                            })

                        # Add image attachments as image_url content parts
                        for attachment in attachments:
                            if attachment.get("type") == "image" and attachment.get(
                                "content_url"
                            ):
                                content_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": attachment["content_url"]},
                                })

                        # Convert attachment URLs to data URIs if we have content parts with images
                        if content_parts:
                            content_parts = (
                                await self._convert_attachment_urls_to_data_uris(
                                    content_parts
                                )
                            )

                        messages.append({
                            "role": "user",
                            "content": content_parts
                            if content_parts
                            else user_message_content,
                        })
                    else:
                        # User message without attachments
                        messages.append({
                            "role": "user",
                            "content": user_message_content,
                        })
                else:
                    # Append other non-user messages directly
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
        request_confirmation_callback: (
            Callable[
                [str, str, str | None, str, str, dict[str, Any], float],
                Awaitable[bool],
            ]
            | None
        ) = None,
        trigger_attachments: list[dict[str, Any]] | None = None,
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
            user_message_timestamp = self.clock.now()  # Timestamp for user message

            if replied_to_interface_id:
                try:
                    replied_to_db_msg = (
                        await db_context.message_history.get_by_interface_id(
                            interface_type=interface_type,
                            interface_message_id=replied_to_interface_id,
                        )
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

            saved_user_msg_record = await db_context.message_history.add(
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
                attachments=trigger_attachments,
                tool_call_id=None,
                processing_profile_id=self.service_config.id,  # Record profile ID
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
            # Use interface-specific history limits
            history_limit, history_max_age = self._get_history_limits_for_interface(
                interface_type
            )

            try:
                raw_history_messages = await db_context.message_history.get_recent(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    limit=history_limit,
                    max_age=history_max_age,
                    processing_profile_id=self.service_config.id,  # Filter by profile
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

            initial_messages_for_llm = await self._format_history_for_llm(
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
                    full_thread_messages_db = (
                        await db_context.message_history.get_by_thread_id(
                            thread_root_id=thread_root_id_for_turn,
                            processing_profile_id=self.service_config.id,
                        )
                    )  # Filter by profile
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

                    initial_messages_for_llm = await self._format_history_for_llm(
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
                # Use the injected clock's now() method
                current_time_str = (
                    self.clock.now()
                    .astimezone(local_tz)
                    .strftime("%Y-%m-%d %H:%M:%S %Z")
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

            # Prepare arguments for system prompt formatting
            format_args = {
                "user_name": user_name,
                "current_time": current_time_str,
                "aggregated_other_context": aggregated_other_context_str,
                "server_url": self.server_url,
                "profile_id": self.service_config.id,
            }

            class SafePromptFormatter(dict):
                def __missing__(self, key: str) -> str:
                    # This method is called by format_map if a key is not found.
                    logger.warning(
                        f"System prompt template used key '{{{key}}}' which was not found "
                        f"in the provided format arguments: {list(self.keys())}. "
                        f"Substituting with an empty string."
                    )
                    return ""  # Return empty string for missing keys

            # Pre-process template to handle JSON examples and other literal braces
            # Strategy: Find all format placeholders first, then escape everything else
            safe_template = system_prompt_template

            # Find all valid format placeholders (e.g., {key_name})
            placeholder_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"
            placeholders = set(re.findall(placeholder_pattern, safe_template))

            # Escape all braces that aren't part of valid placeholders
            # First, temporarily replace valid placeholders with a unique marker
            temp_template = safe_template
            for i, placeholder in enumerate(placeholders):
                marker = f"__PLACEHOLDER_{i}__"
                temp_template = temp_template.replace(f"{{{placeholder}}}", marker)

            # Now escape all remaining braces
            temp_template = temp_template.replace("{", "{{").replace("}", "}}")

            # Restore the valid placeholders
            for i, placeholder in enumerate(placeholders):
                marker = f"__PLACEHOLDER_{i}__"
                temp_template = temp_template.replace(marker, f"{{{placeholder}}}")

            safe_template = temp_template

            # Use format_map with the custom dictionary to safely format the template
            try:
                final_system_prompt = safe_template.format_map(
                    SafePromptFormatter(format_args)
                ).strip()
            except ValueError as e:
                # If we still get format errors, log them and use the template as-is
                logger.error(
                    f"Failed to format system prompt template: {e}. Using template without substitution."
                )
                final_system_prompt = system_prompt_template.strip()

            if final_system_prompt:
                messages_for_llm.insert(
                    0, {"role": "system", "content": final_system_prompt}
                )

            # Convert attachment URLs to data URIs before sending to LLM
            converted_trigger_parts = await self._convert_attachment_urls_to_data_uris(
                trigger_content_parts
            )

            # Add current user trigger message
            llm_user_content: str | list[dict[str, Any]]
            if (
                len(converted_trigger_parts) == 1
                and converted_trigger_parts[0].get("type") == "text"
            ):
                llm_user_content = converted_trigger_parts[0]["text"]
            else:
                llm_user_content = converted_trigger_parts

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
                user_name=user_name,  # Pass user_name
                turn_id=turn_id,
                chat_interface=chat_interface,
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
                        "timestamp",
                        self.clock.now(),  # Use ProcessingService's clock
                    )
                    msg_to_save.setdefault("interface_message_id", None)
                    # Add processing_profile_id for turn messages
                    msg_to_save["processing_profile_id"] = self.service_config.id

                    saved_turn_msg_record = await db_context.message_history.add(
                        **msg_to_save
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
                    await db_context.message_history.update_error_traceback(
                        internal_id=saved_user_msg_record["internal_id"],
                        error_traceback=processing_error_traceback,
                    )
                except Exception as db_err_update:
                    logger.error(
                        f"Failed to update user message with error traceback: {db_err_update}"
                    )

            # Generate and store an error message in message history so LLM can see it
            error_message = (
                "Sorry, an unexpected error occurred while processing your request."
            )
            error_message_internal_id = None
            try:
                saved_error_msg_record = await db_context.message_history.add(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=None,  # No interface message ID for generated error
                    turn_id=turn_id,  # Use the same turn_id
                    thread_root_id=thread_root_id_for_turn,  # Use the same thread_root_id
                    timestamp=datetime.now(timezone.utc),
                    role="error",
                    content=error_message,
                    error_traceback=processing_error_traceback,
                    processing_profile_id=self.service_config.id,
                )
                if saved_error_msg_record:
                    error_message_internal_id = saved_error_msg_record.get(
                        "internal_id"
                    )
                    logger.info(
                        f"Stored error message in history with internal_id {error_message_internal_id}"
                    )
            except Exception as error_save_err:
                logger.error(
                    f"Failed to save error message to history: {error_save_err}",
                    exc_info=True,
                )

            # Return the error message and its ID so the caller can send it to the user
            return (
                error_message,
                error_message_internal_id,
                None,
                processing_error_traceback,
            )

    async def handle_chat_interaction_stream(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[dict[str, Any]],
        trigger_interface_message_id: str | None,
        user_name: str,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        request_confirmation_callback: (
            Callable[
                [str, str, str | None, str, str, dict[str, Any], float],
                Awaitable[bool],
            ]
            | None
        ) = None,
        trigger_attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """
        Streaming version of handle_chat_interaction.

        Yields LLMStreamEvent objects as the interaction progresses, providing
        real-time updates on text generation, tool calls, and tool results.

        Args:
            Same as handle_chat_interaction

        Yields:
            LLMStreamEvent objects representing different stages of processing
        """
        turn_id = str(uuid.uuid4())
        logger.info(
            f"Starting streaming chat interaction. Turn ID: {turn_id}, "
            f"Interface: {interface_type}, Conversation: {conversation_id}, "
            f"User: {user_name}, Content parts: {len(trigger_content_parts)}"
        )

        # Debug log content parts
        for i, part in enumerate(trigger_content_parts):
            logger.info(
                f"Processing content part {i}: type={part.get('type')}, size={len(str(part))}"
            )

        try:
            # --- 1. Determine Thread Root ID & Save User Trigger Message ---
            thread_root_id_for_turn: int | None = None
            user_message_timestamp = self.clock.now()

            if replied_to_interface_id:
                try:
                    replied_to_db_msg = (
                        await db_context.message_history.get_by_interface_id(
                            interface_type=interface_type,
                            interface_message_id=replied_to_interface_id,
                        )
                    )
                    if replied_to_db_msg:
                        thread_root_id_for_turn = replied_to_db_msg.get(
                            "thread_root_id"
                        ) or replied_to_db_msg.get("internal_id")
                        logger.info(
                            f"Determined thread_root_id {thread_root_id_for_turn} from replied-to message"
                        )
                except Exception as thread_err:
                    logger.error(
                        f"Error determining thread root ID: {thread_err}",
                        exc_info=True,
                    )

            # Prepare user message content
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

            # Save user message in its own transaction to avoid long-running transactions
            async with get_db_context(engine=db_context.engine) as user_msg_db:
                saved_user_msg_record = await user_msg_db.message_history.add(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=trigger_interface_message_id,
                    turn_id=turn_id,
                    thread_root_id=thread_root_id_for_turn,
                    timestamp=user_message_timestamp,
                    role="user",
                    content=user_content_for_history,
                    tool_calls=None,
                    reasoning_info=None,
                    error_traceback=None,
                    tool_call_id=None,
                    processing_profile_id=self.service_config.id,
                    attachments=trigger_attachments,
                )

            if saved_user_msg_record and not thread_root_id_for_turn:
                thread_root_id_for_turn = saved_user_msg_record.get("internal_id")

            # --- 2. Prepare LLM Context ---
            # Use interface-specific history limits
            history_limit, history_max_age = self._get_history_limits_for_interface(
                interface_type
            )

            try:
                raw_history_messages = await db_context.message_history.get_recent(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    limit=history_limit,
                    max_age=history_max_age,
                    processing_profile_id=self.service_config.id,
                )
            except Exception as hist_err:
                logger.error(
                    f"Failed to get message history: {hist_err}", exc_info=True
                )
                raw_history_messages = []

            # Filter out current trigger message
            filtered_history_messages = []
            if trigger_interface_message_id:
                for h_msg in raw_history_messages:
                    if (
                        h_msg.get("interface_message_id")
                        != trigger_interface_message_id
                    ):
                        filtered_history_messages.append(h_msg)
            else:
                filtered_history_messages = raw_history_messages

            initial_messages_for_llm = await self._format_history_for_llm(
                filtered_history_messages
            )

            # Handle reply thread context
            if replied_to_interface_id and thread_root_id_for_turn:
                try:
                    full_thread_messages_db = (
                        await db_context.message_history.get_by_thread_id(
                            thread_root_id=thread_root_id_for_turn,
                            processing_profile_id=self.service_config.id,
                        )
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

                    initial_messages_for_llm = await self._format_history_for_llm(
                        current_trigger_removed_from_thread
                    )
                except Exception as thread_fetch_err:
                    logger.error(f"Error fetching thread history: {thread_fetch_err}")

            messages_for_llm = initial_messages_for_llm

            # Prune leading invalid messages
            while messages_for_llm:
                first_msg = messages_for_llm[0]
                role = first_msg.get("role")
                has_tool_calls = bool(first_msg.get("tool_calls"))
                if role == "tool" or (role == "assistant" and has_tool_calls):
                    messages_for_llm.pop(0)
                else:
                    break

            # Prepare System Prompt
            system_prompt_template = self.prompts.get(
                "system_prompt",
                "You are a helpful assistant. Current time is {current_time}.",
            )

            try:
                local_tz = pytz.timezone(self.timezone_str)
                current_time_str = (
                    self.clock.now()
                    .astimezone(local_tz)
                    .strftime("%Y-%m-%d %H:%M:%S %Z")
                )
            except Exception:
                current_time_str = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )

            aggregated_other_context_str = (
                await self._aggregate_context_from_providers()
            )

            format_args = {
                "user_name": user_name,
                "current_time": current_time_str,
                "aggregated_other_context": aggregated_other_context_str,
                "server_url": self.server_url,
                "profile_id": self.service_config.id,
            }

            # Safe format system prompt (simplified version)
            try:
                final_system_prompt = system_prompt_template.format(
                    **format_args
                ).strip()
            except Exception:
                final_system_prompt = system_prompt_template.strip()

            if final_system_prompt:
                messages_for_llm.insert(
                    0, {"role": "system", "content": final_system_prompt}
                )

            # Convert attachment URLs to data URIs before sending to LLM
            converted_trigger_parts = await self._convert_attachment_urls_to_data_uris(
                trigger_content_parts
            )

            # Add current user trigger message
            llm_user_content: str | list[dict[str, Any]]
            if (
                len(converted_trigger_parts) == 1
                and converted_trigger_parts[0].get("type") == "text"
            ):
                llm_user_content = converted_trigger_parts[0]["text"]
            else:
                llm_user_content = converted_trigger_parts

            messages_for_llm.append({"role": "user", "content": llm_user_content})

            # --- 3. Stream LLM Processing ---
            async for event, message_dict in self.process_message_stream(
                db_context=db_context,
                messages=messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,
                turn_id=turn_id,
                chat_interface=chat_interface,
                request_confirmation_callback=request_confirmation_callback,
            ):
                # Yield the event to the caller
                yield event

                # Save messages as they're generated
                if message_dict and message_dict.get("role"):
                    msg_to_save = message_dict.copy()
                    msg_to_save["interface_type"] = interface_type
                    msg_to_save["conversation_id"] = conversation_id
                    msg_to_save["turn_id"] = turn_id
                    msg_to_save["thread_root_id"] = thread_root_id_for_turn
                    msg_to_save["timestamp"] = msg_to_save.get(
                        "timestamp", self.clock.now()
                    )
                    msg_to_save.setdefault("interface_message_id", None)
                    msg_to_save["processing_profile_id"] = self.service_config.id

                    # Save each message in its own transaction to avoid PostgreSQL transaction issues
                    async with get_db_context(engine=db_context.engine) as msg_db:
                        await msg_db.message_history.add(**msg_to_save)

        except Exception as e:
            logger.error(f"Error in streaming chat interaction: {e}", exc_info=True)
            yield LLMStreamEvent(
                type="error", error=str(e), metadata={"error_id": str(uuid.uuid4())}
            )
