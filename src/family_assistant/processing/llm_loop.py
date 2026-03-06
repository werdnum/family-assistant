from __future__ import annotations

import asyncio
import json
import logging
import traceback
import uuid
from typing import TYPE_CHECKING, Any

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.base import ContextLengthError
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

from .utils import _map_stream_error_to_exception, prune_messages_for_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from family_assistant.config_models import AppConfig
    from family_assistant.interfaces import ChatInterface
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools import ToolExecutionContext

    from .attachments import AttachmentProcessor
    from .tool_execution import ToolExecutor
    from .types import ProcessingServiceConfig

logger = logging.getLogger(__name__)


class LLMStreamingLoop:
    """Core LLM interaction loop with retry logic."""

    def __init__(
        self,
        llm_client: LLMInterface,
        service_config: ProcessingServiceConfig,
        app_config: AppConfig,
        tool_executor: ToolExecutor,
        attachment_processor: AttachmentProcessor,
    ) -> None:
        self.llm_client = llm_client
        self.service_config = service_config
        self.app_config = app_config
        self.tool_executor = tool_executor
        self.attachment_processor = attachment_processor

    async def run(
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
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: Any = None,  # noqa: ANN401 - Circular import with ProcessingService
        home_assistant_client: Any = None,  # noqa: ANN401 - Optional runtime dependency
        camera_backend: Any = None,  # noqa: ANN401 - Optional runtime dependency
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_sources: dict[str, Any] | None = None,
        # ast-grep-ignore: no-dict-any - Legacy reasoning_info structure
    ) -> tuple[list[LLMMessage], dict[str, Any] | None, list[str] | None]:
        """
        Non-streaming version of process_message that uses the streaming generator internally.

        Returns:
            A tuple containing:
            - A list of all typed LLMMessage objects generated during this turn.
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
            - A list of attachment IDs to send with the response (or None).
        """
        turn_messages: list[LLMMessage] = []
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        final_reasoning_info: dict[str, Any] | None = None
        final_attachment_ids: list[str] | None = None

        async for event, message in self.run_stream(
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
            processing_service=processing_service,
            home_assistant_client=home_assistant_client,
            camera_backend=camera_backend,
            event_sources=event_sources,
        ):
            if message is not None:
                turn_messages.append(message)

            if event.metadata:
                if "reasoning_info" in event.metadata:
                    final_reasoning_info = event.metadata["reasoning_info"]
                if "attachment_ids" in event.metadata:
                    final_attachment_ids = event.metadata["attachment_ids"]

        return turn_messages, final_reasoning_info, final_attachment_ids

    async def run_stream(
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
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: Any = None,  # noqa: ANN401 - Circular import with ProcessingService
        home_assistant_client: Any = None,  # noqa: ANN401 - Optional runtime dependency
        camera_backend: Any = None,  # noqa: ANN401 - Optional runtime dependency
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_sources: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message) where:
        - event: The LLMStreamEvent object
        - message: The typed LLMMessage to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        final_content: str | None = None
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        final_reasoning_info: dict[str, Any] | None = None
        max_iterations = self.service_config.max_iterations
        current_iteration = 1
        pending_attachment_ids: list[
            str
        ] = []  # Track attachment IDs from attach_to_response calls
        original_system_content: str | None = None  # Store original system prompt

        # Get tool definitions
        all_tool_definitions = (
            await self.tool_executor.tools_provider.get_tool_definitions()
        )
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
            elif is_final_iteration and has_thought_signatures:
                # Add final iteration instruction as a user message rather than modifying
                # the system prompt. This approach works reliably regardless of whether
                # thought signatures are present.
                final_iteration_instruction = UserMessage(
                    content=(
                        "[SYSTEM: This is the final processing iteration. Tools are no longer available. "
                        "You MUST now provide your final response summarizing your findings and conclusions. "
                        "Do NOT output raw JSON or tool call arguments - provide a natural language response to the user.]"
                    )
                )
                messages.append(final_iteration_instruction)
                logger.info("Added final iteration instruction as user message")

            # On final iteration, don't offer any tools to ensure we get a response
            tools_to_offer = None if is_final_iteration else tools_for_llm
            tool_choice_mode = (
                "none" if is_final_iteration or not tools_to_offer else "auto"
            )

            # Stream from LLM (with one context-length retry and one empty-response retry)
            context_retry_attempted = False
            empty_response_retry_attempted = False
            while True:
                accumulated_content = []
                tool_calls_from_stream = []
                done_provider_metadata = None

                try:
                    async for event in self.llm_client.generate_response_stream(
                        messages=messages,
                        tools=tools_to_offer,
                        tool_choice=tool_choice_mode,
                    ):
                        # Yield content events as they come
                        if event.type == "content" and event.content:
                            accumulated_content.append(event.content)
                            yield (event, None)  # No message to save yet

                        # Collect tool calls
                        elif event.type == "tool_call" and event.tool_call:
                            tool_calls_from_stream.append(event.tool_call)
                            yield (event, None)  # No message to save yet

                        # Handle done event
                        elif event.type == "done":
                            final_reasoning_info = event.metadata
                            # Extract provider_metadata from done event if present
                            done_provider_metadata = (
                                event.metadata.get("provider_metadata")
                                if event.metadata
                                else None
                            )

                        # Handle errors -- map to typed exceptions when possible
                        elif event.type == "error":
                            logger.error(f"Stream error: {event.error}")
                            raise _map_stream_error_to_exception(event)

                    # Check for empty response (no content and no tool calls)
                    if not accumulated_content and not tool_calls_from_stream:
                        if not empty_response_retry_attempted:
                            logger.warning(
                                "LLM returned empty response (no content, no tool calls). "
                                "iteration=%d/%d, tools_offered=%d, tool_choice=%s, "
                                "num_messages=%d. Re-prompting.",
                                current_iteration,
                                max_iterations,
                                len(tools_to_offer) if tools_to_offer else 0,
                                tool_choice_mode,
                                len(messages),
                            )
                            empty_response_retry_attempted = True
                            continue
                        logger.warning(
                            "LLM returned empty response on retry. "
                            "iteration=%d/%d, tools_offered=%d, tool_choice=%s, "
                            "num_messages=%d. Proceeding with empty response.",
                            current_iteration,
                            max_iterations,
                            len(tools_to_offer) if tools_to_offer else 0,
                            tool_choice_mode,
                            len(messages),
                        )

                    break  # Success, exit while loop

                except ContextLengthError as e:
                    if (
                        context_retry_attempted
                        or accumulated_content
                        or tool_calls_from_stream
                    ):
                        raise
                    logger.warning(
                        f"Context length exceeded, pruning messages and retrying: {e}"
                    )
                    messages = prune_messages_for_context(messages)
                    context_retry_attempted = True
                    continue

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

            effective_tool_calls = tool_calls_from_stream or None

            # If the LLM returned nothing (e.g. after exhausted empty-response
            # retries), skip creating an AssistantMessage and yield done with
            # no message so callers see an empty turn.
            has_content = isinstance(final_content, str) and final_content.strip()
            if not has_content and not effective_tool_calls:
                yield (
                    LLMStreamEvent(type="done", metadata={}),
                    None,
                )
                return

            assistant_message_for_turn = AssistantMessage(
                content=final_content,
                tool_calls=effective_tool_calls,
                provider_metadata=serialized_provider_metadata,
            )

            # Yield a synthetic "done" event with the complete assistant message
            # Include attachment IDs if any were captured from attach_to_response calls
            # Automatically select attachments if too many accumulated
            if (
                len(pending_attachment_ids)
                > self.app_config.attachment_selection_threshold
            ):
                # Extract original user query from messages (most recent first)
                original_query = ""
                for msg in reversed(messages):
                    if isinstance(msg, UserMessage):
                        if isinstance(msg.content, str):
                            original_query = msg.content
                        elif isinstance(msg.content, list) and msg.content:
                            for part in msg.content:
                                if (
                                    isinstance(part, dict)
                                    and part.get("type") == "text"
                                ):
                                    original_query = part.get("text", "")
                                    break
                        if original_query:
                            break

                if original_query:
                    pending_attachment_ids = (
                        await self.attachment_processor.select_for_response(
                            pending_attachment_ids=pending_attachment_ids,
                            original_query=original_query,
                        )
                    )
                    logger.info(
                        f"Attachment selection reduced from auto-queued to {len(pending_attachment_ids)} based on query relevance"
                    )

            # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
            done_metadata: dict[str, Any] = {"message": assistant_message_for_turn}
            if serialized_reasoning_info:
                done_metadata["reasoning_info"] = serialized_reasoning_info
            if pending_attachment_ids:
                # Fetch full metadata for each attachment for web UI display
                attachment_details = []
                if self.attachment_processor.attachment_registry:
                    for att_id in pending_attachment_ids:
                        try:
                            metadata = await self.attachment_processor.attachment_registry.get_attachment_with_context(
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
                tool_calls=effective_tool_calls,
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
                    self.tool_executor.execute(
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
                        processing_service=processing_service,
                        home_assistant_client=home_assistant_client,
                        camera_backend=camera_backend,
                        event_sources=event_sources,
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
                    auto_attachment_ids = result.auto_attachment_ids or []

                    # Auto-queue tool result attachments
                    for auto_attachment_id in auto_attachment_ids:
                        if auto_attachment_id not in pending_attachment_ids:
                            pending_attachment_ids.append(auto_attachment_id)
                            logger.info(
                                f"Auto-queued tool attachment {auto_attachment_id} for display"
                            )

                    # Check if this is an attach_to_response tool call
                    if llm_message.name == "attach_to_response" and event.tool_result:
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

                    # Yield tool result event (llm_message for database storage)
                    yield (event, llm_message)

                    # Add to messages for LLM (llm_message with _attachment)
                    tool_response_messages_for_llm.append(llm_message)

                except Exception as e:
                    # This should not happen since we handle exceptions inside tool_executor.execute
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
                    error_tool_message = ToolMessage(
                        tool_call_id=f"error_{uuid.uuid4()}",
                        content=f"Unexpected error: {str(e)}",
                        error_traceback=traceback.format_exc(),
                        name="unknown",
                    )
                    yield (error_event, error_tool_message)
                    tool_response_messages_for_llm.append(error_tool_message)

            # Add tool responses to messages for next iteration
            messages.extend(tool_response_messages_for_llm)
            current_iteration += 1

        # Check if we hit max iterations
        if current_iteration > max_iterations:
            logger.warning(
                f"Reached maximum iterations ({max_iterations}) in streaming tool loop."
            )
