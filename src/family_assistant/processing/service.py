from __future__ import annotations

import json
import logging
import re
import traceback
import uuid
from string import Formatter
from typing import TYPE_CHECKING, Literal

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from family_assistant.llm import LLMInterface, LLMStreamEvent
from family_assistant.llm.messages import (
    AssistantMessage,
    ContentPartDict,
    ErrorMessage,
    LLMMessage,
    MessageAttachmentMetadata,
    MessageReasoningInfo,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.utils.clock import Clock, SystemClock
from family_assistant.utils.text_normalization import normalize_latex_to_unicode

from .attachments import AttachmentProcessor
from .context import ContextPreparer
from .llm_loop import LLMStreamingLoop
from .tool_execution import ToolExecutor
from .types import (
    ChatInteractionResult,
    ProcessingServiceConfig,
    RequestConfirmationCallback,
)
from .utils import (
    _user_friendly_error_message,
    format_attachment_metadata_block,
    inject_metadata_into_user_message,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from datetime import datetime

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.config_models import AppConfig
    from family_assistant.context_providers import ContextProvider
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.processing.protocol import DelegatableService
    from family_assistant.processing.types import MidTurnInputProvider
    from family_assistant.security.taint import TaintSource
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools import OnDemandToolsView, ToolsProvider
    from family_assistant.tools.types import EventSourcesById

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ProcessingService:
    """
    Encapsulates the logic for preparing context, processing messages,
    interacting with the LLM, and handling tool calls.
    """

    _USE_ISOLATED_HISTORY_WRITES = True
    kind: Literal["local"] = "local"

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
        event_sources: EventSourcesById | None = None,
        processing_services_registry: Mapping[str, DelegatableService] | None = None,
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        on_demand_view: OnDemandToolsView | None = None,
    ) -> None:
        self._llm_client = llm_client
        self.tools_provider = tools_provider
        self.on_demand_view = on_demand_view
        self.service_config = service_config
        self.context_providers = context_providers
        self.server_url = server_url or "http://localhost:8000"
        self.app_config = app_config
        self.clock = clock if clock is not None else SystemClock()
        self._attachment_registry = attachment_registry
        self.processing_services_registry = processing_services_registry
        self.home_assistant_client = home_assistant_client
        self.camera_backend = camera_backend
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
        self.llm_loop.llm_client = value
        self.attachment_processor.llm_client = value

    @property
    def attachment_registry(self) -> AttachmentRegistry | None:
        return self._attachment_registry

    @attachment_registry.setter
    def attachment_registry(self, value: AttachmentRegistry | None) -> None:
        self._attachment_registry = value
        self.attachment_processor.attachment_registry = value
        self.tool_executor.attachment_registry = value

    async def _resolve_thread_root_id(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        replied_to_interface_id: str | None,
    ) -> int | None:
        """Resolve the thread root ID when the user replied to an existing message."""
        if replied_to_interface_id is None:
            return None

        replied_to_msg_row = await db_context.message_history.get_row_by_interface_id(
            interface_type=interface_type,
            interface_message_id=replied_to_interface_id,
        )
        if replied_to_msg_row:
            thread_root_id = replied_to_msg_row.get(
                "thread_root_id"
            ) or replied_to_msg_row.get("internal_id")
            logger.info(
                "Received reply to interface message %s. Thread root ID: %s",
                replied_to_interface_id,
                thread_root_id,
            )
            return thread_root_id

        logger.warning(
            "Replied-to interface message %s not found. Creating new thread.",
            replied_to_interface_id,
        )
        return None

    def _extract_user_content_for_history(
        self, trigger_content_parts: list[ContentPartDict]
    ) -> str:
        """Extract a concise text value for message-history storage."""
        if not trigger_content_parts:
            return ""

        first_text_part = next(
            (
                part.get("text")
                for part in trigger_content_parts
                if part.get("type") == "text"
            ),
            None,
        )
        if first_text_part:
            return str(first_text_part)
        if trigger_content_parts[0].get("type") == "image_url":
            return "[Media Attached]"
        return ""

    async def _build_initial_messages_for_llm(
        self,
        db_context: DatabaseContext,
        interface_type: str,
        conversation_id: str,
        replied_to_interface_id: str | None,
        thread_root_id_for_turn: int | None,
        subconversation_id: str | None,
    ) -> tuple[list[LLMMessage], str]:
        """Load history and optional full-thread context for LLM processing."""
        history_limit, history_max_age = self.context_preparer.get_history_limits(
            interface_type
        )
        raw_history_messages = await db_context.message_history.get_recent(
            interface_type=interface_type,
            conversation_id=conversation_id,
            limit=history_limit,
            max_age=history_max_age,
            processing_profile_id=self.service_config.id,
            subconversation_id=subconversation_id,
            current_time=self.clock.now(),
        )
        logger.debug("Raw history messages fetched (%d).", len(raw_history_messages))

        initial_messages_for_llm = await self.context_preparer.format_history(
            raw_history_messages
        )
        logger.debug(
            "Initial messages for LLM after formatting history (%d).",
            len(initial_messages_for_llm),
        )

        thread_attachments_context = ""
        if replied_to_interface_id and thread_root_id_for_turn:
            logger.info(
                "Fetching full thread history for root ID %s due to reply.",
                thread_root_id_for_turn,
            )
            full_thread_messages = await db_context.message_history.get_by_thread_id(
                thread_root_id=thread_root_id_for_turn,
                processing_profile_id=None,
                subconversation_id=subconversation_id,
            )
            initial_messages_for_llm = await self.context_preparer.format_history(
                full_thread_messages
            )
            logger.info(
                "Using %d messages from full thread history for LLM context.",
                len(initial_messages_for_llm),
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

        return initial_messages_for_llm, thread_attachments_context

    async def _append_missing_pinned_history_messages(
        self,
        db_context: DatabaseContext,
        messages_for_llm: list[LLMMessage],
        pinned_history_message_ids: list[int] | None,
    ) -> None:
        """Append required rows that history limits may have excluded."""
        if not pinned_history_message_ids:
            return

        pinned_messages = await db_context.message_history.get_by_internal_ids(
            tuple(pinned_history_message_ids)
        )
        pinned_messages_for_llm = await self.context_preparer.format_history(
            pinned_messages
        )
        existing_keys = {
            (message.role, self._message_content_key(message))
            for message in messages_for_llm
        }
        for pinned_message in pinned_messages_for_llm:
            pinned_key = (
                pinned_message.role,
                self._message_content_key(pinned_message),
            )
            if pinned_key not in existing_keys:
                messages_for_llm.append(pinned_message)
                existing_keys.add(pinned_key)

    @staticmethod
    def _message_content_key(message: LLMMessage) -> str:
        """Return a stable content key for duplicate detection."""
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, default=str, sort_keys=True)

    @staticmethod
    def _prune_leading_invalid_messages(messages_for_llm: list[LLMMessage]) -> int:
        """Remove leading tool messages/tool-calling assistant messages."""
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
        return pruned_count

    def render_available_service_profiles(self) -> str:
        """Render the catalog of delegatable service profiles.

        Sourced from the live processing-services registry so it reflects the
        operator's configured profiles (including custom ones). Returns an empty
        string when no registry is available.
        """
        registry = self.processing_services_registry
        if not registry:
            return ""
        lines = [
            f"- ID: {profile_id}, Description: "
            f"{service.service_config.description or 'No description available.'}"
            for profile_id, service in registry.items()
        ]
        return "\n".join(lines)

    async def delegation_catalog_addition(self) -> str:
        """Return a system-prompt section listing delegation targets, or "".

        This used to live in the delegate_to_service schema. It is now appended
        to the rendered system prompt independently of the per-profile prompt
        template, so every profile that actually advertises delegate_to_service
        gets the catalog (some shipped profiles override system_prompt and would
        otherwise miss it). Returns "" when the profile cannot delegate or the
        registry is empty.
        """
        catalog = self.render_available_service_profiles()
        if not catalog:
            return ""
        from family_assistant.tools.infrastructure import (  # noqa: PLC0415
            get_tool_definitions_for_advertisement,
        )

        advertised = await get_tool_definitions_for_advertisement(
            self.tools_provider, can_confirm=True
        )
        advertised_names = {
            definition.get("function", {}).get("name") for definition in advertised
        }
        if "delegate_to_service" not in advertised_names:
            return ""
        return (
            "## Service profiles you can delegate to\n"
            "Use `delegate_to_service` with one of these IDs as `target_service_id`:\n"
            f"{catalog}"
        )

    def _render_system_prompt(
        self, user_name: str, aggregated_other_context_str: str
    ) -> str:
        """Render a system prompt with strict placeholder validation."""
        system_prompt_template = self.service_config.prompts.get(
            "system_prompt",
            "You are a helpful assistant. Current time is {current_time}.",
        )
        system_prompt_docs = self.service_config.prompts.get("system_prompt_docs", "")
        current_time_str = (
            self.clock
            .now()
            .astimezone(self.service_config.timezone)
            .strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        format_args = {
            "user_name": user_name,
            "current_time": current_time_str,
            "aggregated_other_context": aggregated_other_context_str,
            "server_url": self.server_url,
            "profile_id": self.service_config.id,
        }

        formatter = Formatter()
        escaped_template_parts: list[str] = []
        unknown_placeholders: set[str] = set()
        simple_placeholder_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

        for literal_text, field_name, format_spec, conversion in formatter.parse(
            system_prompt_template
        ):
            escaped_template_parts.append(
                literal_text.replace("{", "{{").replace("}", "}}")
            )
            if field_name is None:
                continue

            if (
                simple_placeholder_pattern.fullmatch(field_name)
                and not format_spec
                and conversion is None
            ):
                if field_name not in format_args:
                    unknown_placeholders.add(field_name)
                    continue
                escaped_template_parts.append(f"{{{field_name}}}")
                continue

            literal_field = "{" + field_name
            if conversion is not None:
                literal_field += f"!{conversion}"
            if format_spec:
                literal_field += f":{format_spec}"
            literal_field += "}"
            escaped_template_parts.append(
                literal_field.replace("{", "{{").replace("}", "}}")
            )

        if unknown_placeholders:
            unknown_placeholder_list = ", ".join(sorted(unknown_placeholders))
            raise ValueError(
                "System prompt template contains unknown placeholders: "
                f"{unknown_placeholder_list}. Escape literal braces with '{{' and '}}'."
            )
        escaped_template = "".join(escaped_template_parts)

        try:
            final_system_prompt = escaped_template.format_map(format_args).strip()
        except ValueError as exc:
            raise ValueError(f"Failed to format system prompt template: {exc}") from exc

        if isinstance(system_prompt_docs, str) and system_prompt_docs.strip():
            if final_system_prompt:
                final_system_prompt = (
                    f"{final_system_prompt}\n{system_prompt_docs}".strip()
                )
            else:
                final_system_prompt = system_prompt_docs.strip()

        return self.context_preparer.prepend_profile_preamble(final_system_prompt)

    @staticmethod
    def _inject_trigger_attachment_metadata(
        messages_for_llm: list[LLMMessage],
        trigger_attachments: list[MessageAttachmentMetadata] | None,
    ) -> None:
        """Inject trigger-attachment metadata into the latest trigger message."""
        if not trigger_attachments:
            return

        metadata_text = format_attachment_metadata_block(trigger_attachments)
        if not metadata_text:
            return

        for i in range(len(messages_for_llm) - 1, -1, -1):
            msg = messages_for_llm[i]
            if isinstance(msg, UserMessage):
                inject_metadata_into_user_message(msg, metadata_text)
                return
            if isinstance(msg, SystemMessage):
                msg.content = f"{msg.content}\n\n{metadata_text}"
                return

    @staticmethod
    def _is_delegation_wake_trigger_content(content: str) -> bool:
        """Return whether content is a one-shot delegation wake trigger."""
        return content.startswith((
            "System: Delegated profile task completed.",
            "System: Delegated profile task failed.",
        ))

    @staticmethod
    def _replace_historical_delegation_wake_with_active_system_trigger(
        messages_for_llm: list[LLMMessage],
        trigger_content: str,
    ) -> None:
        """Make the current wake a system trigger without replaying old wakes as system."""
        delegation_reference_line = next(
            (
                line
                for line in trigger_content.splitlines()
                if line.startswith("Delegation reference:")
            ),
            None,
        )
        for index in range(len(messages_for_llm) - 1, -1, -1):
            msg = messages_for_llm[index]
            if (
                delegation_reference_line is not None
                and isinstance(msg, UserMessage)
                and isinstance(msg.content, str)
                and msg.content.startswith(
                    "Historical delegation completion event from a previous turn."
                )
                and delegation_reference_line in msg.content
            ):
                messages_for_llm.pop(index)
                break
        messages_for_llm.append(SystemMessage(content=trigger_content))

    async def _save_history_message(
        self,
        db_context: DatabaseContext,
        *,
        message: LLMMessage,
        interface_type: str,
        conversation_id: str,
        turn_id: str,
        thread_root_id: int | None,
        timestamp: datetime | None = None,
        interface_message_id: str | None = None,
        subconversation_id: str | None = None,
        user_id: str | None = None,
        reasoning_info: MessageReasoningInfo | None = None,
        attachments: list[MessageAttachmentMetadata] | None = None,
        is_internal: bool = False,
        save_with_isolated_context: bool = False,
    ) -> int | None:
        """Persist a history message using either the active context or a fresh one."""
        message_timestamp = timestamp if timestamp is not None else self.clock.now()

        async def _persist_message(target_db_context: DatabaseContext) -> int | None:
            return await target_db_context.message_history.add_message(
                message=message,
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=interface_message_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id,
                timestamp=message_timestamp,
                processing_profile_id=self.service_config.id,
                subconversation_id=subconversation_id,
                user_id=user_id,
                reasoning_info=reasoning_info,
                attachments=attachments,
                is_internal=is_internal,
            )

        # On SQLite, avoid nested contexts with StaticPool because they may share
        # the same underlying connection/transaction as the outer context.
        if save_with_isolated_context and db_context.supports_isolated_writes:
            async with db_context.create_isolated_context() as isolated_db_context:
                return await _persist_message(isolated_db_context)

        return await _persist_message(db_context)

    async def _persist_error_history_message(
        self,
        db_context: DatabaseContext,
        *,
        error_message: str,
        error_traceback: str,
        interface_type: str,
        conversation_id: str,
        turn_id: str,
        thread_root_id: int | None,
        subconversation_id: str | None,
        user_id: str | None,
        save_with_isolated_context: bool | None = None,
    ) -> int | None:
        """Persist a processing error message with the standard write strategy."""
        use_isolated_context = (
            self._USE_ISOLATED_HISTORY_WRITES
            if save_with_isolated_context is None
            else save_with_isolated_context
        )
        try:
            return await self._save_history_message(
                db_context,
                message=ErrorMessage(
                    content=error_message,
                    error_traceback=error_traceback,
                ),
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=None,
                turn_id=turn_id,
                thread_root_id=thread_root_id,
                subconversation_id=subconversation_id,
                user_id=user_id,
                save_with_isolated_context=use_isolated_context,
            )
        except Exception:
            logger.error("Failed to save error message to history", exc_info=True)
            return None

    async def _prepare_turn_messages_for_llm(
        self,
        db_context: DatabaseContext,
        *,
        interface_type: str,
        conversation_id: str,
        trigger_content_parts: list[ContentPartDict],
        trigger_interface_message_id: str | None,
        user_name: str,
        turn_id: str,
        user_id: str | None,
        replied_to_interface_id: str | None,
        trigger_attachments: list[MessageAttachmentMetadata] | None,
        subconversation_id: str | None,
        thread_root_id: int | None = None,
        trigger_is_internal: bool = False,
        pinned_history_message_ids: list[int] | None = None,
        save_history_with_isolated_context: bool = True,
        trigger_role: Literal["user", "system"] = "user",
        reuse_existing_user_row: bool = False,
    ) -> tuple[int | None, list[LLMMessage], tuple[TaintSource, ...]]:
        """Build the full pre-LLM turn state shared by sync and streaming flows."""
        thread_root_id_for_turn = thread_root_id
        if thread_root_id_for_turn is None:
            thread_root_id_for_turn = await self._resolve_thread_root_id(
                db_context=db_context,
                interface_type=interface_type,
                replied_to_interface_id=replied_to_interface_id,
            )
        user_content_for_history = self._extract_user_content_for_history(
            trigger_content_parts
        )
        actual_interface_message_id = trigger_interface_message_id or f"temp_{turn_id}"

        trigger_message: LLMMessage
        if trigger_role == "system":
            trigger_message = SystemMessage(content=user_content_for_history)
        else:
            trigger_message = UserMessage(content=user_content_for_history)
        # When the caller already persisted this turn's user message, reuse it
        # instead of inserting a duplicate. The web endpoint does this before
        # launching the (cancellable) producer task, so a Stop that cancels the
        # producer before it runs still leaves the prompt durable. Only the web
        # path sets this; other callers insert here as usual, so we avoid an extra
        # read on their (often single-connection SQLite) db_context.
        existing_user_row = (
            await db_context.message_history.get_user_row_by_turn_id(turn_id)
            if reuse_existing_user_row and trigger_role == "user"
            else None
        )
        if existing_user_row is not None:
            saved_user_msg_record = existing_user_row["internal_id"]
        else:
            saved_user_msg_record = await self._save_history_message(
                db_context,
                message=trigger_message,
                interface_type=interface_type,
                conversation_id=conversation_id,
                interface_message_id=actual_interface_message_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id_for_turn,
                timestamp=self.clock.now(),
                attachments=trigger_attachments,
                subconversation_id=subconversation_id,
                user_id=user_id,
                is_internal=trigger_is_internal,
                save_with_isolated_context=save_history_with_isolated_context,
            )
        if saved_user_msg_record is not None and thread_root_id_for_turn is None:
            thread_root_id_for_turn = saved_user_msg_record
            logger.info("Established new thread_root_id: %s", thread_root_id_for_turn)

        (
            messages_for_llm,
            thread_attachments_context,
        ) = await self._build_initial_messages_for_llm(
            db_context=db_context,
            interface_type=interface_type,
            conversation_id=conversation_id,
            replied_to_interface_id=replied_to_interface_id,
            thread_root_id_for_turn=thread_root_id_for_turn,
            subconversation_id=subconversation_id,
        )
        if trigger_role == "system" and self._is_delegation_wake_trigger_content(
            user_content_for_history
        ):
            self._replace_historical_delegation_wake_with_active_system_trigger(
                messages_for_llm,
                user_content_for_history,
            )
        await self._append_missing_pinned_history_messages(
            db_context,
            messages_for_llm,
            pinned_history_message_ids,
        )
        pruned_count = self._prune_leading_invalid_messages(messages_for_llm)
        if pruned_count > 0:
            logger.warning("Pruned %d leading messages from LLM history.", pruned_count)

        aggregated_other_context_str = await self.context_preparer.aggregate_context()
        context_taint_sources = (
            await self.context_preparer.aggregate_context_taint_sources()
        )
        if thread_attachments_context:
            if aggregated_other_context_str:
                aggregated_other_context_str += "\n\n" + thread_attachments_context
            else:
                aggregated_other_context_str = thread_attachments_context

        final_system_prompt = self._render_system_prompt(
            user_name=user_name,
            aggregated_other_context_str=aggregated_other_context_str,
        )
        delegation_addition = await self.delegation_catalog_addition()
        if delegation_addition:
            final_system_prompt = (
                f"{final_system_prompt}\n\n{delegation_addition}".strip()
            )
        if final_system_prompt:
            messages_for_llm.insert(0, SystemMessage(content=final_system_prompt))

        attachment_injection_messages = (
            await self.attachment_processor.process_content_parts(
                db_context,
                conversation_id,
                trigger_content_parts,
            )
        )
        messages_for_llm.extend(attachment_injection_messages)
        self._inject_trigger_attachment_metadata(
            messages_for_llm=messages_for_llm,
            trigger_attachments=trigger_attachments,
        )
        typed_messages_for_llm = await self.attachment_processor.convert_message_urls(
            messages_for_llm
        )
        return thread_root_id_for_turn, typed_messages_for_llm, context_taint_sources

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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
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
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
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
            confirmation_ui_managers=confirmation_ui_managers,
            request_confirmation_callback=request_confirmation_callback,
            subconversation_id=subconversation_id,
            processing_service=self,
            home_assistant_client=self.home_assistant_client,
            camera_backend=self.camera_backend,
            event_sources=self.event_sources,
            mid_turn_input_provider=mid_turn_input_provider,
            initial_taint_sources=initial_taint_sources,
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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        turn_id: str | None = None,
        thread_root_id: int | None = None,
        trigger_is_internal: bool = False,
        pinned_history_message_ids: list[int] | None = None,
        trigger_role: Literal["user", "system"] = "user",
        save_history_with_isolated_context: bool | None = None,
        initial_taint_sources: Sequence[TaintSource] | None = None,
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
            confirmation_ui_managers: Confirmation UI managers by interface
            request_confirmation_callback: Callback for tool confirmations
            trigger_attachments: Attachments from the user
            subconversation_id: Subconversation identifier
            thread_root_id: Existing message-history row to use as this turn's thread root
            trigger_is_internal: Hide the trigger row from user-facing history.
            pinned_history_message_ids: Message rows that must be present even if
                normal history limits would exclude them.

        Returns:
            ChatInteractionResult containing:
            - text_reply: Final LLM content to send to user (str; empty if no text)
            - assistant_message_internal_id: Internal message ID of assistant's response (int | None)
            - reasoning_info: Final reasoning information (dict | None)
            - error_traceback: Processing error traceback if any (str | None)
            - attachment_ids: Response attachment IDs (list[str] | None)
        """

        if turn_id is None:
            turn_id = str(uuid.uuid4())
        use_isolated_history_writes = (
            self._USE_ISOLATED_HISTORY_WRITES
            if save_history_with_isolated_context is None
            else save_history_with_isolated_context
        )
        logger.info(
            f"Starting handle_chat_interaction for conversation {conversation_id}, turn {turn_id}"
        )

        thread_root_id_for_turn: int | None = None
        try:
            # --- 1-2. Persist user trigger + build LLM-ready messages ---
            (
                thread_root_id_for_turn,
                typed_messages_for_llm,
                context_taint_sources,
            ) = await self._prepare_turn_messages_for_llm(
                db_context,
                interface_type=interface_type,
                conversation_id=conversation_id,
                trigger_content_parts=trigger_content_parts,
                trigger_interface_message_id=trigger_interface_message_id,
                user_name=user_name,
                turn_id=turn_id,
                user_id=user_id,
                replied_to_interface_id=replied_to_interface_id,
                trigger_attachments=trigger_attachments,
                subconversation_id=subconversation_id,
                thread_root_id=thread_root_id,
                trigger_is_internal=trigger_is_internal,
                pinned_history_message_ids=pinned_history_message_ids,
                save_history_with_isolated_context=use_isolated_history_writes,
                trigger_role=trigger_role,
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
                chat_interfaces=chat_interfaces,
                confirmation_ui_managers=confirmation_ui_managers,
                request_confirmation_callback=request_confirmation_callback,
                subconversation_id=subconversation_id,
                mid_turn_input_provider=mid_turn_input_provider,
                initial_taint_sources=(
                    *context_taint_sources,
                    *(initial_taint_sources or ()),
                ),
            )
            final_reasoning_info = final_reasoning_info_from_process_msg

            # --- 4. Save Generated Turn Messages & Extract Final Reply ---
            final_text_reply = ""
            final_assistant_message_internal_id = None

            if generated_turn_messages:
                for turn_msg in generated_turn_messages:
                    if (
                        isinstance(turn_msg, AssistantMessage)
                        and turn_msg.content
                        and not turn_msg.tool_calls
                    ):
                        # Skip messages that carry tool calls: their content
                        # may be cryptographically tied to a Google thought
                        # signature (see ContextPreparer.format_history) and
                        # rewriting it would break replay continuity.
                        turn_msg.content = normalize_latex_to_unicode(turn_msg.content)

                    reasoning_info_for_msg = (
                        final_reasoning_info
                        if isinstance(turn_msg, AssistantMessage)
                        else None
                    )
                    saved_turn_msg_record = await self._save_history_message(
                        db_context,
                        message=turn_msg,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        thread_root_id=thread_root_id_for_turn,
                        subconversation_id=subconversation_id,
                        user_id=user_id,
                        reasoning_info=reasoning_info_for_msg,
                        save_with_isolated_context=use_isolated_history_writes,
                    )

                    if isinstance(turn_msg, AssistantMessage) and turn_msg.content:
                        final_text_reply = turn_msg.content
                        if saved_turn_msg_record is not None:
                            final_assistant_message_internal_id = saved_turn_msg_record
            else:
                logger.warning(
                    f"No messages generated by self.process_message for turn {turn_id}."
                )

            return ChatInteractionResult.success(
                text_reply=final_text_reply,
                assistant_message_internal_id=final_assistant_message_internal_id,
                reasoning_info=final_reasoning_info,
                attachment_ids=response_attachment_ids,
            )

        except Exception as exc:
            logger.error(
                f"Error in handle_chat_interaction for conversation {conversation_id}, turn {turn_id}",
                exc_info=True,
            )
            processing_error_traceback = traceback.format_exc()

            error_message = _user_friendly_error_message(exc)
            error_message_internal_id = await self._persist_error_history_message(
                db_context,
                error_message=error_message,
                error_traceback=processing_error_traceback,
                interface_type=interface_type,
                conversation_id=conversation_id,
                turn_id=turn_id,
                thread_root_id=thread_root_id_for_turn,
                subconversation_id=subconversation_id,
                user_id=user_id,
                save_with_isolated_context=use_isolated_history_writes,
            )

            return ChatInteractionResult.error(
                text_reply=error_message,
                error_traceback=processing_error_traceback,
                assistant_message_internal_id=error_message_internal_id,
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
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        trigger_attachments: list[MessageAttachmentMetadata] | None = None,
        subconversation_id: str | None = None,
        mid_turn_input_provider: MidTurnInputProvider | None = None,
        turn_id: str | None = None,
        reuse_existing_user_row: bool = False,
        initial_taint_sources: Sequence[TaintSource] | None = None,
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
        if turn_id is None:
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

        thread_root_id_for_turn: int | None = None
        try:
            with trace.use_span(span, end_on_exit=False):
                try:
                    # --- 1-2. Persist user trigger + build LLM-ready messages ---
                    (
                        thread_root_id_for_turn,
                        typed_messages_for_llm,
                        context_taint_sources,
                    ) = await self._prepare_turn_messages_for_llm(
                        db_context,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        trigger_content_parts=trigger_content_parts,
                        trigger_interface_message_id=trigger_interface_message_id,
                        user_name=user_name,
                        turn_id=turn_id,
                        user_id=user_id,
                        replied_to_interface_id=replied_to_interface_id,
                        trigger_attachments=trigger_attachments,
                        subconversation_id=subconversation_id,
                        reuse_existing_user_row=reuse_existing_user_row,
                    )

                    # --- 3. Stream LLM Processing ---
                    async for event, stream_msg in self.process_message_stream(
                        db_context=db_context,
                        messages=typed_messages_for_llm,
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
                        mid_turn_input_provider=mid_turn_input_provider,
                        initial_taint_sources=(
                            *context_taint_sources,
                            *(initial_taint_sources or ()),
                        ),
                    ):
                        yield event  # noqa: ASYNC119

                        # Save messages as they're generated
                        if stream_msg is not None:
                            if (
                                isinstance(stream_msg, AssistantMessage)
                                and stream_msg.content
                                and not stream_msg.tool_calls
                            ):
                                # Skip messages that carry tool calls: see
                                # the matching branch in
                                # handle_chat_interaction for the
                                # thought-signature rationale.
                                stream_msg.content = normalize_latex_to_unicode(
                                    stream_msg.content
                                )
                            reasoning_info_for_stream = (
                                event.metadata.get("reasoning_info")
                                if isinstance(stream_msg, AssistantMessage)
                                and event.metadata
                                else None
                            )
                            await self._save_history_message(
                                db_context,
                                message=stream_msg,
                                interface_type=interface_type,
                                conversation_id=conversation_id,
                                turn_id=turn_id,
                                thread_root_id=thread_root_id_for_turn,
                                subconversation_id=subconversation_id,
                                user_id=user_id,
                                reasoning_info=reasoning_info_for_stream,
                                save_with_isolated_context=self._USE_ISOLATED_HISTORY_WRITES,
                            )

                except Exception as e:
                    span.set_status(StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    logger.error(
                        f"Error in streaming chat interaction: {e}", exc_info=True
                    )
                    processing_error_traceback = traceback.format_exc()
                    error_message = _user_friendly_error_message(e)
                    await self._persist_error_history_message(
                        db_context,
                        error_message=error_message,
                        error_traceback=processing_error_traceback,
                        interface_type=interface_type,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        thread_root_id=thread_root_id_for_turn,
                        subconversation_id=subconversation_id,
                        user_id=user_id,
                    )
                    error_event = LLMStreamEvent(
                        type="error",
                        error=error_message,
                        metadata={"error_id": str(uuid.uuid4())},
                    )

                    yield error_event  # noqa: ASYNC119
        finally:
            span.end()
