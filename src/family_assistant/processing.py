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
    UTC,
    datetime,
    timedelta,  # Added
)
from typing import (
    TYPE_CHECKING,
    Any,
)

if TYPE_CHECKING:
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper

import aiofiles
import pytz  # Added

from family_assistant.services.attachment_registry import AttachmentRegistry

# Import storage and calendar integration for context building
# storage import removed - using repository pattern via DatabaseContext
# --- NEW: Import ContextProvider ---
from .context_providers import ContextProvider
from .interfaces import ChatInterface  # Import ChatInterface

# Import the LLM interface and output structure
from .llm import LLMInterface, LLMStreamEvent, ToolCallItem
from .llm.google_types import GeminiProviderMetadata
from .llm.messages import (
    AssistantMessage,
    ContentPart,
    ContentPartDict,
    ErrorMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
    message_to_json_dict,
    tool_result_to_llm_message,
)

# Import DatabaseContext for type hinting
from .storage.context import DatabaseContext, get_db_context

# Import ToolsProvider interface and context
from .tools import ToolExecutionContext, ToolNotFoundError, ToolsProvider
from .tools.types import ToolAttachment, ToolResult
from .utils.clock import Clock, SystemClock

logger = logging.getLogger(__name__)


@dataclass
class ChatInteractionResult:
    """Result of a chat interaction from ProcessingService.handle_chat_interaction."""

    text_reply: str | None = None
    assistant_message_internal_id: int | None = None
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    reasoning_info: dict[str, Any] | None = None
    error_traceback: str | None = None
    attachment_ids: list[str] | None = None

    @property
    def has_error(self) -> bool:
        """Check if this result represents an error."""
        return self.error_traceback is not None


@dataclass
class ToolExecutionResult:
    """Result of executing a single tool call."""

    stream_event: "LLMStreamEvent"
    llm_message: "ToolMessage"
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    history_message: dict[str, Any]
    auto_attachment_ids: list[str] | None = None  # list of attachment IDs


# --- Configuration for ProcessingService ---
@dataclass
class ProcessingServiceConfig:
    """Configuration specific to a ProcessingService instance."""

    prompts: dict[str, str]
    timezone_str: str
    max_history_messages: int
    history_max_age_hours: int
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    tools_config: dict[
        str, Any
    ]  # Added to hold tool configurations like 'confirm_tools'
    delegation_security_level: str  # "blocked", "confirm", "unrestricted"
    id: str  # Unique identifier for this service profile
    description: str = ""  # Human-readable description of this profile
    # Type hint for model_parameters should reflect pattern -> params_dict structure
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    fallback_model_id: str | None = None  # Added for LLM fallback
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    fallback_model_parameters: dict[str, dict[str, Any]] | None = None  # Corrected type
    # Web-specific history settings
    web_max_history_messages: int | None = None  # If None, uses max_history_messages
    web_history_max_age_hours: int | None = None  # If None, uses history_max_age_hours
    max_iterations: int = 5


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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        app_config: dict[str, Any],  # Keep app_config for now
        clock: Clock | None = None,
        attachment_registry: AttachmentRegistry
        | None = None,  # AttachmentRegistry (required for attachment operations)
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
            attachment_registry: AttachmentRegistry instance for handling file attachments.
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

        # Store attachment registry
        self.attachment_registry = attachment_registry

        self.processing_services_registry: dict[str, ProcessingService] | None = None
        # Store the confirmation callback function if provided at init? No, get from context.
        self.home_assistant_client: HomeAssistantClientWrapper | None = (
            None  # Store HA client if available
        )
        self.event_sources = event_sources  # Store event sources for validation  # Store event sources for validation

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

    @property
    def max_iterations(self) -> int:
        return self.service_config.max_iterations

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

    async def process_message(
        self,
        db_context: DatabaseContext,  # Added db_context
        messages: list[LLMMessage],
        # --- Updated Signature ---
        interface_type: str,
        conversation_id: str,
        user_name: str,  # Added user_name
        turn_id: str,  # Added turn_id
        chat_interface: ChatInterface | None,  # Added chat_interface
        user_id: str | None = None,  # Added user_id
        chat_interfaces: dict[str, ChatInterface] | None = None,
        # Callback signature updated to match ToolExecutionContext's expectation
        request_confirmation_callback: (
            Callable[
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                [
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                    dict[str, Any],
                    float,
                    "ToolExecutionContext",
                ],
                Awaitable[bool],  # Changed int to str
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str] | None]:
        """
        Non-streaming version of process_message that uses the streaming generator internally.

        This method maintains backward compatibility by collecting all streaming events
        and returning the complete list of messages, final reasoning info, and attachment IDs.

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
            - A list of attachment IDs to send with the response (or None).
        """
        # Use the streaming generator and collect all messages
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        turn_messages: list[dict[str, Any]] = []
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        final_reasoning_info: dict[str, Any] | None = None
        final_attachment_ids: list[str] | None = None

        async for event, message_dict in self.process_message_stream(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            user_id=user_id,
            turn_id=turn_id,
            chat_interface=chat_interface,
            chat_interfaces=chat_interfaces,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
        ):
            # Collect messages that should be saved
            if message_dict and message_dict.get("role"):
                turn_messages.append(message_dict)

            # Extract reasoning info and attachment IDs from done events
            if event.type == "done" and event.metadata and "message" in event.metadata:
                assistant_msg = event.metadata["message"]
                if assistant_msg.get("reasoning_info"):
                    final_reasoning_info = assistant_msg["reasoning_info"]

                # Extract attachment IDs if present
                if "attachment_ids" in event.metadata:
                    final_attachment_ids = event.metadata["attachment_ids"]

        return turn_messages, final_reasoning_info, final_attachment_ids

    async def process_message_stream(
        self,
        db_context: DatabaseContext,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        request_confirmation_callback: (
            Callable[
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                [
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                    dict[str, Any],
                    float,
                    "ToolExecutionContext",
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> AsyncIterator[tuple[LLMStreamEvent, dict[str, Any]]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message_dict) where:
        - event: The LLMStreamEvent object
        - message_dict: The message dictionary to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        final_content: str | None = None
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        final_reasoning_info: dict[str, Any] | None = None
        max_iterations = self.max_iterations
        current_iteration = 1
        pending_attachment_ids: list[
            str
        ] = []  # Track attachment IDs from attach_to_response calls
        original_system_content: str | None = None  # Store original system prompt

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
            is_final_iteration = current_iteration == max_iterations

            logger.debug(
                "Starting streaming LLM interaction loop iteration %d/%d%s",
                current_iteration,
                max_iterations,
                " (FINAL - will force response without tools)"
                if is_final_iteration
                else "",
            )

            # Check if conversation has thought signatures that must be preserved
            # If so, we cannot modify the system prompt as it would invalidate signatures
            has_thought_signatures = False
            for msg in messages:
                if isinstance(msg, AssistantMessage) and msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.provider_metadata:
                            # Check if provider_metadata indicates Google thought signatures
                            if isinstance(tc.provider_metadata, GeminiProviderMetadata):
                                if tc.provider_metadata.thought_signature:
                                    has_thought_signatures = True
                                    break
                            elif isinstance(tc.provider_metadata, dict) and (
                                tc.provider_metadata.get("provider") == "google"
                                and "thought_signature" in tc.provider_metadata
                            ):
                                has_thought_signatures = True
                                break
                if has_thought_signatures:
                    break

            # Add iteration context to system prompt ONLY if no thought signatures present
            # Thought signatures are cryptographically tied to the exact conversation context
            if messages and messages[0].role == "system" and not has_thought_signatures:
                # Store original system content on first iteration
                if original_system_content is None:
                    original_system_content = str(messages[0].content)

                # Add iteration status to system prompt
                iteration_suffix = (
                    f"\n\n[Processing iteration {current_iteration}/{max_iterations}]"
                )
                if is_final_iteration:
                    iteration_suffix += "\nIMPORTANT: This is the final iteration. You MUST provide your final response now without requesting additional tools."

                # Create new message with modified content (Pydantic models are immutable)
                messages[0] = SystemMessage(
                    content=original_system_content + iteration_suffix
                )

            # Stream from LLM
            accumulated_content = []
            tool_calls_from_stream = []
            done_provider_metadata = None  # Initialize before loop

            # On final iteration, don't offer any tools to ensure we get a response
            tools_to_offer = None if is_final_iteration else tools_for_llm
            tool_choice_mode = (
                "none" if is_final_iteration or not tools_to_offer else "auto"
            )

            try:
                async for event in self.llm_client.generate_response_stream(
                    messages=messages,
                    tools=tools_to_offer,
                    tool_choice=tool_choice_mode,
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
                        # Extract provider_metadata from done event if present
                        done_provider_metadata = (
                            event.metadata.get("provider_metadata")
                            if event.metadata
                            else None
                        )

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

            # Extract provider_metadata from tool calls or done event
            # Keep as typed objects (GeminiProviderMetadata) to preserve thought signatures
            provider_metadata = None
            if tool_calls_from_stream and tool_calls_from_stream[0].provider_metadata:
                # Extract provider_metadata from first tool call (all have the same metadata)
                provider_metadata = tool_calls_from_stream[0].provider_metadata
            elif done_provider_metadata:
                # Use provider_metadata from done event if not in tool calls
                provider_metadata = done_provider_metadata

            # Serialize provider_metadata to dict before creating message dict
            # This ensures it's JSON-serializable when saved to database
            serialized_provider_metadata = None
            if provider_metadata:
                if isinstance(provider_metadata, GeminiProviderMetadata):
                    serialized_provider_metadata = provider_metadata.to_dict()
                else:
                    # Already a dict or other serializable type
                    serialized_provider_metadata = provider_metadata

            # Also serialize provider_metadata inside final_reasoning_info if present
            # final_reasoning_info comes from event.metadata which may contain unserialized objects
            serialized_reasoning_info = None
            if final_reasoning_info:
                serialized_reasoning_info = final_reasoning_info.copy()
                if "provider_metadata" in serialized_reasoning_info:
                    pm = serialized_reasoning_info["provider_metadata"]
                    if isinstance(pm, GeminiProviderMetadata):
                        serialized_reasoning_info["provider_metadata"] = pm.to_dict()

            # Create assistant message with serialized provider_metadata
            # tool_calls remain as typed ToolCallItem objects - repository handles those
            assistant_message_for_turn = {
                "role": "assistant",
                "content": final_content,
                "tool_calls": tool_calls_from_stream,  # Pass typed ToolCallItem objects directly
                "reasoning_info": serialized_reasoning_info,
                "provider_metadata": serialized_provider_metadata,
                "tool_call_id": None,
                "error_traceback": None,
            }

            # Yield a synthetic "done" event with the complete assistant message
            # Include attachment IDs if any were captured from attach_to_response calls
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            done_metadata: dict[str, Any] = {"message": assistant_message_for_turn}
            if pending_attachment_ids:
                # Fetch full metadata for each attachment for web UI display
                attachment_details = []
                if self.attachment_registry:
                    for att_id in pending_attachment_ids:
                        try:
                            metadata = await self.attachment_registry.get_attachment_with_context(
                                att_id
                            )
                            if metadata:
                                attachment_details.append({
                                    "id": att_id,
                                    "type": "image",  # Currently all are images, could use metadata.mime_type
                                    "name": metadata.description or "Attachment",
                                    "content": f"/api/attachments/{att_id}",
                                    "mime_type": metadata.mime_type,
                                    "size": metadata.size,
                                })
                        except Exception as e:
                            logger.warning(
                                f"Failed to fetch metadata for attachment {att_id}: {e}"
                            )

                done_metadata["attachment_ids"] = pending_attachment_ids
                done_metadata["attachments"] = attachment_details
                logger.info(
                    f"Including {len(pending_attachment_ids)} attachment IDs and {len(attachment_details)} attachment details in done event"
                )

            yield (
                LLMStreamEvent(type="done", metadata=done_metadata),
                assistant_message_for_turn,
            )

            # Add to context for next iteration
            # Reuse the original ToolCallItem objects from the stream
            # (no need to serialize and deserialize within the same function)
            llm_context_assistant_message = AssistantMessage(
                role="assistant",
                content=final_content,
                tool_calls=tool_calls_from_stream,
            )
            messages.append(llm_context_assistant_message)

            # Break if no tool calls
            if not tool_calls_from_stream:
                logger.info(
                    "LLM streaming response received with no further tool calls."
                )
                break

            # Force break on final iteration to ensure we get a response
            # This prevents infinite loops if LLM somehow returns tool calls on the last iteration
            if is_final_iteration:
                logger.warning(
                    f"Final iteration ({max_iterations}) reached but LLM returned tool calls. "
                    "Forcing break to ensure response is returned. Tool calls will be ignored."
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
                        user_id=user_id,
                        turn_id=turn_id,
                        db_context=db_context,
                        chat_interface=chat_interface,
                        chat_interfaces=chat_interfaces,
                        request_confirmation_callback=request_confirmation_callback,
                        subconversation_id=subconversation_id,
                    )
                )
                for tool_call in tool_calls_from_stream
            ]

            # Process results as they complete
            for completed_task in asyncio.as_completed(tool_tasks):
                try:
                    result = await completed_task
                    event = result.stream_event
                    llm_message = result.llm_message
                    history_message = result.history_message
                    auto_attachment_ids = result.auto_attachment_ids or []

                    # Auto-queue tool result attachments
                    for auto_attachment_id in auto_attachment_ids:
                        if auto_attachment_id not in pending_attachment_ids:
                            pending_attachment_ids.append(auto_attachment_id)
                            logger.info(
                                f"Auto-queued tool attachment {auto_attachment_id} for display"
                            )

                    # Check if this is an attach_to_response tool call
                    tool_name = history_message.get("tool_name")
                    if tool_name == "attach_to_response" and event.tool_result:
                        try:
                            result_data = json.loads(event.tool_result)
                            if (
                                result_data.get("status") == "attachments_queued"
                                and "attachment_ids" in result_data
                            ):
                                attachment_ids = result_data["attachment_ids"]
                                # LLM is taking control - replace auto-collected attachments with explicit list
                                old_count = len(pending_attachment_ids)
                                pending_attachment_ids.clear()
                                pending_attachment_ids.extend(attachment_ids)
                                logger.info(
                                    f"LLM explicitly controlling attachments: replaced {old_count} auto-queued with {len(attachment_ids)} explicit attachments"
                                )
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(
                                f"Failed to parse attach_to_response result: {e}"
                            )

                    # Yield tool result event (history_message for database storage)
                    yield (event, history_message)

                    # Add to messages for LLM (llm_message with _attachment)
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
        tool_call_item_obj: ToolCallItem,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        db_context: DatabaseContext,
        chat_interface: ChatInterface | None,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        request_confirmation_callback: Callable[
            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            [
                str,
                str,
                str | None,
                str,
                str,
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                dict[str, Any],
                float,
                "ToolExecutionContext",
            ],
            Awaitable[bool],
        ]
        | None = None,
        subconversation_id: str | None = None,
    ) -> ToolExecutionResult:
        """Execute a single tool call and return the result.

        Args:
            tool_call_item_obj: The tool call object from LLM (ToolCallItem instance)
            interface_type: Interface type (e.g., 'telegram')
            conversation_id: Conversation identifier
            user_name: User name for context
            turn_id: Current turn identifier
            db_context: Database context
            chat_interface: Chat interface for sending messages
            request_confirmation_callback: Callback for tool confirmation

        Returns:
            Tuple of (event, tool_response_message, llm_message, auto_attachment_id)
        """
        call_id = tool_call_item_obj.id
        function_name = tool_call_item_obj.function.name
        function_args = tool_call_item_obj.function.arguments

        # Validate tool call
        if not call_id or not function_name:
            logger.error(f"Invalid tool call: id='{call_id}', name='{function_name}'")
            error_content = "Error: Invalid tool call structure."
            error_traceback = "Invalid tool call structure received from LLM."
            func_name = function_name or "unknown_function"

            llm_message = ToolMessage(
                role="tool",
                tool_call_id=call_id or f"missing_id_{uuid.uuid4()}",
                content=error_content,
                error_traceback=error_traceback,
                name=func_name,
            )

            # Create history message from ToolMessage
            history_message = message_to_json_dict(llm_message)
            history_message["tool_name"] = func_name

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                llm_message=llm_message,
                history_message=history_message,
                auto_attachment_ids=None,  # No attachments for error cases
            )

        # Parse arguments
        try:
            if isinstance(function_args, str):
                arguments = json.loads(function_args)
            else:
                arguments = function_args
        except json.JSONDecodeError:
            logger.error(
                f"Failed to parse arguments for {function_name}: {function_args}"
            )
            error_content = f"Error: Invalid arguments format for {function_name}."
            error_traceback = f"JSONDecodeError: {function_args}"

            llm_message = ToolMessage(
                role="tool",
                tool_call_id=call_id,
                content=error_content,
                error_traceback=error_traceback,
                name=function_name,
            )

            # Create history message from ToolMessage
            history_message = message_to_json_dict(llm_message)
            history_message["tool_name"] = function_name

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                llm_message=llm_message,
                history_message=history_message,
                auto_attachment_ids=None,  # No attachments for error cases
            )

        # Execute tool
        logger.info(f"Executing tool '{function_name}' with args: {arguments}")

        # Build chat_interfaces dict for cross-interface messaging
        # Use the provided chat_interfaces (containing all registered interfaces) if available,
        # otherwise fall back to just the current interface for backward compatibility
        chat_interfaces_dict = chat_interfaces
        if chat_interfaces_dict is None and chat_interface:
            # Fallback: if no registry provided, create dict with just current interface
            chat_interfaces_dict = {interface_type: chat_interface}

        tool_execution_context = ToolExecutionContext(
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            user_id=user_id,
            turn_id=turn_id,
            db_context=db_context,
            chat_interface=chat_interface,
            chat_interfaces=chat_interfaces_dict,
            timezone_str=self.timezone_str,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            request_confirmation_callback=request_confirmation_callback,
            processing_service=self,
            clock=self.clock,
            home_assistant_client=self.home_assistant_client,
            event_sources=self.event_sources,
            indexing_source=(
                self.event_sources.get("indexing") if self.event_sources else None
            ),
            attachment_registry=self.attachment_registry,
        )

        try:
            # Execute the tool
            result = await self.tools_provider.execute_tool(
                function_name, arguments, tool_execution_context, call_id
            )
            logger.info(f"Tool '{function_name}' executed successfully.")

            # Handle both string and ToolResult
            if isinstance(result, ToolResult):
                content_for_stream = result.text
                auto_attachment_ids: list[
                    str
                ] = []  # Track attachment IDs for auto-queuing

                # Extract attachment metadata for streaming
                stream_metadata = None
                attachments_data = []
                if result.attachments:
                    for attachment in result.attachments:
                        attachment_data = {
                            "type": "tool_result",
                            "mime_type": attachment.mime_type,
                            "description": attachment.description,
                        }

                        # Determine if this is a new attachment (has content) or a reference (has ID but no content)
                        if attachment.content and self.attachment_registry:
                            # New attachment with content - store it
                            try:
                                # Store the attachment content with proper file extension
                                file_extension = (
                                    self._get_file_extension_from_mime_type(
                                        attachment.mime_type
                                    )
                                )
                                # Store and register the attachment using AttachmentRegistry
                                registered_metadata = await self.attachment_registry.store_and_register_tool_attachment(
                                    file_content=attachment.content,
                                    filename=f"tool_result_{uuid.uuid4()}{file_extension}",
                                    content_type=attachment.mime_type,
                                    tool_name=function_name,
                                    description=attachment.description
                                    or f"Output from {function_name}",
                                    conversation_id=conversation_id,
                                    metadata={
                                        "tool_call_id": call_id,
                                        "auto_display": True,
                                    },
                                )

                                attachment_data["content_url"] = (
                                    registered_metadata.content_url or ""
                                )
                                attachment_data["attachment_id"] = (
                                    registered_metadata.attachment_id
                                )
                                # Queue this newly stored attachment
                                auto_attachment_ids.append(
                                    registered_metadata.attachment_id
                                )

                                # Populate the attachment_id in the ToolAttachment object
                                attachment.attachment_id = (
                                    registered_metadata.attachment_id
                                )

                                logger.info(
                                    f"Stored and registered tool attachment: {registered_metadata.attachment_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to store tool result attachment: {e}"
                                )
                                # Continue without URL if storage fails
                        elif attachment.attachment_id:
                            # Reference to existing attachment - just queue it
                            attachment_data["attachment_id"] = attachment.attachment_id
                            # Note: content_url might not be available for references, that's OK
                            auto_attachment_ids.append(attachment.attachment_id)
                            logger.info(
                                f"Queuing existing attachment reference: {attachment.attachment_id}"
                            )

                        attachments_data.append(attachment_data)

                    stream_metadata = {"attachments": attachments_data}

                # Create LLM message AFTER storing attachments (if any) so attachment_ids are populated
                llm_message = tool_result_to_llm_message(
                    result,
                    call_id,
                    function_name,
                    provider_metadata=tool_call_item_obj.provider_metadata,
                )

                # Inject attachment IDs into LLM message content so LLM can reference them in subsequent calls
                if auto_attachment_ids:
                    attachment_id_list = ", ".join(auto_attachment_ids)
                    modified_content = (
                        llm_message.content
                        + f"\n[Attachment ID(s): {attachment_id_list}]"
                    )
                    # Create new ToolMessage with modified content using model_copy
                    llm_message = llm_message.model_copy(
                        update={"content": modified_content}
                    )

                # Create history_message from the modified llm_message to preserve attachment IDs
                history_message = message_to_json_dict(llm_message)
                history_message["tool_name"] = (
                    function_name  # Store tool name for database
                )
                if attachments_data:
                    history_message["attachments"] = attachments_data
            else:
                # Backward compatible string handling
                content_for_stream = str(result)
                auto_attachment_ids = []  # String results don't generate attachments
                llm_message = ToolMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=content_for_stream,
                    name=function_name,
                )
                # Create history message for string results
                history_message = message_to_json_dict(llm_message)
                history_message["tool_name"] = function_name
                stream_metadata = None

                # Special handling for attach_to_response tool: enrich with attachment metadata
                if function_name == "attach_to_response":
                    try:
                        result_data = json.loads(content_for_stream)
                        if (
                            result_data.get("status") == "attachments_queued"
                            and "attachment_ids" in result_data
                        ):
                            # Only enrich metadata if attachment registry is available
                            if self.attachment_registry:
                                attachment_registry = self.attachment_registry

                                attachment_metadata_list = []
                                for attachment_id in result_data["attachment_ids"]:
                                    try:
                                        attachment_info = (
                                            await attachment_registry.get_attachment(
                                                db_context, attachment_id
                                            )
                                        )
                                        if attachment_info:
                                            attachment_metadata_list.append({
                                                "attachment_id": attachment_id,
                                                "type": "tool_result",
                                                "description": attachment_info.description
                                                or "Attachment",
                                                "url": attachment_info.content_url,
                                                "content_url": attachment_info.content_url,
                                                "mime_type": attachment_info.mime_type,
                                                "size": attachment_info.size,
                                            })
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to get metadata for attachment {attachment_id}: {e}"
                                        )

                                if attachment_metadata_list:
                                    stream_metadata = {
                                        "attachments": attachment_metadata_list
                                    }
                                    logger.info(
                                        f"Enriched attach_to_response result with {len(attachment_metadata_list)} attachment metadata entries"
                                    )
                            else:
                                logger.warning(
                                    "AttachmentRegistry not available, skipping metadata enrichment for attach_to_response"
                                )
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(
                            f"Failed to parse attach_to_response result for metadata enrichment: {e}"
                        )
                        # Continue with normal processing if parsing fails

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=content_for_stream,
                    metadata=stream_metadata,
                ),
                llm_message=llm_message,
                history_message=history_message,
                auto_attachment_ids=auto_attachment_ids
                if auto_attachment_ids
                else None,
            )

        except ToolNotFoundError:
            logger.error(f"Tool '{function_name}' not found.")
            error_content = f"Error: Tool '{function_name}' not found."
            error_traceback = traceback.format_exc()

            llm_message = ToolMessage(
                role="tool",
                tool_call_id=call_id,
                content=error_content,
                error_traceback=error_traceback,
                name=function_name,
            )

            # Create history message from ToolMessage
            history_message = message_to_json_dict(llm_message)
            history_message["tool_name"] = function_name

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                llm_message=llm_message,
                history_message=history_message,
                auto_attachment_ids=None,  # No attachments for error cases
            )

        except Exception as e:
            logger.error(f"Error executing tool '{function_name}': {e}", exc_info=True)
            error_content = f"Error executing {function_name}: {str(e)}"
            error_traceback = traceback.format_exc()

            llm_message = ToolMessage(
                role="tool",
                tool_call_id=call_id,
                content=error_content,
                error_traceback=error_traceback,
                name=function_name,
            )

            # Create history message from ToolMessage
            history_message = message_to_json_dict(llm_message)
            history_message["tool_name"] = function_name

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                llm_message=llm_message,
                history_message=history_message,
                auto_attachment_ids=None,  # No attachments for error cases
            )

    async def _process_attachment_content_parts(
        self,
        db_context: DatabaseContext,
        conversation_id: str,
        content_parts: list[ContentPartDict],
    ) -> tuple[list[ContentPartDict], list[LLMMessage]]:
        """
        Process attachment content parts by fetching and injecting them as user messages.

        This handles {"type": "attachment", "attachment_id": "..."} content parts,
        which are created when attachments are passed through delegate_to_service.
        It converts them into proper LLM-visible attachment injections.

        Args:
            db_context: Database context for attachment queries
            conversation_id: Current conversation ID for security validation
            content_parts: List of content parts that may contain attachment references

        Returns:
            Tuple of (modified_content_parts, injection_messages)
        """
        logger.info(
            f"_process_attachment_content_parts called with {len(content_parts)} parts, "
            f"attachment_registry={'present' if self.attachment_registry else 'MISSING'}"
        )
        if not self.attachment_registry:
            logger.warning(
                "Attachment registry not available - skipping attachment content part processing"
            )
            return content_parts, []

        modified_parts = []
        injection_messages = []

        for part in content_parts:
            logger.debug(f"Processing content part: {part}")
            if part.get("type") == "attachment":
                attachment_id = part.get("attachment_id")
                if not attachment_id:
                    logger.warning(
                        "Attachment content part missing attachment_id, skipping"
                    )
                    continue

                try:
                    # Fetch attachment metadata
                    attachment_metadata = await self.attachment_registry.get_attachment(
                        db_context, attachment_id
                    )

                    if not attachment_metadata:
                        logger.warning(
                            f"Attachment {attachment_id} not found in registry, skipping"
                        )
                        continue

                    # Security: Verify conversation scoping
                    if attachment_metadata.conversation_id != conversation_id:
                        logger.error(
                            f"Security violation: Attachment {attachment_id} from conversation "
                            f"{attachment_metadata.conversation_id} not accessible from "
                            f"conversation {conversation_id}"
                        )
                        continue

                    # Fetch attachment content
                    content = await self.attachment_registry.get_attachment_content(
                        db_context, attachment_id, conversation_id
                    )

                    if content is None:
                        logger.warning(
                            f"Could not retrieve content for attachment {attachment_id}"
                        )
                        continue

                    # Create ToolAttachment object
                    tool_attachment = ToolAttachment(
                        content=content,
                        mime_type=attachment_metadata.mime_type,
                        attachment_id=attachment_id,
                        description=attachment_metadata.description or "Attachment",
                    )

                    # Generate injection message using LLM client's logic
                    injection_msg = self.llm_client.create_attachment_injection(
                        tool_attachment
                    )
                    injection_messages.append(injection_msg)

                    logger.info(
                        f"Processed attachment content part {attachment_id} for LLM injection"
                    )

                except Exception as e:
                    logger.error(
                        f"Error processing attachment content part {attachment_id}: {e}",
                        exc_info=True,
                    )
                    continue
            else:
                # Not an attachment part, keep as-is
                modified_parts.append(part)

        return modified_parts, injection_messages

    async def _convert_attachment_urls_to_data_uris(
        self,
        content_parts: list[ContentPartDict],
    ) -> list[ContentPartDict]:
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
        if not self.attachment_registry:
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

                        # Use AttachmentRegistry to get the file path
                        file_path = self.attachment_registry.get_attachment_path(
                            attachment_id
                        )

                        if file_path and file_path.exists():
                            try:
                                # Read file asynchronously
                                async with aiofiles.open(file_path, "rb") as f:
                                    file_bytes = await f.read()

                                # Detect MIME type from file extension
                                content_type = (
                                    self.attachment_registry.get_content_type(file_path)
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

    async def _extract_conversation_attachments_context(
        self, db_context: DatabaseContext, conversation_id: str, max_age_hours: int
    ) -> str:
        """
        Extracts recent attachment information from the conversation and formats it for LLM context.

        Args:
            db_context: Database context for attachment queries.
            conversation_id: Conversation identifier to query attachments for.
            max_age_hours: Maximum age of attachments to include (in hours).

        Returns:
            Formatted string with attachment context, or empty string if no attachments found.
        """
        if not self.attachment_registry:
            return ""

        try:
            # Query recent attachments using storage layer method
            cutoff_time = self.clock.now() - timedelta(hours=max_age_hours)

            attachments = (
                await self.attachment_registry.get_recent_attachments_for_conversation(
                    db_context=db_context,
                    conversation_id=conversation_id,
                    max_age=cutoff_time,
                )
            )

            if not attachments:
                return ""

            # Format attachment context
            attachment_items = []
            now = self.clock.now()

            for attachment in attachments:
                attachment_id = attachment.attachment_id
                filename = attachment.description or "unknown"
                content_type = attachment.mime_type or "unknown"
                created_at = attachment.created_at

                # Ensure created_at is timezone-aware (SQLite may return naive datetimes)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)

                # Calculate age
                age = now - created_at
                if age.total_seconds() < 3600:  # Less than 1 hour
                    minutes = int(age.total_seconds() / 60)
                    age_str = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
                else:
                    hours = int(age.total_seconds() / 3600)
                    age_str = f"{hours} hour{'s' if hours != 1 else ''} ago"

                attachment_items.append(
                    f"- [{attachment_id}] {filename} ({content_type}) - {age_str}"
                )

            # Use prompt template
            items_str = "\n".join(attachment_items)
            header_template = self.prompts.get(
                "thread_attachments_context_header",
                "Recent Attachments in Conversation:\n{attachments_list}",
            )

            return header_template.format(attachments_list=items_str)

        except Exception as e:
            logger.error(
                f"Error extracting conversation attachments context: {e}", exc_info=True
            )
            return ""

    async def _format_history_for_llm(
        self,
        history_messages: list[LLMMessage],
    ) -> list[LLMMessage]:
        """
        Formats message history retrieved from the database, handling assistant tool calls correctly.

        Args:
            history_messages: List of typed LLMMessage objects from db_context.message_history.get_recent.

        Returns:
            A list of LLMMessage objects formatted for the LLM API.
        """
        messages: list[LLMMessage] = []
        # Process history messages, formatting assistant tool calls correctly
        for msg in history_messages:
            if isinstance(msg, AssistantMessage):
                # Check if tool_calls have thought signatures
                has_thought_signature = False
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.provider_metadata:
                            tc_metadata = tc.provider_metadata
                            # Check if it's a Google thought signature
                            if isinstance(tc_metadata, GeminiProviderMetadata):
                                if tc_metadata.thought_signature:
                                    has_thought_signature = True
                                    break
                            elif (
                                isinstance(tc_metadata, dict)
                                and tc_metadata.get("provider") == "google"
                                and "thought_signature" in tc_metadata
                            ):
                                has_thought_signature = True
                                break

                # Strip text content from messages with tool calls UNLESS they have thought signatures
                # Thought signatures are cryptographically tied to exact conversation context
                # So if a thought signature is present, we MUST preserve the original text content exactly.
                final_content: str | None = msg.content

                if msg.tool_calls and msg.content and not has_thought_signature:
                    # Only strip text if NO thought signature is present, to avoid redundancy/partial response issues
                    # with other providers. But for Google with signatures, we keep it.
                    final_content = None
                    logger.debug(
                        f"Stripped text content from assistant message with tool calls (no signature). Original content: {msg.content[:100]}..."
                    )

                # Create new AssistantMessage with potentially modified content
                assistant_msg = AssistantMessage(
                    content=final_content,
                    tool_calls=msg.tool_calls,
                    provider_metadata=msg.provider_metadata,
                )
                messages.append(assistant_msg)
            elif isinstance(msg, ToolMessage):
                # --- Format tool response messages ---
                if (
                    msg.tool_call_id
                ):  # Only include if tool_call_id is present (retrieved from DB)
                    messages.append(msg)
                else:
                    # Log a warning if a tool message is found without an ID (indicates logging issue)
                    logger.warning(
                        f"Found 'tool' role message in history without a tool_call_id: {msg}"
                    )
                    # Skip adding malformed tool message to history to avoid LLM errors
            elif isinstance(msg, ErrorMessage):
                # Include error messages as assistant messages so LLM knows it responded
                error_content = f"I encountered an error: {msg.content}"
                if msg.error_traceback:
                    error_content += f"\n\nError details: {msg.error_traceback}"
                messages.append(AssistantMessage(content=error_content))
            else:
                # SystemMessage, UserMessage, or other message types - pass through as-is
                messages.append(msg)

        logger.debug(
            f"Formatted {len(history_messages)} DB history messages into {len(messages)} LLM messages."
        )
        return messages

    async def handle_chat_interaction(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        user_id: str | None = None,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        request_confirmation_callback: (
            Callable[
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                [
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                    dict[str, Any],
                    float,
                    "ToolExecutionContext",
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        trigger_attachments: list[dict[str, Any]] | None = None,
        subconversation_id: str | None = None,
    ) -> ChatInteractionResult:
        """
        Handles a complete chat interaction from user input to final response.

        This method orchestrates the entire conversation flow:
        1. Context aggregation (messages, attachments, calendar, etc.)
        2. LLM processing with tool execution
        3. Message saving and final response extraction
        4. Error handling and recovery

        Args:
            db_context: Database context for operations
            interface_type: Type of interface (e.g., "telegram", "web")
            conversation_id: Unique conversation identifier
            trigger_content_parts: User's message content parts
            trigger_interface_message_id: Interface-specific message ID
            user_name: Name of the user
            replied_to_interface_id: ID of message being replied to
            chat_interface: Interface for sending messages
            request_confirmation_callback: Callback for tool confirmations
            trigger_attachments: Attachments from the user

        Returns:
            ChatInteractionResult containing:
            - text_reply: Final LLM content to send to user (str | None)
            - assistant_message_internal_id: Internal message ID of assistant's response (int | None)
            - reasoning_info: Final reasoning information (dict | None)
            - error_traceback: Processing error traceback if any (str | None)
            - attachment_ids: Response attachment IDs (list[str] | None)
        """

        turn_id = str(uuid.uuid4())
        logger.info(
            f"Starting handle_chat_interaction for conversation {conversation_id}, turn {turn_id}"
        )

        try:
            # --- 1. Determine Thread Root ID & Save User Trigger Message ---
            thread_root_id_for_turn: int | None = None
            user_message_timestamp = self.clock.now()  # Timestamp for user message

            if replied_to_interface_id:
                # Look up the replied-to message to get its thread_root_id
                replied_to_msg_row = (
                    await db_context.message_history.get_row_by_interface_id(
                        interface_type=interface_type,
                        interface_message_id=replied_to_interface_id,
                    )
                )
                if replied_to_msg_row:
                    # Use the thread root from the replied-to message, or its internal_id if it's a root
                    thread_root_id_for_turn = replied_to_msg_row.get(
                        "thread_root_id"
                    ) or replied_to_msg_row.get("internal_id")
                    logger.info(
                        f"Received reply to interface message {replied_to_interface_id}. "
                        f"Thread root ID: {thread_root_id_for_turn}"
                    )
                else:
                    logger.warning(
                        f"Replied-to interface message {replied_to_interface_id} not found. "
                        "Creating new thread."
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

            # Generate a temporary interface_message_id if not provided
            # This ensures the just-saved message can be filtered out of history
            actual_interface_message_id = trigger_interface_message_id
            if actual_interface_message_id is None:
                actual_interface_message_id = f"temp_{turn_id}"

            saved_user_msg_record = await db_context.message_history.add(
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=actual_interface_message_id,
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
                subconversation_id=subconversation_id,  # Pass subconversation ID
                user_id=user_id,  # Pass user_id
            )

            if saved_user_msg_record and not thread_root_id_for_turn:
                # If it was the first message in a thread, its own ID is the root.
                thread_root_id_for_turn = saved_user_msg_record.get("internal_id")
                if thread_root_id_for_turn:
                    logger.info(
                        f"Established new thread_root_id: {thread_root_id_for_turn}"
                    )

            # --- 2. Prepare LLM Context (History, System Prompt) ---
            # Use interface-specific history limits
            history_limit, history_max_age = self._get_history_limits_for_interface(
                interface_type
            )

            try:
                if actual_interface_message_id:
                    # Need to filter - use typed metadata
                    messages_with_metadata = (
                        await db_context.message_history.get_recent_with_typed_metadata(
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            limit=history_limit,
                            max_age=history_max_age,
                            processing_profile_id=self.service_config.id,
                            subconversation_id=subconversation_id,
                        )
                    )

                    # Filter out current trigger message
                    filtered_with_metadata = [
                        msg_meta
                        for msg_meta in messages_with_metadata
                        if msg_meta.interface_message_id != actual_interface_message_id
                    ]

                    # Extract typed messages
                    raw_history_messages = [
                        msg_meta.message for msg_meta in filtered_with_metadata
                    ]
                else:
                    # No filtering needed - use direct typed messages
                    raw_history_messages = await db_context.message_history.get_recent(
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        limit=history_limit,
                        max_age=history_max_age,
                        processing_profile_id=self.service_config.id,
                        subconversation_id=subconversation_id,
                    )
            except Exception as hist_err:
                logger.error(
                    f"Failed to get message history for {interface_type}:{conversation_id}: {hist_err}",
                    exc_info=True,
                )
                raw_history_messages = []  # Continue with empty history on error

            logger.debug(f"Raw history messages fetched ({len(raw_history_messages)}).")

            initial_messages_for_llm = await self._format_history_for_llm(
                raw_history_messages
            )
            logger.debug(
                f"Initial messages for LLM after formatting history ({len(initial_messages_for_llm)})."
            )

            # Handle reply thread context
            thread_attachments_context = ""
            if replied_to_interface_id and thread_root_id_for_turn:
                try:
                    logger.info(
                        f"Fetching full thread history for root ID {thread_root_id_for_turn} due to reply."
                    )
                    if actual_interface_message_id:
                        full_thread_with_metadata = await db_context.message_history.get_recent_with_typed_metadata(
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            thread_root_id=thread_root_id_for_turn,
                            processing_profile_id=None,  # Get ALL messages in thread regardless of profile
                            subconversation_id=subconversation_id,
                        )

                        # Filter out current trigger
                        filtered_thread = [
                            msg_meta.message
                            for msg_meta in full_thread_with_metadata
                            if msg_meta.interface_message_id
                            != actual_interface_message_id
                        ]
                        initial_messages_for_llm = await self._format_history_for_llm(
                            filtered_thread
                        )
                    else:
                        full_thread_messages_db = await db_context.message_history.get_by_thread_id(
                            thread_root_id=thread_root_id_for_turn,
                            processing_profile_id=None,  # Get ALL messages in thread regardless of profile
                            subconversation_id=subconversation_id,
                        )
                        initial_messages_for_llm = await self._format_history_for_llm(
                            full_thread_messages_db
                        )
                    logger.info(
                        f"Using {len(initial_messages_for_llm)} messages from full thread history for LLM context."
                    )

                    # Extract attachment context from conversation
                    thread_attachments_context = (
                        await self._extract_conversation_attachments_context(
                            db_context, conversation_id, self.history_max_age_hours
                        )
                    )
                    if thread_attachments_context:
                        logger.debug(
                            "Extracted attachment context from thread messages for LLM."
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
                # Check if this is a ToolMessage or AssistantMessage with tool calls
                is_tool_msg = isinstance(first_msg, ToolMessage)
                is_assistant_with_tools = (
                    isinstance(first_msg, AssistantMessage) and first_msg.tool_calls
                )
                if is_tool_msg or is_assistant_with_tools:
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
                current_time_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

            aggregated_other_context_str = (
                await self._aggregate_context_from_providers()
            )

            # Add thread attachments context if available
            if thread_attachments_context:
                if aggregated_other_context_str:
                    aggregated_other_context_str += "\n\n" + thread_attachments_context
                else:
                    aggregated_other_context_str = thread_attachments_context

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
                messages_for_llm.insert(0, SystemMessage(content=final_system_prompt))

            # Process attachment content parts from delegation
            (
                processed_trigger_parts,
                attachment_injection_messages,
            ) = await self._process_attachment_content_parts(
                db_context, conversation_id, trigger_content_parts
            )

            # Attachment injection messages are already typed LLMMessage objects from the LLM client
            # Just append them directly to messages_for_llm
            messages_for_llm.extend(attachment_injection_messages)

            # Convert attachment URLs to data URIs before sending to LLM
            processed_trigger_parts = await self._convert_attachment_urls_to_data_uris(
                processed_trigger_parts
            )

            # Add the current user trigger message to messages_for_llm
            # We filtered it out from history to avoid duplication issues, but we need to add it
            # here so the LLM can see the current user request
            # Extract text content to match the format used when saving to history (line 1617-1633)
            user_content_for_llm: str | list[ContentPart]
            if processed_trigger_parts and len(processed_trigger_parts) == 1:
                # Single part - extract text if it's a text part
                first_part = processed_trigger_parts[0]
                if isinstance(first_part, dict) and first_part.get("type") == "text":
                    user_content_for_llm = str(first_part.get("text", ""))
                else:
                    # Non-text single part, keep as list
                    user_content_for_llm = processed_trigger_parts  # type: ignore[assignment]  # ContentPartDict is structurally compatible with ContentPart at runtime
            elif processed_trigger_parts:
                # Multiple parts - keep as list for multimodal content
                user_content_for_llm = processed_trigger_parts  # type: ignore[assignment]  # ContentPartDict is structurally compatible with ContentPart at runtime
            else:
                # No parts - empty string
                user_content_for_llm = ""

            messages_for_llm.append(UserMessage(content=user_content_for_llm))

            # Messages are already typed (list[LLMMessage])
            typed_messages_for_llm = messages_for_llm

            # --- 3. Call Core LLM Processing (self.process_message) ---
            (
                generated_turn_messages,
                final_reasoning_info_from_process_msg,
                response_attachment_ids,
            ) = await self.process_message(
                db_context=db_context,
                messages=typed_messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,  # Pass user_name
                user_id=user_id,  # Pass user_id
                turn_id=turn_id,
                chat_interface=chat_interface,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,  # Pass subconversation ID
            )
            final_reasoning_info = final_reasoning_info_from_process_msg

            # --- 4. Save Generated Turn Messages & Extract Final Reply ---
            final_text_reply = None
            final_assistant_message_internal_id = None

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
                    msg_to_save["subconversation_id"] = subconversation_id
                    msg_to_save["user_id"] = user_id

                    # Remove fields that shouldn't be saved to database
                    msg_to_save.pop("_attachment", None)  # Remove raw attachment data

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

            return ChatInteractionResult(
                text_reply=final_text_reply,
                assistant_message_internal_id=final_assistant_message_internal_id,
                reasoning_info=final_reasoning_info,
                error_traceback=None,
                attachment_ids=response_attachment_ids,
            )

        except Exception:
            logger.error(
                f"Error in handle_chat_interaction for conversation {conversation_id}, turn {turn_id}",
                exc_info=True,
            )
            # Capture the full traceback as a string
            processing_error_traceback = traceback.format_exc()

            # Create a user-friendly error message
            error_message = (
                "I encountered an error while processing your message. "
                "Please try again, and if the problem persists, contact support."
            )

            # Save error message to conversation history
            try:
                error_message_record = await db_context.message_history.add_message(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=None,  # Will be set when sent
                    turn_id=turn_id,
                    thread_root_id=thread_root_id_for_turn,
                    timestamp=datetime.now(UTC),
                    role="assistant",
                    content=error_message,
                    subconversation_id=subconversation_id,
                )
                error_message_internal_id = (
                    error_message_record.get("internal_id")
                    if error_message_record
                    else None
                )
            except Exception as error_save_err:
                logger.error(
                    f"Failed to save error message to history: {error_save_err}",
                    exc_info=True,
                )

            # Return the error message and its ID so the caller can send it to the user
            return ChatInteractionResult(
                text_reply=error_message,
                assistant_message_internal_id=error_message_internal_id,
                reasoning_info=None,
                error_traceback=processing_error_traceback,
                attachment_ids=None,
            )

    async def handle_chat_interaction_stream(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        user_id: str | None = None,
        replied_to_interface_id: str | None = None,
        chat_interface: ChatInterface | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        request_confirmation_callback: (
            Callable[
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                [
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                    dict[str, Any],
                    float,
                    "ToolExecutionContext",
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
                # Note: Repository now returns typed LLMMessage objects without database metadata fields
                # like thread_root_id and internal_id. Cannot determine thread root from replied-to message.
                # Thread root will be set to the current saved message's internal_id if not already set.
                logger.info(
                    f"Received reply to interface message {replied_to_interface_id}. "
                    "Thread root ID will be determined from saved message."
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

            # Generate a temporary interface_message_id if not provided
            # This ensures the just-saved message can be filtered out of history
            actual_interface_message_id = trigger_interface_message_id
            if actual_interface_message_id is None:
                actual_interface_message_id = f"temp_{turn_id}"

            # Save user message in its own transaction to avoid long-running transactions
            async with get_db_context(engine=db_context.engine) as user_msg_db:
                saved_user_msg_record = await user_msg_db.message_history.add(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=actual_interface_message_id,
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
                    user_id=user_id,  # Pass user_id
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

            # Note: Repository now returns typed LLMMessage objects without database metadata fields
            # like interface_message_id. Cannot filter by interface_message_id anymore.
            initial_messages_for_llm = await self._format_history_for_llm(
                raw_history_messages
            )

            # Handle reply thread context
            if replied_to_interface_id and thread_root_id_for_turn:
                try:
                    full_thread_messages_db = (
                        await db_context.message_history.get_by_thread_id(
                            thread_root_id=thread_root_id_for_turn,
                        )
                    )
                    # Note: Repository now returns typed LLMMessage objects without database metadata fields
                    # Cannot filter by interface_message_id anymore. Using all thread messages as-is.
                    initial_messages_for_llm = await self._format_history_for_llm(
                        full_thread_messages_db
                    )
                except Exception as thread_fetch_err:
                    logger.error(f"Error fetching thread history: {thread_fetch_err}")

            messages_for_llm = initial_messages_for_llm

            # Prune leading invalid messages
            while messages_for_llm:
                first_msg = messages_for_llm[0]
                # Check if this is a ToolMessage or AssistantMessage with tool calls
                is_tool_msg = isinstance(first_msg, ToolMessage)
                is_assistant_with_tools = (
                    isinstance(first_msg, AssistantMessage) and first_msg.tool_calls
                )
                if is_tool_msg or is_assistant_with_tools:
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
                current_time_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

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
                messages_for_llm.insert(0, SystemMessage(content=final_system_prompt))

            # Process attachment content parts from delegation
            (
                processed_trigger_parts,
                attachment_injection_messages,
            ) = await self._process_attachment_content_parts(
                db_context, conversation_id, trigger_content_parts
            )

            # Attachment injection messages are already typed LLMMessage objects from the LLM client
            # Just append them directly to messages_for_llm
            messages_for_llm.extend(attachment_injection_messages)

            # Convert attachment URLs to data URIs before sending to LLM
            converted_trigger_parts = await self._convert_attachment_urls_to_data_uris(
                processed_trigger_parts
            )

            # Add inline attachment metadata if there are trigger attachments
            if trigger_attachments and len(trigger_attachments) > 0:
                attachment_metadata_lines = self._generate_attachment_metadata_lines(
                    trigger_attachments
                )

                # Add metadata to text content parts
                if attachment_metadata_lines:
                    metadata_text = "\n".join(attachment_metadata_lines)

                    # Find or create text content part
                    text_part_found = False
                    for part in converted_trigger_parts:
                        if part.get("type") == "text":
                            # Append metadata to existing text
                            part["text"] = part["text"] + "\n" + metadata_text  # type: ignore[typeddict-item]  # Runtime dict modification
                            text_part_found = True
                            break

                    # If no text part exists, create one with just metadata
                    if not text_part_found:
                        converted_trigger_parts.insert(  # type: ignore[arg-type]  # Runtime dict matches TypedDict structure
                            0,
                            {
                                "type": "text",
                                "text": metadata_text,
                            },
                        )

            # NOTE: We do NOT add the current user trigger message here because it was already
            # saved to the database and will be included in the history fetched earlier.
            # Adding it again would create a duplicate. The comment near line 1669-1672 in
            # handle_chat_interaction confirms the just-saved user message is included in history.

            # Messages are already typed (list[LLMMessage])
            typed_messages_for_llm = messages_for_llm

            # --- 3. Stream LLM Processing ---
            async for event, message_dict in self.process_message_stream(
                db_context=db_context,
                messages=typed_messages_for_llm,
                interface_type=interface_type,
                conversation_id=conversation_id,
                user_name=user_name,
                turn_id=turn_id,
                chat_interface=chat_interface,
                chat_interfaces=chat_interfaces,
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
                    msg_to_save["user_id"] = user_id

                    # Remove fields that shouldn't be saved to database
                    msg_to_save.pop("_attachment", None)  # Remove raw attachment data

                    # Save each message in its own transaction to avoid PostgreSQL transaction issues
                    async with get_db_context(engine=db_context.engine) as msg_db:
                        await msg_db.message_history.add(**msg_to_save)

        except Exception as e:
            logger.error(f"Error in streaming chat interaction: {e}", exc_info=True)
            yield LLMStreamEvent(
                type="error", error=str(e), metadata={"error_id": str(uuid.uuid4())}
            )

    def _generate_attachment_metadata_lines(
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        attachments: list[dict[str, Any]],
    ) -> list[str]:
        """
        Generate attachment metadata lines for a list of attachments.

        Args:
            attachments: List of attachment dictionaries

        Returns:
            List of formatted attachment metadata strings
        """
        attachment_metadata_lines = []

        for attachment in attachments:
            attachment_id = attachment.get("attachment_id")
            attachment_type = attachment.get("type", "file")
            filename = attachment.get("filename", "unknown")
            mime_type = attachment.get("mime_type", "")

            if attachment_id:
                # Format attachment metadata line
                type_desc = attachment_type
                if mime_type:
                    type_desc = f"{attachment_type} ({mime_type})"

                attachment_metadata_lines.append(
                    f"[Attachment available: {attachment_id} ({type_desc}: {filename})]"
                )

        return attachment_metadata_lines

    def _get_file_extension_from_mime_type(self, mime_type: str) -> str:
        """
        Return the appropriate file extension (with leading dot) for a given MIME type.

        If the MIME type has an exact match in the internal mapping, the corresponding extension is returned.
        If there is no exact match, a generic extension is returned based on the main type (e.g., '.img' for images, '.audio' for audio).
        If the main type is unrecognized, '.bin' is returned as a default.

        All returned extensions include a leading dot (e.g., '.jpg', '.bin').
        """
        # Common MIME type to extension mappings
        mime_to_ext = {
            # Images
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "image/svg+xml": ".svg",
            # Documents
            "application/pdf": ".pdf",
            "application/msword": ".doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.ms-excel": ".xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-powerpoint": ".ppt",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            # Text files
            "text/plain": ".txt",
            "text/csv": ".csv",
            "text/html": ".html",
            "application/json": ".json",
            "application/xml": ".xml",
            # Audio
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/ogg": ".ogg",
            # Video
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/ogg": ".ogv",
        }

        # Try exact match first
        if mime_type in mime_to_ext:
            return mime_to_ext[mime_type]

        # Fallback to generic extension based on main type
        main_type = (
            mime_type.split("/", maxsplit=1)[0] if "/" in mime_type else "application"
        )
        fallback_extensions = {
            "image": ".img",
            "audio": ".audio",
            "video": ".video",
            "text": ".txt",
            "application": ".bin",
        }

        return fallback_extensions.get(main_type, ".bin")
