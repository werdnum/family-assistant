from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from family_assistant.llm import LLMInterface, LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.base import ContextLengthError
from family_assistant.llm.call_context import CallAttribution, attributed_to_profile
from family_assistant.llm.deferred_tools import activated_tool_names
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    MessageReasoningInfo,
    SystemMessage,
    ToolMessage,
    UserMessage,
    is_turn_scaffolding,
)
from family_assistant.observability.metrics import TurnMetrics
from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_state_into_tracker,
    prompt_window_taint,
)
from family_assistant.tools import (
    TaintTrackingToolsProvider,
    ToolPolicyDeniedError,
    collect_system_prompt_addition,
    find_provider_by_type,
    get_tool_definitions_for_advertisement,
)
from family_assistant.tools.types import ToolCallBatch, ToolCallReviewTurnState

from .attachments import AttachmentSelectionError
from .protocol import TaintedSinkRefusedError
from .quiet_turn import (
    END_TURN_QUIETLY_TOOL_DEFINITION,
    END_TURN_QUIETLY_TOOL_NAME,
    QUIET_END_TOOL_RESULT,
    quiet_end_reason,
    quiet_end_record,
)
from .utils import (
    _map_stream_error_to_exception,
    prune_messages_for_context,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from family_assistant.config_models import AppConfig
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.model_selection import ResolvedModelSelection
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.memory.review_context import MemoryReviewContext
    from family_assistant.plugins.runtime import ProfilePlugins
    from family_assistant.security.taint import TurnTaintTracker
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.database import Database
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
        config: LLMStreamingLoopConfig,
        app_config: AppConfig,
        tool_executor: ToolExecutor,
        attachment_processor: AttachmentProcessor,
    ) -> None:
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
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        llm_client: LLMInterface,
        model_selection: ResolvedModelSelection,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: ProcessingService | None = None,
        plugins: ProfilePlugins | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        memory_review: MemoryReviewContext | None = None,
        allow_quiet_end: bool = False,
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
            llm_client=llm_client,
            model_selection=model_selection,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=processing_service,
            plugins=plugins,
            event_sources=event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
            taint_tracker=taint_tracker,
            tool_call_review_trigger=tool_call_review_trigger,
            memory_review=memory_review,
            allow_quiet_end=allow_quiet_end,
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
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        llm_client: LLMInterface,
        model_selection: ResolvedModelSelection,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: ProcessingService | None = None,
        plugins: ProfilePlugins | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        memory_review: MemoryReviewContext | None = None,
        allow_quiet_end: bool = False,
        completed_iterations: int = 0,
    ) -> AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]:
        """Run a turn, attributing its telemetry to this profile.

        Every path into the loop funnels through here -- ``run()`` consumes
        this same generator -- so entering the profile context and counting the
        turn once here covers both the streaming and the non-streaming caller.
        """
        turn = TurnMetrics(self.config.id)
        inner = self._run_stream(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            llm_client=llm_client,
            model_selection=model_selection,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=processing_service,
            plugins=plugins,
            event_sources=event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
            taint_tracker=taint_tracker,
            tool_call_review_trigger=tool_call_review_trigger,
            memory_review=memory_review,
            allow_quiet_end=allow_quiet_end,
            completed_iterations=completed_iterations,
        )
        try:
            attribution = CallAttribution(
                profile_id=self.config.id, model_selection=model_selection
            )
            async for item in attributed_to_profile(attribution, inner):
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            turn.finish("cancelled")
            raise
        except BaseException:
            turn.finish("error")
            raise
        else:
            turn.finish("success")
        finally:
            await inner.aclose()

    async def _run_stream(
        self,
        db_context: Database,
        messages: list[LLMMessage],
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        chat_interface: ChatInterface | None,
        llm_client: LLMInterface,
        model_selection: ResolvedModelSelection,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        # Runtime deps passed through to tool_executor
        processing_service: ProcessingService | None = None,
        plugins: ProfilePlugins | None = None,
        event_sources: EventSourcesById | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
        taint_tracker: TurnTaintTracker | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        memory_review: MemoryReviewContext | None = None,
        allow_quiet_end: bool = False,
        completed_iterations: int = 0,
        # AsyncGenerator rather than AsyncIterator: run_stream closes this
        # deterministically, and only the generator protocol offers aclose().
    ) -> AsyncGenerator[tuple[LLMStreamEvent, LLMMessage | None]]:
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
        # A resumed turn has already spent iterations on the rounds it replays;
        # it continues on the same budget rather than starting a fresh one,
        # though always with at least the final iteration left to answer in.
        current_iteration = min(1 + completed_iterations, max_iterations)
        pending_attachment_ids: list[
            str
        ] = []  # Track attachment IDs from attach_to_response calls
        can_confirm = request_confirmation_callback is not None

        # Activation is part of the conversation: each activating tool message
        # records what it activated, so the set starts from the history and grows
        # as this turn activates more. The view is long-lived and shared across
        # concurrent turns, so it holds none of this.
        tools_provider = self.tool_executor.tools_provider
        on_demand_view = (
            processing_service.on_demand_view
            if processing_service is not None
            else None
        )
        activated_on_demand: frozenset[str] = activated_tool_names(messages)
        initial_taint_state = prompt_window_taint(messages)
        for source in initial_taint_sources or ():
            initial_taint_state = initial_taint_state.add_source(source)
        if taint_tracker is None:
            taint_tracker = InMemoryTurnTaintTracker(initial_taint_state)
        else:
            merge_taint_state_into_tracker(taint_tracker, initial_taint_state)
        turn_taint_tracker = taint_tracker
        tool_call_review_state = ToolCallReviewTurnState()

        async def refuse_if_sink_denied() -> None:
            """Gate a sink-declaring profile on the turn's taint as it stands.

            The loop is the one place where the whole turn's taint is known --
            the prompt's own sources, the aggregated context's and the
            history's -- so this is where a profile that declares a sink is
            gated rather than at an entry point, where a trusted prompt
            carrying an email-derived attachment or tainted history would still
            read as trusted. It runs before every model call, not only the
            first: on such a profile the model *is* the sink, and a tool that
            reads the web or a mailbox mid-turn raises the tier of what the
            next call would carry into it.
            """
            if processing_service is None:
                return
            sink_class = processing_service.service_config.taint_sink_class
            if sink_class is None:
                return
            taint_provider = find_provider_by_type(
                tools_provider, TaintTrackingToolsProvider
            )
            if taint_provider is None:
                sink_refusal = processing_service.sink_refusal_reason(
                    turn_taint_tracker.snapshot()
                )
                if sink_refusal is not None:
                    raise TaintedSinkRefusedError(sink_refusal)
                return
            execution_context = self.tool_executor.build_execution_context(
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
                llm_client=llm_client,
                plugins=plugins,
                event_sources=event_sources,
                taint_tracker=turn_taint_tracker,
                taint_policy_snapshot=turn_taint_tracker.snapshot(),
                tool_call_review_state=tool_call_review_state,
                tool_call_review_messages=tuple(messages),
                tool_call_review_trigger=tool_call_review_trigger,
                memory_review=memory_review,
            )
            try:
                await taint_provider.authorize_taint_sink(
                    name=f"profile:{processing_service.service_config.id}",
                    sink_class=sink_class,
                    arguments={"profile_id": processing_service.service_config.id},
                    context=execution_context,
                    call_id=f"profile_sink:{current_iteration}",
                    taint_policy=processing_service.taint_policy,
                )
            except ToolPolicyDeniedError as exc:
                raise TaintedSinkRefusedError(str(exc)) from exc

        async def declared_tools_and_catalog() -> tuple[
            list[ToolDefinition], str | None
        ]:
            """The tool list and on-demand catalog, fixed for the conversation.

            Every tool is declared on every request, on-demand ones marked
            deferred, and the catalog lists every on-demand tool whether or not
            it has been activated. Neither depends on what is active, so
            activating a tool changes no earlier part of the prompt: it is an
            appended message, which each provider adapter renders.
            """
            if on_demand_view is not None:
                defs = await on_demand_view.get_declared_definitions(
                    can_confirm=can_confirm
                )
            else:
                defs = await get_tool_definitions_for_advertisement(
                    tools_provider,
                    can_confirm=can_confirm,
                )
            additions: list[str] = []
            chain_addition = await collect_system_prompt_addition(
                tools_provider, can_confirm=can_confirm
            )
            if chain_addition:
                additions.append(chain_addition)
            if on_demand_view is not None:
                view_addition = await on_demand_view.get_system_prompt_addition(
                    can_confirm=can_confirm
                )
                if view_addition:
                    additions.append(view_addition)
            addition = "\n\n".join(additions) if additions else None
            if allow_quiet_end:
                defs = [*defs, END_TURN_QUIETLY_TOOL_DEFINITION]
            return defs, addition

        async def record_activation(
            message: ToolMessage,
            *,
            names: list[str] | None = None,
            search: str | None = None,
            mcp_server_ids: list[str] | None = None,
        ) -> frozenset[str]:
            """Activate on-demand tools and record them on *message*, before it is saved."""
            nonlocal activated_on_demand
            if on_demand_view is None:
                return frozenset()
            activation = await on_demand_view.activate_tools(
                names=names,
                search=search,
                mcp_server_ids=mcp_server_ids,
                can_confirm=can_confirm,
                activated=activated_on_demand,
            )
            if activation.newly_activated:
                activated_on_demand |= activation.newly_activated
                message.activated_tools = sorted(activation.newly_activated)
            return activation.newly_activated

        async def build_done_metadata(
            assistant_message: AssistantMessage,
            reasoning_info: MessageReasoningInfo | None,
        ) -> StreamEventMetadata:
            """Build final event metadata, including any attachments queued so far."""
            nonlocal pending_attachment_ids
            if (
                len(pending_attachment_ids)
                > self.app_config.attachment_selection_threshold
            ):
                original_query = ""
                for msg in reversed(messages):
                    if is_turn_scaffolding(msg):
                        continue
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
                                acting_user_id=user_id,
                                llm_client=llm_client,
                            )
                        )
                    except AttachmentSelectionError as exc:
                        logger.warning(
                            "Attachment selection failed; applying deterministic "
                            "ID-sorted cap to auto-queued attachments. error=%s",
                            exc,
                        )
                        pending_attachment_ids = sorted(pending_attachment_ids)[
                            : self.app_config.max_response_attachments
                        ]
                    logger.info(
                        "Final queued attachments count for response: %d",
                        len(pending_attachment_ids),
                    )

            done_metadata: StreamEventMetadata = {"message": assistant_message}
            if reasoning_info:
                done_metadata["reasoning_info"] = reasoning_info
            if pending_attachment_ids:
                attachment_details = []
                if self.attachment_processor.attachment_registry:
                    for att_id in pending_attachment_ids:
                        metadata = await self.attachment_processor.attachment_registry.get_attachment(
                            db_context, att_id, acting_user_id=user_id
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
                    "Including %d attachment IDs and %d attachment details in done event",
                    len(pending_attachment_ids),
                    len(attachment_details),
                )
            return done_metadata

        tools_for_llm, system_prompt_addition = await declared_tools_and_catalog()
        # Applied once, before the first request: the catalog does not change
        # with activation, so the system prompt is identical on every request
        # and all of it belongs inside the cached block.
        if (
            system_prompt_addition
            and messages
            and isinstance(messages[0], SystemMessage)
        ):
            content = f"{messages[0].content}\n\n{system_prompt_addition}"
            messages[0] = messages[0].model_copy(
                update={"content": content, "stable_prefix_len": len(content)}
            )

        logger.debug(
            f"Total available tools for this interaction: {len(tools_for_llm)}"
        )

        # Tool call loop
        while current_iteration <= max_iterations:
            await refuse_if_sink_denied()
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

            if is_final_iteration:
                # Delivered as a trailing user message rather than a system-prompt
                # edit so the cached prefix survives the final iteration too.
                # Flagged as scaffolding so downstream scans for the user's actual
                # request skip it -- it is not something the user said.
                messages.append(
                    UserMessage(
                        content=(
                            "[SYSTEM: This is the final processing iteration. Tools are no longer available. "
                            "You MUST now provide your final response summarizing your findings and conclusions. "
                            "Do NOT output raw JSON or tool call arguments - provide a natural language response to the user."
                            + (
                                " If nothing needs the user's attention, you may instead call end_turn_quietly.]"
                                if allow_quiet_end
                                else "]"
                            )
                        ),
                        is_turn_scaffolding=True,
                    )
                )
                logger.info("Added final iteration instruction as user message")

            # On final iteration, don't offer any tools to ensure we get a response
            # A quiet end is a way of finishing, not more work, so it stays on
            # offer when the tool budget runs out: the last tool result may be
            # exactly what shows there is nothing to report.
            if is_final_iteration:
                tools_to_offer = (
                    [END_TURN_QUIETLY_TOOL_DEFINITION] if allow_quiet_end else None
                )
            else:
                tools_to_offer = tools_for_llm
            tool_choice_mode = "none" if not tools_to_offer else "auto"

            # Stream from LLM (with one context-length retry and one empty-response retry)
            context_retry_attempted = False
            empty_response_retry_attempted = False
            while True:
                accumulated_content = []
                tool_calls_from_stream = []
                done_provider_metadata = None
                done_external_read = None

                async def stream_events(
                    messages_for_attempt: list[LLMMessage],
                    tools_for_attempt: list[ToolDefinition] | None,
                    tool_choice_for_attempt: str,
                    content_for_attempt: list[str],
                    tool_calls_for_attempt: list[ToolCallItem],
                ) -> AsyncGenerator[LLMStreamEvent]:
                    nonlocal done_provider_metadata, final_reasoning_info
                    nonlocal done_external_read
                    async for event in llm_client.generate_response_stream(
                        messages=messages_for_attempt,
                        tools=tools_for_attempt,
                        tool_choice=tool_choice_for_attempt,
                    ):
                        # Yield content events as they come
                        if event.type == "content" and event.content:
                            content_for_attempt.append(event.content)
                            yield event

                        # Collect tool calls
                        elif event.type == "tool_call" and event.tool_call:
                            tool_calls_for_attempt.append(event.tool_call)
                            yield event

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
                            done_external_read = (
                                event.metadata.get("provider_external_read")
                                if event.metadata
                                else None
                            )

                        # Handle errors -- map to typed exceptions when possible
                        elif event.type == "error":
                            logger.error(f"Stream error: {event.error}")
                            raise _map_stream_error_to_exception(event)

                def should_retry_empty_response(
                    content_for_attempt: list[str],
                    tool_calls_for_attempt: list[ToolCallItem],
                    iteration: int,
                    offered_tools: list[ToolDefinition] | None,
                    choice_mode: str,
                    message_count: int,
                ) -> bool:
                    nonlocal empty_response_retry_attempted
                    if not content_for_attempt and not tool_calls_for_attempt:
                        if not empty_response_retry_attempted:
                            logger.warning(
                                "LLM returned empty response (no content, no tool calls). "
                                "iteration=%d/%d, tools_offered=%d, tool_choice=%s, "
                                "num_messages=%d. Re-prompting.",
                                iteration,
                                max_iterations,
                                len(offered_tools) if offered_tools else 0,
                                choice_mode,
                                message_count,
                            )
                            empty_response_retry_attempted = True
                            return True
                        logger.warning(
                            "LLM returned empty response on retry. "
                            "iteration=%d/%d, tools_offered=%d, tool_choice=%s, "
                            "num_messages=%d. Proceeding with empty response.",
                            iteration,
                            max_iterations,
                            len(offered_tools) if offered_tools else 0,
                            choice_mode,
                            message_count,
                        )
                    return False

                stream_iterator = stream_events(
                    messages,
                    tools_to_offer,
                    tool_choice_mode,
                    accumulated_content,
                    tool_calls_from_stream,
                )
                retry_empty_response = False
                try:
                    async for stream_event in stream_iterator:
                        yield (stream_event, None)
                    retry_empty_response = should_retry_empty_response(
                        accumulated_content,
                        tool_calls_from_stream,
                        current_iteration,
                        tools_to_offer,
                        tool_choice_mode,
                        len(messages),
                    )

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
                    # Prune without the synthetic scaffolding messages -- the
                    # turn-context block and the final-iteration instruction. The
                    # turn splitter starts a new turn at every UserMessage, so
                    # leaving them in costs real turns out of min_turns -- and at
                    # min_turns=1 the newest of them is the *only* turn kept,
                    # discarding the user's request and every accumulated tool
                    # result. They are re-appended in their original order, which
                    # keeps the final-iteration instruction last.
                    scaffolding = [msg for msg in messages if is_turn_scaffolding(msg)]
                    messages = prune_messages_for_context(
                        [msg for msg in messages if not is_turn_scaffolding(msg)],
                        min_turns=self.config.context_pruning_min_turns,
                    )
                    messages.extend(scaffolding)
                    context_retry_attempted = True
                    continue

                except Exception as e:
                    logger.exception(f"Error in LLM streaming: {e}")
                    raise
                finally:
                    await stream_iterator.aclose()

                if retry_empty_response:
                    continue
                break  # Success, exit while loop

            if done_external_read is not None:
                taint_tracker.add_source(
                    TaintSource(
                        source_type=TaintSourceType.TOOL_OUTPUT,
                        source_id=done_external_read["source_id"],
                        tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                        labels=frozenset(),
                        reason=done_external_read["reason"],
                    )
                )

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
            #
            # Any provider reasoning state from this iteration is deliberately
            # discarded here. An AssistantMessage requires content or tool calls,
            # so there is no row to hang it on, and a reasoning-only turn is one
            # the model did not finish -- replaying its reasoning into the next
            # attempt would carry the dead end forward rather than help. The
            # retry starts from the last complete turn instead.
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
                # This iteration's call, not the turn's last one: each pass
                # round the loop is its own provider call, and the row saved
                # for this message is where that call's cost and timing live.
                reasoning_info=serialized_reasoning_info,
            )

            # Yield a synthetic "done" event with the complete assistant message.
            done_metadata = await build_done_metadata(
                assistant_message_for_turn, serialized_reasoning_info
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
            # silently dropping them. A quiet end is the one call still honoured
            # there, since it is the only tool offered on that pass.
            only_quiet_end = allow_quiet_end and all(
                tc.function.name == END_TURN_QUIETLY_TOOL_NAME
                for tc in tool_calls_from_stream
            )
            if is_final_iteration and not only_quiet_end:
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
                    activate_message = ToolMessage(
                        tool_call_id=activate_call.id,
                        content="",
                        name="activate_tools",
                        taint_metadata=taint_tracker.snapshot().to_metadata(),
                    )
                    names_requested = (
                        [n for n in requested_names if isinstance(n, str)]
                        if isinstance(requested_names, list)
                        else None
                    )
                    already_active = sorted(
                        set(names_requested or ()) & activated_on_demand
                    )
                    newly_activated = await record_activation(
                        activate_message,
                        names=names_requested,
                        search=requested_search
                        if isinstance(requested_search, str)
                        else None,
                        mcp_server_ids=requested_mcp
                        if isinstance(requested_mcp, list)
                        else None,
                    )
                    result_parts: list[str] = []
                    if newly_activated:
                        result_parts.append(
                            f"Activated tools: {', '.join(sorted(newly_activated))}. "
                            "You can now use them for the rest of the conversation."
                        )
                    if already_active:
                        result_parts.append(
                            f"Already active: {', '.join(already_active)}."
                        )
                    activate_message.content = (
                        " ".join(result_parts)
                        or "No matching tools found. Check the on-demand catalog for available tool names."
                    )
                    activate_event = LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=activate_call.id,
                        tool_result=activate_message.content,
                    )
                    yield (activate_event, activate_message)
                    messages.append(activate_message)

            # end_turn_quietly is the loop's own tool, so it is answered here
            # and never reaches the executor. It is only honoured on a turn it
            # was advertised on; anywhere else it runs as an unknown tool.
            quiet_end_calls: list[ToolCallItem] = []
            if allow_quiet_end:
                quiet_end_calls = [
                    tc
                    for tc in regular_tool_calls
                    if tc.function.name == END_TURN_QUIETLY_TOOL_NAME
                ]
                regular_tool_calls = [
                    tc
                    for tc in regular_tool_calls
                    if tc.function.name != END_TURN_QUIETLY_TOOL_NAME
                ]

            # Execute tool calls in parallel
            tool_response_messages_for_llm = []
            pre_batch_taint_snapshot = taint_tracker.snapshot()

            # The calls run concurrently, but the model issued them in an order
            # and tools that share one resource (the browser) have to respect
            # it. The batch is what lets a call see its own place in that order.
            tool_call_batch = ToolCallBatch([
                (tool_call.id, tool_call.function.name)
                for tool_call in regular_tool_calls
            ])

            async def _execute_tool_call(
                tool_call: ToolCallItem,
                taint_policy_snapshot: TurnTaintState = pre_batch_taint_snapshot,
                review_messages: tuple[LLMMessage, ...] = tuple(messages),
                batch: ToolCallBatch = tool_call_batch,
            ) -> ToolExecutionResult:
                return await self.tool_executor.execute(
                    tool_call,
                    tool_call_batch=batch,
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
                    llm_client=llm_client,
                    plugins=plugins,
                    event_sources=event_sources,
                    taint_tracker=taint_tracker,
                    taint_policy_snapshot=taint_policy_snapshot,
                    tool_call_review_state=tool_call_review_state,
                    tool_call_review_messages=review_messages,
                    tool_call_review_trigger=tool_call_review_trigger,
                    memory_review=memory_review,
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

                    # A skill loaded with get_note can declare tools or whole MCP
                    # servers in its frontmatter. They are activated here, before
                    # the result is saved, so the activation is recorded on it.
                    if on_demand_view is not None and isinstance(
                        llm_message, ToolMessage
                    ):
                        auto_names, auto_mcp_servers = _extract_activations_from_result(
                            llm_message
                        )
                        if auto_names or auto_mcp_servers:
                            newly = await record_activation(
                                llm_message,
                                names=auto_names or None,
                                mcp_server_ids=auto_mcp_servers or None,
                            )
                            if newly:
                                logger.info(
                                    "Auto-activated tools from skill: %s",
                                    sorted(newly),
                                )

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

            # Add tool responses to messages for next iteration
            messages.extend(tool_response_messages_for_llm)

            termination_message = (
                tool_call_review_state.terminal_denial_escalation_message
            )
            if termination_message is not None:
                # Every tool call in the assistant's batch has a persisted result
                # before this deterministic assistant row is emitted. Ending here
                # preserves provider tool/result protocol validity and ensures the
                # denied turn cannot reach another model invocation.
                terminal_assistant_message = AssistantMessage(
                    content=termination_message,
                    taint_metadata=taint_tracker.snapshot().to_metadata(),
                )
                yield (
                    LLMStreamEvent(type="content", content=termination_message),
                    None,
                )
                terminal_done_metadata = await build_done_metadata(
                    terminal_assistant_message, None
                )
                yield (
                    LLMStreamEvent(type="done", metadata=terminal_done_metadata),
                    terminal_assistant_message,
                )
                return

            if quiet_end_calls:
                # Every call in the batch has its result before the closing
                # row, as with the denial above, so the history stays valid
                # provider protocol when the next turn replays it.
                for quiet_call in quiet_end_calls:
                    quiet_result_message = ToolMessage(
                        tool_call_id=quiet_call.id,
                        content=QUIET_END_TOOL_RESULT,
                        name=END_TURN_QUIETLY_TOOL_NAME,
                        taint_metadata=taint_tracker.snapshot().to_metadata(),
                    )
                    yield (
                        LLMStreamEvent(
                            type="tool_result",
                            tool_call_id=quiet_call.id,
                            tool_result=QUIET_END_TOOL_RESULT,
                        ),
                        quiet_result_message,
                    )
                quiet_reason = quiet_end_reason(quiet_end_calls[0])
                logger.info("Turn %s ended quietly: %s", turn_id, quiet_reason)
                quiet_assistant_message = AssistantMessage(
                    content=quiet_end_record(quiet_reason),
                    taint_metadata=taint_tracker.snapshot().to_metadata(),
                    ended_quietly=True,
                )
                yield (
                    LLMStreamEvent(
                        type="done",
                        metadata=await build_done_metadata(
                            quiet_assistant_message, None
                        ),
                    ),
                    quiet_assistant_message,
                )
                return

            if mid_turn_input_provider is not None:
                pending_user_inputs = (
                    await mid_turn_input_provider.drain_pending_mid_turn_inputs()
                )
                for user_input in pending_user_inputs:
                    # Mid-turn input providers are authenticated interface paths,
                    # just like the user message that opened the turn.  Give both
                    # the model-facing steering wrapper and the raw persisted row
                    # explicit trusted-user provenance so the action reviewer can
                    # render the updated intent and compute destination-echo signals.
                    mid_turn_taint_metadata = TurnTaintState.empty().to_metadata()
                    # The model sees the wrapped steering prompt (re-evaluate the
                    # plan, etc.) so it adapts mid-turn...
                    mid_turn_message = UserMessage(
                        content=self._format_mid_turn_user_input(user_input),
                        taint_metadata=mid_turn_taint_metadata,
                    )
                    messages.append(mid_turn_message)
                    # ...but persist (and stream) only the raw user text, so a
                    # later history reload shows what the user actually typed,
                    # not the internal [MID-TURN USER UPDATE] boilerplate.
                    yield (
                        LLMStreamEvent(
                            type="user_input",
                            content=user_input.content,
                            input_id=user_input.interface_message_id,
                        ),
                        UserMessage(
                            content=user_input.content,
                            taint_metadata=mid_turn_taint_metadata,
                            authorship_taint_metadata=mid_turn_taint_metadata,
                        ),
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
