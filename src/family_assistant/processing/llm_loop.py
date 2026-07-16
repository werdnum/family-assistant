from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from family_assistant.llm import LLMInterface, LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.base import ContextLengthError
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    MessageReasoningInfo,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    TurnTaintState,
    merge_history_taint,
)
from family_assistant.tools import (
    collect_system_prompt_addition,
    get_tool_definitions_for_advertisement,
)

from .attachments import AttachmentSelectionError
from .utils import (
    _map_stream_error_to_exception,
    messages_have_thought_signatures,
    prune_messages_for_context,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AppConfig
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.security.taint import TaintSource
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import EventSourcesById, ToolDefinition

    from .attachments import AttachmentProcessor
    from .service import ProcessingService
    from .tool_execution import ToolExecutor
    from .types import (
        LLMStreamingLoopConfig,
        MidTurnInputProvider,
        MidTurnUserInput,
        RequestConfirmationCallback,
        ToolExecutionResult,
    )

logger = logging.getLogger(__name__)


def _extract_activations_from_result(
    tool_msg: ToolMessage,
) -> tuple[list[str], list[str]]:
    """Extract activation directives from a trusted skill-loading tool result.

    Skills loaded via ``get_note`` can declare tools and/or whole MCP servers
    in their frontmatter, and that frontmatter is propagated through the tool
    result as ``activate_tools`` and ``activate_mcp_servers`` keys. Parsing is
    restricted to ``get_note`` results so arbitrary tools cannot expand the
    active tool surface by emitting those keys in their output.

    Prefer reading the structured ``tool_result.data`` payload when it is
    available: ``tool_msg.content`` is the LLM-facing string, which the
    large-result handling path can rewrite with attachment hints, at which
    point it is no longer parseable JSON. Fall back to JSON-decoding the
    content only for tool results that never set ``tool_result``.

    Returns:
        ``(tool_names, mcp_server_ids)`` — either may be empty.
    """
    if tool_msg.name != "get_note":
        return [], []

    data: object = None
    if tool_msg.tool_result is not None and tool_msg.tool_result.data is not None:
        data = tool_msg.tool_result.data
    else:
        content = tool_msg.content
        if not isinstance(content, str) or (
            "activate_tools" not in content and "activate_mcp_servers" not in content
        ):
            return [], []
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            return [], []

    if not isinstance(data, dict):
        return [], []
    raw_tools = data.get("activate_tools")
    raw_servers = data.get("activate_mcp_servers")
    tool_names = (
        [n for n in raw_tools if isinstance(n, str)]
        if isinstance(raw_tools, list)
        else []
    )
    mcp_server_ids = (
        [s for s in raw_servers if isinstance(s, str)]
        if isinstance(raw_servers, list)
        else []
    )
    return tool_names, mcp_server_ids


class LLMStreamingLoop:
    """Core LLM interaction loop with retry logic."""

    def __init__(
        self,
        llm_client: LLMInterface,
        config: LLMStreamingLoopConfig,
        app_config: AppConfig,
        tool_executor: ToolExecutor,
        attachment_processor: AttachmentProcessor,
    ) -> None:
        self.llm_client = llm_client
        self.config = config
        self.app_config = app_config
        self.tool_executor = tool_executor
        self.attachment_processor = attachment_processor

    @staticmethod
    def _infer_attachment_type(mime_type: str | None) -> str:
        """Infer a display type for streamed attachment metadata."""
        if mime_type is None:
            return "file"
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type == "application/pdf":
            return "document"
        return "file"

    @staticmethod
    def _format_mid_turn_user_input(user_input: MidTurnUserInput) -> str:
        """Render a mid-turn user update as model-facing steering context."""
        source = user_input.user_name or "The user"
        return (
            "[MID-TURN USER UPDATE]\n"
            f"{source} sent this while you were already working. Re-evaluate the "
            "active plan, decide whether this changes the current task or adds "
            "context, and make the smallest necessary adjustment. Treat this as "
            "the latest user instruction for the current turn.\n\n"
            f"{user_input.content}"
        )

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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: ProcessingService | None = None,
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> tuple[list[LLMMessage], MessageReasoningInfo | None, list[str] | None]:
        """
        Non-streaming version of process_message that uses the streaming generator internally.

        Returns:
            A tuple containing:
            - A list of all typed LLMMessage objects generated during this turn.
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
            - A list of attachment IDs to send with the response (or None).
        """
        turn_messages: list[LLMMessage] = []
        final_reasoning_info: MessageReasoningInfo | None = None
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
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=processing_service,
            home_assistant_client=home_assistant_client,
            camera_backend=camera_backend,
            event_sources=event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: ProcessingService | None = None,
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
    ) -> AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message) where:
        - event: The LLMStreamEvent object
        - message: The typed LLMMessage to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        final_content: str | None = None
        final_reasoning_info: MessageReasoningInfo | None = None
        max_iterations = self.config.max_iterations
        current_iteration = 1
        pending_attachment_ids: list[
            str
        ] = []  # Track attachment IDs from attach_to_response calls
        original_system_content: str | None = None  # Store original system prompt

        can_confirm = request_confirmation_callback is not None

        # The on-demand view is long-lived per profile and shared across
        # concurrent turns, so all activation state is kept turn-local here.
        tools_provider = self.tool_executor.tools_provider
        on_demand_view = (
            processing_service.on_demand_view
            if processing_service is not None
            else None
        )
        activated_on_demand: frozenset[str] = frozenset()
        initial_taint_state = merge_history_taint(messages)
        for source in initial_taint_sources or ():
            initial_taint_state = initial_taint_state.add_source(source)
        taint_tracker = InMemoryTurnTaintTracker(initial_taint_state)

        async def refresh_on_demand_tools() -> tuple[list[ToolDefinition], str | None]:
            """Re-compute the tool list and system prompt addition for this turn.

            On-demand tool definitions are sourced directly from the on-demand
            view (when present) so the activate_tools meta-tool and the
            turn-local activation set are honored. The system prompt addition
            walks the provider chain for any ``SystemPromptContributingProvider``
            contributions and appends the on-demand catalog from the view.
            """
            if on_demand_view is not None:
                defs = await on_demand_view.get_tool_definitions(
                    can_confirm=can_confirm, activated=activated_on_demand
                )
            else:
                defs = await get_tool_definitions_for_advertisement(
                    tools_provider,
                    can_confirm=can_confirm,
                )
            additions: list[str] = []
            chain_addition = await collect_system_prompt_addition(
                tools_provider,
                can_confirm=can_confirm,
                activated=activated_on_demand,
            )
            if chain_addition:
                additions.append(chain_addition)
            if on_demand_view is not None:
                view_addition = await on_demand_view.get_system_prompt_addition(
                    can_confirm=can_confirm,
                    activated=activated_on_demand,
                )
                if view_addition:
                    additions.append(view_addition)
            addition = "\n\n".join(additions) if additions else None
            return defs, addition

        tools_for_llm, system_prompt_addition = await refresh_on_demand_tools()

        logger.debug(
            f"Total available tools for this interaction: {len(tools_for_llm)}"
        )

        # Tool call loop
        while current_iteration <= max_iterations:
            if (
                mid_turn_input_provider is not None
                and mid_turn_input_provider.should_interrupt()
            ):
                raise asyncio.CancelledError("Turn interrupted by user")

            is_final_iteration = current_iteration == max_iterations

            logger.debug(
                "Starting streaming LLM interaction loop iteration %d/%d%s",
                current_iteration,
                max_iterations,
                " (FINAL - will force response without tools)"
                if is_final_iteration
                else "",
            )

            # Check if conversation has thought signatures that must be preserved.
            # If so, we cannot modify the system prompt as it would invalidate signatures.
            has_thought_signatures = messages_have_thought_signatures(messages)

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

                # Build system prompt: original + provider additions + iteration suffix
                system_content = original_system_content
                if system_prompt_addition:
                    system_content += "\n\n" + system_prompt_addition
                system_content += iteration_suffix

                # Create new message with modified content (Pydantic models are immutable)
                messages[0] = SystemMessage(content=system_content)
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
                            if event.metadata and "reasoning_info" in event.metadata:
                                final_reasoning_info = event.metadata["reasoning_info"]
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
                    messages = prune_messages_for_context(
                        messages,
                        min_turns=self.config.context_pruning_min_turns,
                    )
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

            serialized_reasoning_info = final_reasoning_info

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
                taint_metadata=taint_tracker.snapshot().to_metadata(),
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
                    try:
                        pending_attachment_ids = (
                            await self.attachment_processor.select_for_response(
                                pending_attachment_ids=pending_attachment_ids,
                                original_query=original_query,
                            )
                        )
                    except AttachmentSelectionError as exc:
                        logger.warning(
                            "Attachment selection failed; applying deterministic ID-sorted cap to auto-queued attachments. error=%s",
                            exc,
                        )
                        pending_attachment_ids = sorted(pending_attachment_ids)[
                            : self.app_config.max_response_attachments
                        ]
                    logger.info(
                        "Final queued attachments count for response: %d",
                        len(pending_attachment_ids),
                    )

            done_metadata: StreamEventMetadata = {"message": assistant_message_for_turn}
            if serialized_reasoning_info:
                done_metadata["reasoning_info"] = serialized_reasoning_info
            if pending_attachment_ids:
                # Fetch full metadata for each attachment for web UI display
                attachment_details = []
                if self.attachment_processor.attachment_registry:
                    for att_id in pending_attachment_ids:
                        metadata = await self.attachment_processor.attachment_registry.get_attachment(
                            db_context, att_id
                        )
                        if metadata is None:
                            raise ValueError(
                                f"Missing metadata for pending attachment '{att_id}'"
                            )
                        attachment_details.append({
                            "id": att_id,
                            "type": self._infer_attachment_type(metadata.mime_type),
                            "name": metadata.description or "Attachment",
                            "content": f"/api/attachments/{att_id}",
                            "mime_type": metadata.mime_type,
                            "size": metadata.size,
                        })

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
                content=final_content,
                tool_calls=effective_tool_calls,
                provider_metadata=serialized_provider_metadata,
                taint_metadata=taint_tracker.snapshot().to_metadata(),
            )
            messages.append(llm_context_assistant_message)

            # Break if no tool calls
            if not tool_calls_from_stream:
                logger.info(
                    "LLM streaming response received with no further tool calls."
                )
                break

            # On final iteration, report unexecuted tool calls explicitly rather than
            # silently dropping them.
            if is_final_iteration:
                logger.warning(
                    "Final iteration (%d) reached but LLM returned %d tool call(s). "
                    "Emitting explicit non-executed tool results and ending loop.",
                    max_iterations,
                    len(tool_calls_from_stream),
                )
                for tool_call in tool_calls_from_stream:
                    non_executed_message = (
                        f"Error: Tool call '{tool_call.function.name}' was not executed "
                        f"because the maximum iteration limit ({max_iterations}) was reached."
                    )
                    tool_result_event = LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=tool_call.id,
                        tool_result=non_executed_message,
                        error="max_iterations_reached",
                    )
                    tool_result_message = ToolMessage(
                        tool_call_id=tool_call.id,
                        content=non_executed_message,
                        name=tool_call.function.name,
                        error_traceback="max_iterations_reached",
                        taint_metadata=taint_tracker.snapshot().to_metadata(),
                    )
                    yield (tool_result_event, tool_result_message)
                break

            # Handle activate_tools meta-tool calls before regular execution
            regular_tool_calls = tool_calls_from_stream
            if on_demand_view:
                activate_calls = [
                    tc
                    for tc in tool_calls_from_stream
                    if tc.function.name == "activate_tools"
                ]
                regular_tool_calls = [
                    tc
                    for tc in tool_calls_from_stream
                    if tc.function.name != "activate_tools"
                ]
                for activate_call in activate_calls:
                    raw_args = activate_call.function.arguments
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            parsed_args = {}
                    else:
                        parsed_args = raw_args
                    args = parsed_args if isinstance(parsed_args, dict) else {}
                    requested_names = args.get("tool_names")
                    requested_search = args.get("search")
                    requested_mcp = args.get("mcp_server_ids")
                    activation = await on_demand_view.activate_tools(
                        names=requested_names
                        if isinstance(requested_names, list)
                        else None,
                        search=requested_search
                        if isinstance(requested_search, str)
                        else None,
                        mcp_server_ids=requested_mcp
                        if isinstance(requested_mcp, list)
                        else None,
                        can_confirm=can_confirm,
                        activated=activated_on_demand,
                    )
                    if activation.newly_activated:
                        activated_on_demand |= activation.newly_activated
                        activated_names = sorted(activation.newly_activated)
                        result_text = f"Activated tools: {', '.join(activated_names)}. You can now use them."
                    else:
                        result_text = "No matching tools found. Check the on-demand catalog for available tool names."
                    # Refresh the local tool list and system prompt addition so
                    # the catalog/meta-tool reflect the new activation set.
                    (
                        tools_for_llm,
                        system_prompt_addition,
                    ) = await refresh_on_demand_tools()
                    # Emit result event and message
                    activate_event = LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=activate_call.id,
                        tool_result=result_text,
                    )
                    activate_message = ToolMessage(
                        tool_call_id=activate_call.id,
                        content=result_text,
                        name="activate_tools",
                        taint_metadata=taint_tracker.snapshot().to_metadata(),
                    )
                    yield (activate_event, activate_message)
                    messages.append(activate_message)

            # Execute tool calls in parallel
            tool_response_messages_for_llm = []
            pre_batch_taint_snapshot = taint_tracker.snapshot()

            async def _execute_tool_call(
                tool_call: ToolCallItem,
                taint_policy_snapshot: TurnTaintState = pre_batch_taint_snapshot,
            ) -> ToolExecutionResult:
                return await self.tool_executor.execute(
                    tool_call,
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    user_name=user_name,
                    user_id=user_id,
                    turn_id=turn_id,
                    db_context=db_context,
                    chat_interface=chat_interface,
                    chat_interfaces=chat_interfaces,
                    confirmation_ui_managers=confirmation_ui_managers,
                    request_confirmation_callback=request_confirmation_callback,
                    subconversation_id=subconversation_id,
                    processing_service=processing_service,
                    home_assistant_client=home_assistant_client,
                    camera_backend=camera_backend,
                    event_sources=event_sources,
                    taint_tracker=taint_tracker,
                    taint_policy_snapshot=taint_policy_snapshot,
                )

            tool_execution_tasks = [
                asyncio.create_task(_execute_tool_call(tool_call))
                for tool_call in regular_tool_calls
            ]

            try:
                # Process results as they complete. Unexpected exceptions from
                # ToolExecutor are treated as fatal and bubble to the caller.
                for completed_task in asyncio.as_completed(tool_execution_tasks):
                    result = await completed_task
                    event = result.stream_event
                    llm_message = result.llm_message
                    result.apply_attachment_updates(pending_attachment_ids)

                    # Yield tool result event (llm_message for database storage)
                    yield (event, llm_message)

                    # Add to messages for LLM (llm_message with _attachment)
                    tool_response_messages_for_llm.append(llm_message)
            finally:
                # Ensure unfinished tasks are cancelled if one task fails.
                for task in tool_execution_tasks:
                    if not task.done():
                        task.cancel()
                if tool_execution_tasks:
                    await asyncio.gather(*tool_execution_tasks, return_exceptions=True)

            # Auto-activate tools / MCP servers from skill results (e.g.,
            # get_note returning a skill with activate_tools or
            # activate_mcp_servers in its frontmatter).
            if on_demand_view:
                for tool_msg in tool_response_messages_for_llm:
                    auto_names, auto_mcp_servers = _extract_activations_from_result(
                        tool_msg
                    )
                    if not auto_names and not auto_mcp_servers:
                        continue
                    activation = await on_demand_view.activate_tools(
                        names=auto_names or None,
                        mcp_server_ids=auto_mcp_servers or None,
                        can_confirm=can_confirm,
                        activated=activated_on_demand,
                    )
                    if activation.newly_activated:
                        activated_on_demand |= activation.newly_activated
                        (
                            tools_for_llm,
                            system_prompt_addition,
                        ) = await refresh_on_demand_tools()
                        logger.info(
                            "Auto-activated tools from skill: %s",
                            sorted(activation.newly_activated),
                        )

            # Add tool responses to messages for next iteration
            messages.extend(tool_response_messages_for_llm)

            if mid_turn_input_provider is not None:
                pending_user_inputs = (
                    await mid_turn_input_provider.drain_pending_mid_turn_inputs()
                )
                for user_input in pending_user_inputs:
                    # The model sees the wrapped steering prompt (re-evaluate the
                    # plan, etc.) so it adapts mid-turn...
                    mid_turn_message = UserMessage(
                        content=self._format_mid_turn_user_input(user_input)
                    )
                    messages.append(mid_turn_message)
                    # ...but persist (and stream) only the raw user text, so a
                    # later history reload shows what the user actually typed,
                    # not the internal [MID-TURN USER UPDATE] boilerplate.
                    yield (
                        LLMStreamEvent(
                            type="user_input",
                            content=user_input.content,
                        ),
                        UserMessage(content=user_input.content),
                    )

            if (
                mid_turn_input_provider is not None
                and mid_turn_input_provider.should_interrupt()
            ):
                raise asyncio.CancelledError("Turn interrupted by user")

            current_iteration += 1

        # Check if we hit max iterations
        if current_iteration > max_iterations:
            logger.warning(
                f"Reached maximum iterations ({max_iterations}) in streaming tool loop."
            )
