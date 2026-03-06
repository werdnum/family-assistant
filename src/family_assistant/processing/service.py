from __future__ import annotations

import logging
import re
import traceback
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    LLMMessage,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.utils.clock import Clock, SystemClock

from .attachments import AttachmentProcessor
from .context import ContextPreparer
from .llm_loop import LLMStreamingLoop
from .tool_execution import ToolExecutor
from .types import ChatInteractionResult, ProcessingServiceConfig
from .utils import (
    _user_friendly_error_message,
    generate_attachment_metadata_lines,
    inject_metadata_into_user_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AppConfig
    from family_assistant.context_providers import ContextProvider
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.tools import ToolExecutionContext, ToolsProvider

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ProcessingService:
    """
    Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls.
    """

    def __init__(
        self,
        llm_client: LLMInterface,
        tools_provider: ToolsProvider,
        service_config: ProcessingServiceConfig,
        context_providers: list[ContextProvider],
        server_url: str | None,
        app_config: AppConfig,
        clock: Clock | None = None,
        attachment_registry: AttachmentRegistry | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_sources: dict[str, Any] | None = None,
    ) -> None:
        self._llm_client = llm_client
        self.tools_provider = tools_provider
        self.service_config = service_config
        self.context_providers = context_providers
        self.server_url = server_url or "http://localhost:8000"
        self.app_config = app_config
        self.clock = clock if clock is not None else SystemClock()
        self._attachment_registry = attachment_registry
        self.processing_services_registry: dict[str, ProcessingService] | None = None
        self.home_assistant_client: HomeAssistantClientWrapper | None = None
        self.camera_backend: CameraBackend | None = None
        self.event_sources = event_sources

        # Compose helpers
        self.attachment_processor = AttachmentProcessor(
            attachment_registry, llm_client, app_config, self.clock
        )
        self.context_preparer = ContextPreparer(
            context_providers, service_config, self.clock
        )
        self.tool_executor = ToolExecutor(
            tools_provider,
            service_config,
            self.attachment_processor,
            attachment_registry,
            self.clock,
        )
        self.llm_loop = LLMStreamingLoop(
            llm_client,
            service_config,
            app_config,
            self.tool_executor,
            self.attachment_processor,
        )

    @property
    def llm_client(self) -> LLMInterface:
        return self._llm_client

    @llm_client.setter
    def llm_client(self, value: LLMInterface) -> None:
        self._llm_client = value
        if hasattr(self, "llm_loop"):
            self.llm_loop.llm_client = value
        if hasattr(self, "attachment_processor"):
            self.attachment_processor.llm_client = value

    @property
    def attachment_registry(self) -> AttachmentRegistry | None:
        return self._attachment_registry

    @attachment_registry.setter
    def attachment_registry(self, value: AttachmentRegistry | None) -> None:
        self._attachment_registry = value
        if hasattr(self, "attachment_processor"):
            self.attachment_processor.attachment_registry = value
        if hasattr(self, "tool_executor"):
            self.tool_executor.attachment_registry = value

    def set_processing_services_registry(
        self, registry: dict[str, ProcessingService]
    ) -> None:
        """Sets the registry of all processing services."""
        self.processing_services_registry = registry

    async def process_message(
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
    ) -> tuple[list[LLMMessage], MessageReasoningInfo | None, list[str] | None]:
        """
        Non-streaming version of process_message that uses the streaming generator internally.

        Returns:
            A tuple containing:
            - A list of all typed LLMMessage objects generated during this turn.
            - A dictionary containing reasoning/usage info from the final LLM call (or None).
            - A list of attachment IDs to send with the response (or None).
        """
        return await self.llm_loop.run(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
        )

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
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
    ) -> AsyncIterator[tuple[LLMStreamEvent, LLMMessage | None]]:
        """
        Streaming version of process_message that yields LLMStreamEvent objects as they are generated.

        Yields tuples of (event, message) where:
        - event: The LLMStreamEvent object
        - message: The typed LLMMessage to be saved to history (for assistant/tool messages)

        This generator handles the same logic as process_message but yields events incrementally.
        """
        async for item in self.llm_loop.run_stream(
            db_context=db_context,
            messages=messages,
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            turn_id=turn_id,
            chat_interface=chat_interface,
            user_id=user_id,
            chat_interfaces=chat_interfaces,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
        ):
            yield item

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
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
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
            user_id: User identifier
            replied_to_interface_id: ID of message being replied to
            chat_interface: Interface for sending messages
            chat_interfaces: All registered chat interfaces
            request_confirmation_callback: Callback for tool confirmations
            trigger_attachments: Attachments from the user
            subconversation_id: Subconversation identifier

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
            user_message_timestamp = self.clock.now()

            if replied_to_interface_id:
                replied_to_msg_row = (
                    await db_context.message_history.get_row_by_interface_id(
                        interface_type=interface_type,
                        interface_message_id=replied_to_interface_id,
                    )
                )
                if replied_to_msg_row:
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

            # Prepare user message content for history - store only text
            # Attachments are stored separately in the attachments column and
            # reconstructed into multimodal content when loading from history
            user_content_for_history = ""
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
                    user_content_for_history = "[Media Attached]"

            actual_interface_message_id = trigger_interface_message_id
            if actual_interface_message_id is None:
                actual_interface_message_id = f"temp_{turn_id}"

            saved_user_msg_record = await db_context.message_history.add_message(
                UserMessage(content=user_content_for_history),
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=actual_interface_message_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id_for_turn,
                timestamp=user_message_timestamp,
                attachments=trigger_attachments,
                processing_profile_id=self.service_config.id,
                subconversation_id=subconversation_id,
                user_id=user_id,
            )

            if saved_user_msg_record is not None and not thread_root_id_for_turn:
                thread_root_id_for_turn = saved_user_msg_record
                if thread_root_id_for_turn:
                    logger.info(
                        f"Established new thread_root_id: {thread_root_id_for_turn}"
                    )

            # --- 2. Prepare LLM Context (History, System Prompt) ---
            history_limit, history_max_age = self.context_preparer.get_history_limits(
                interface_type
            )

            try:
                raw_history_messages = await db_context.message_history.get_recent(
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    limit=history_limit,
                    max_age=history_max_age,
                    processing_profile_id=self.service_config.id,
                    subconversation_id=subconversation_id,
                    current_time=self.clock.now(),
                )
            except Exception as hist_err:
                logger.error(
                    f"Failed to get message history for {interface_type}:{conversation_id}: {hist_err}",
                    exc_info=True,
                )
                raw_history_messages = []

            logger.debug(f"Raw history messages fetched ({len(raw_history_messages)}).")

            initial_messages_for_llm = await self.context_preparer.format_history(
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
                    full_thread_messages = (
                        await db_context.message_history.get_by_thread_id(
                            thread_root_id=thread_root_id_for_turn,
                            processing_profile_id=None,
                            subconversation_id=subconversation_id,
                        )
                    )
                    initial_messages_for_llm = (
                        await self.context_preparer.format_history(full_thread_messages)
                    )
                    logger.info(
                        f"Using {len(initial_messages_for_llm)} messages from full thread history for LLM context."
                    )

                    thread_attachments_context = (
                        await self.attachment_processor.extract_conversation_context(
                            db_context,
                            conversation_id,
                            self.service_config.history_max_age_hours,
                            self.service_config.prompts,
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

            messages_for_llm = initial_messages_for_llm

            # Prune leading invalid messages
            pruned_count = 0
            while messages_for_llm:
                first_msg = messages_for_llm[0]
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
            system_prompt_template = self.service_config.prompts.get(
                "system_prompt",
                "You are a helpful assistant. Current time is {current_time}.",
            )
            current_time_str = (
                self.clock
                .now()
                .astimezone(self.service_config.timezone)
                .strftime("%Y-%m-%d %H:%M:%S %Z")
            )

            aggregated_other_context_str = (
                await self.context_preparer.aggregate_context()
            )

            # Add thread attachments context if available
            if thread_attachments_context:
                if aggregated_other_context_str:
                    aggregated_other_context_str += "\n\n" + thread_attachments_context
                else:
                    aggregated_other_context_str = thread_attachments_context

            format_args = {
                "user_name": user_name,
                "current_time": current_time_str,
                "aggregated_other_context": aggregated_other_context_str,
                "server_url": self.server_url,
                "profile_id": self.service_config.id,
            }

            class SafePromptFormatter(dict[str, str]):
                def __missing__(self, key: str) -> str:
                    logger.warning(
                        f"System prompt template used key '{{{key}}}' which was not found "
                        f"in the provided format arguments: {list(self.keys())}. "
                        f"Substituting with an empty string."
                    )
                    return ""

            # Pre-process template to handle JSON examples and other literal braces
            safe_template = system_prompt_template

            placeholder_pattern = r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}"
            placeholders = set(re.findall(placeholder_pattern, safe_template))

            temp_template = safe_template
            for i, placeholder in enumerate(placeholders):
                marker = f"__PLACEHOLDER_{i}__"
                temp_template = temp_template.replace(f"{{{placeholder}}}", marker)

            temp_template = temp_template.replace("{", "{{").replace("}", "}}")

            for i, placeholder in enumerate(placeholders):
                marker = f"__PLACEHOLDER_{i}__"
                temp_template = temp_template.replace(marker, f"{{{placeholder}}}")

            safe_template = temp_template

            try:
                final_system_prompt = safe_template.format_map(
                    SafePromptFormatter(format_args)
                ).strip()
            except ValueError as e:
                logger.error(
                    f"Failed to format system prompt template: {e}. Using template without substitution."
                )
                final_system_prompt = system_prompt_template.strip()

            final_system_prompt = self.context_preparer.prepend_profile_preamble(
                final_system_prompt
            )

            if final_system_prompt:
                messages_for_llm.insert(0, SystemMessage(content=final_system_prompt))

            # Process attachment content parts from delegation
            (
                processed_trigger_parts,
                attachment_injection_messages,
            ) = await self.attachment_processor.process_content_parts(
                db_context, conversation_id, trigger_content_parts
            )

            messages_for_llm.extend(attachment_injection_messages)

            # Convert attachment URLs to data URIs in messages from history
            typed_messages_for_llm = (
                await self.attachment_processor.convert_message_urls(messages_for_llm)
            )

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
                user_name=user_name,
                user_id=user_id,
                turn_id=turn_id,
                chat_interface=chat_interface,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,
            )
            final_reasoning_info = final_reasoning_info_from_process_msg

            # --- 4. Save Generated Turn Messages & Extract Final Reply ---
            final_text_reply = None
            final_assistant_message_internal_id = None

            if generated_turn_messages:
                for turn_msg in generated_turn_messages:
                    reasoning_info_for_msg = (
                        final_reasoning_info
                        if isinstance(turn_msg, AssistantMessage)
                        else None
                    )
                    saved_turn_msg_record = (
                        await db_context.message_history.add_message(
                            message=turn_msg,
                            interface_type=interface_type,
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            thread_root_id=thread_root_id_for_turn,
                            timestamp=self.clock.now(),
                            processing_profile_id=self.service_config.id,
                            subconversation_id=subconversation_id,
                            user_id=user_id,
                            reasoning_info=reasoning_info_for_msg,
                        )
                    )

                    if isinstance(turn_msg, AssistantMessage) and turn_msg.content:
                        final_text_reply = turn_msg.content
                        if saved_turn_msg_record is not None:
                            final_assistant_message_internal_id = saved_turn_msg_record
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

        except Exception as exc:
            logger.error(
                f"Error in handle_chat_interaction for conversation {conversation_id}, turn {turn_id}",
                exc_info=True,
            )
            processing_error_traceback = traceback.format_exc()

            error_message = _user_friendly_error_message(exc)

            error_message_internal_id: int | None = None
            try:
                error_message_record = await db_context.message_history.add_message(
                    AssistantMessage(content=error_message),
                    interface_type=interface_type,
                    conversation_id=conversation_id,
                    interface_message_id=None,
                    turn_id=turn_id,
                    thread_root_id=thread_root_id_for_turn,
                    timestamp=datetime.now(UTC),
                    subconversation_id=subconversation_id,
                )
                error_message_internal_id = (
                    error_message_record if error_message_record is not None else None
                )
            except Exception as error_save_err:
                logger.error(
                    f"Failed to save error message to history: {error_save_err}",
                    exc_info=True,
                )

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
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
        subconversation_id: str | None = None,
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
        span = tracer.start_span(
            "conversation.process",
            attributes={
                "conversation.interface": interface_type,
                "conversation.id": conversation_id,
                "conversation.user": user_name,
            },
        )
        if subconversation_id:
            span.set_attribute("conversation.subconversation_id", subconversation_id)
        logger.info(
            f"Starting streaming chat interaction. Turn ID: {turn_id}, "
            f"Interface: {interface_type}, Conversation: {conversation_id}, "
            f"User: {user_name}, Content parts: {len(trigger_content_parts)}"
        )

        for i, part in enumerate(trigger_content_parts):
            logger.info(
                f"Processing content part {i}: type={part.get('type')}, size={len(str(part))}"
            )

        try:
            with trace.use_span(span, end_on_exit=False):
                try:
                    # --- 1. Determine Thread Root ID & Save User Trigger Message ---
                    thread_root_id_for_turn: int | None = None
                    user_message_timestamp = self.clock.now()

                    if replied_to_interface_id:
                        logger.info(
                            f"Received reply to interface message {replied_to_interface_id}. "
                            "Thread root ID will be determined from saved message."
                        )

                    # Prepare user message content for history - store only text
                    user_content_for_history = ""
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
                            user_content_for_history = "[Media Attached]"

                    actual_interface_message_id = trigger_interface_message_id
                    if actual_interface_message_id is None:
                        actual_interface_message_id = f"temp_{turn_id}"

                    # Save user message
                    # On PostgreSQL, use a separate transaction so the message is committed and visible immediately
                    # to other requests (like UI polling) while the LLM continues processing.
                    # On SQLite, we avoid nested transactions due to connection sharing in StaticPool.
                    user_msg = UserMessage(content=user_content_for_history)
                    if db_context.engine.dialect.name == "postgresql":
                        async with get_db_context(
                            engine=db_context.engine,
                            message_notifier=db_context.message_notifier,
                        ) as user_msg_db:
                            saved_user_msg_record = (
                                await user_msg_db.message_history.add_message(
                                    user_msg,
                                    interface_type=interface_type,
                                    conversation_id=conversation_id,
                                    interface_message_id=actual_interface_message_id,
                                    turn_id=turn_id,
                                    thread_root_id=thread_root_id_for_turn,
                                    timestamp=user_message_timestamp,
                                    attachments=trigger_attachments,
                                    processing_profile_id=self.service_config.id,
                                    subconversation_id=subconversation_id,
                                    user_id=user_id,
                                )
                            )
                    else:
                        saved_user_msg_record = (
                            await db_context.message_history.add_message(
                                user_msg,
                                interface_type=interface_type,
                                conversation_id=conversation_id,
                                interface_message_id=actual_interface_message_id,
                                turn_id=turn_id,
                                thread_root_id=thread_root_id_for_turn,
                                timestamp=user_message_timestamp,
                                attachments=trigger_attachments,
                                processing_profile_id=self.service_config.id,
                                subconversation_id=subconversation_id,
                                user_id=user_id,
                            )
                        )

                    if (
                        saved_user_msg_record is not None
                        and not thread_root_id_for_turn
                    ):
                        thread_root_id_for_turn = saved_user_msg_record

                    # --- 2. Prepare LLM Context ---
                    history_limit, history_max_age = (
                        self.context_preparer.get_history_limits(interface_type)
                    )

                    try:
                        raw_history_messages = (
                            await db_context.message_history.get_recent(
                                interface_type=interface_type,
                                conversation_id=conversation_id,
                                limit=history_limit,
                                max_age=history_max_age,
                                processing_profile_id=self.service_config.id,
                                subconversation_id=subconversation_id,
                                current_time=self.clock.now(),
                            )
                        )
                    except Exception as hist_err:
                        logger.error(
                            f"Failed to get message history: {hist_err}",
                            exc_info=True,
                        )
                        raw_history_messages = []

                    initial_messages_for_llm = (
                        await self.context_preparer.format_history(raw_history_messages)
                    )

                    # Handle reply thread context
                    if replied_to_interface_id and thread_root_id_for_turn:
                        try:
                            full_thread_messages_db = (
                                await db_context.message_history.get_by_thread_id(
                                    thread_root_id=thread_root_id_for_turn,
                                    subconversation_id=subconversation_id,
                                )
                            )
                            initial_messages_for_llm = (
                                await self.context_preparer.format_history(
                                    full_thread_messages_db
                                )
                            )
                        except Exception as thread_fetch_err:
                            logger.error(
                                f"Error fetching thread history: {thread_fetch_err}"
                            )

                    messages_for_llm = initial_messages_for_llm

                    # Prune leading invalid messages
                    while messages_for_llm:
                        first_msg = messages_for_llm[0]
                        is_tool_msg = isinstance(first_msg, ToolMessage)
                        is_assistant_with_tools = (
                            isinstance(first_msg, AssistantMessage)
                            and first_msg.tool_calls
                        )
                        if is_tool_msg or is_assistant_with_tools:
                            messages_for_llm.pop(0)
                        else:
                            break

                    # Prepare System Prompt
                    system_prompt_template = self.service_config.prompts.get(
                        "system_prompt",
                        "You are a helpful assistant. Current time is {current_time}.",
                    )

                    current_time_str = (
                        self.clock
                        .now()
                        .astimezone(self.service_config.timezone)
                        .strftime("%Y-%m-%d %H:%M:%S %Z")
                    )

                    aggregated_other_context_str = (
                        await self.context_preparer.aggregate_context()
                    )

                    format_args = {
                        "user_name": user_name,
                        "current_time": current_time_str,
                        "aggregated_other_context": aggregated_other_context_str,
                        "server_url": self.server_url,
                        "profile_id": self.service_config.id,
                    }

                    try:
                        final_system_prompt = system_prompt_template.format(
                            **format_args
                        ).strip()
                    except Exception:
                        final_system_prompt = system_prompt_template.strip()

                    final_system_prompt = (
                        self.context_preparer.prepend_profile_preamble(
                            final_system_prompt
                        )
                    )

                    if final_system_prompt:
                        messages_for_llm.insert(
                            0, SystemMessage(content=final_system_prompt)
                        )

                    # Process attachment content parts from delegation
                    (
                        processed_trigger_parts,
                        attachment_injection_messages,
                    ) = await self.attachment_processor.process_content_parts(
                        db_context, conversation_id, trigger_content_parts
                    )

                    messages_for_llm.extend(attachment_injection_messages)

                    # Add inline attachment metadata to the last UserMessage if there are trigger attachments
                    if trigger_attachments and len(trigger_attachments) > 0:
                        attachment_metadata_lines = generate_attachment_metadata_lines(
                            trigger_attachments
                        )
                        if attachment_metadata_lines:
                            metadata_text = "\n".join(attachment_metadata_lines)
                            for i in range(len(messages_for_llm) - 1, -1, -1):
                                msg = messages_for_llm[i]
                                if isinstance(msg, UserMessage):
                                    inject_metadata_into_user_message(
                                        msg, metadata_text
                                    )
                                    break

                    # Convert attachment URLs to data URIs in messages from history
                    typed_messages_for_llm = (
                        await self.attachment_processor.convert_message_urls(
                            messages_for_llm
                        )
                    )

                    # --- 3. Stream LLM Processing ---
                    async for event, stream_msg in self.process_message_stream(
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
                        yield event

                        # Save messages as they're generated
                        if stream_msg is not None:
                            reasoning_info_for_stream = (
                                event.metadata.get("reasoning_info")
                                if isinstance(stream_msg, AssistantMessage)
                                and event.metadata
                                else None
                            )
                            if db_context.engine.dialect.name == "postgresql":
                                async with get_db_context(
                                    engine=db_context.engine,
                                    message_notifier=db_context.message_notifier,
                                ) as msg_db:
                                    await msg_db.message_history.add_message(
                                        message=stream_msg,
                                        interface_type=interface_type,
                                        conversation_id=conversation_id,
                                        turn_id=turn_id,
                                        thread_root_id=thread_root_id_for_turn,
                                        timestamp=self.clock.now(),
                                        processing_profile_id=self.service_config.id,
                                        subconversation_id=subconversation_id,
                                        user_id=user_id,
                                        reasoning_info=reasoning_info_for_stream,
                                    )
                            else:
                                await db_context.message_history.add_message(
                                    message=stream_msg,
                                    interface_type=interface_type,
                                    conversation_id=conversation_id,
                                    turn_id=turn_id,
                                    thread_root_id=thread_root_id_for_turn,
                                    timestamp=self.clock.now(),
                                    processing_profile_id=self.service_config.id,
                                    subconversation_id=subconversation_id,
                                    user_id=user_id,
                                    reasoning_info=reasoning_info_for_stream,
                                )

                except Exception as e:
                    span.set_status(StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    logger.error(
                        f"Error in streaming chat interaction: {e}", exc_info=True
                    )
                    error_message = _user_friendly_error_message(e)
                    yield LLMStreamEvent(
                        type="error",
                        error=error_message,
                        metadata={"error_id": str(uuid.uuid4())},
                    )
        finally:
            span.end()
