from __future__ import annotations

import json
import logging
import traceback
import uuid
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from family_assistant.llm import LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.messages import (
    ProviderMetadataDict,
    ToolMessage,
    tool_result_to_llm_message,
)
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolPolicyDeniedError,
    ToolsProvider,
)
from family_assistant.tools.types import ToolAttachment, ToolResult

from .types import (
    RequestConfirmationCallback,
    ToolExecutionResult,
    ToolExecutorConfig,
)
from .utils import get_file_extension_from_mime_type

if TYPE_CHECKING:
    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.google_types import GeminiProviderMetadata
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import EventSourcesById
    from family_assistant.utils.clock import Clock

    from .attachments import AttachmentProcessor
    from .service import ProcessingService

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ToolExecutor:
    """Executes individual tool calls with result/error handling."""

    def __init__(
        self,
        tools_provider: ToolsProvider,
        config: ToolExecutorConfig,
        attachment_processor: AttachmentProcessor,
        attachment_registry: AttachmentRegistry | None,
        clock: Clock,
    ) -> None:
        self.tools_provider = tools_provider
        self.config = config
        self.attachment_processor = attachment_processor
        self.attachment_registry = attachment_registry
        self.clock = clock

    @staticmethod
    def _extract_queued_attachment_ids(result_payload: str) -> list[str] | None:
        """Parse attach_to_response JSON payload and return queued attachment IDs."""
        parsed_payload = json.loads(result_payload)
        if not isinstance(parsed_payload, dict):
            raise ValueError("attach_to_response result payload must be an object")

        if parsed_payload.get("status") != "attachments_queued":
            return None

        attachment_ids_raw = parsed_payload.get("attachment_ids")
        if not isinstance(attachment_ids_raw, list):
            raise ValueError(
                "attach_to_response result must include attachment_ids as a list"
            )
        return [str(attachment_id) for attachment_id in attachment_ids_raw]

    async def _build_attach_to_response_metadata(
        self,
        db_context: DatabaseContext,
        attachment_ids: list[str],
    ) -> list[dict[str, str | int | None]]:
        """Fetch metadata for queued attachments to enrich stream output."""
        if not self.attachment_registry:
            raise RuntimeError(
                "attach_to_response metadata enrichment requires AttachmentRegistry"
            )

        attachment_metadata_list: list[dict[str, str | int | None]] = []
        for attachment_id in attachment_ids:
            attachment_info = await self.attachment_registry.get_attachment(
                db_context, attachment_id
            )
            if attachment_info is None:
                raise ValueError(
                    f"attach_to_response referenced unknown attachment '{attachment_id}'"
                )
            attachment_metadata_list.append({
                "attachment_id": attachment_id,
                "type": "tool_result",
                "description": attachment_info.description or "Attachment",
                "url": attachment_info.content_url,
                "content_url": attachment_info.content_url,
                "mime_type": attachment_info.mime_type,
                "size": attachment_info.size,
            })
        return attachment_metadata_list

    async def _build_attach_to_response_output(
        self,
        db_context: DatabaseContext,
        result_payload: str,
    ) -> tuple[list[str] | None, StreamEventMetadata | None]:
        """Build explicit attachment IDs and metadata for attach_to_response output."""
        queued_attachment_ids = self._extract_queued_attachment_ids(result_payload)
        if not queued_attachment_ids:
            return None, None

        attachment_metadata_list = await self._build_attach_to_response_metadata(
            db_context, queued_attachment_ids
        )
        logger.info(
            "Enriched attach_to_response result with %d attachment metadata entries",
            len(attachment_metadata_list),
        )
        return queued_attachment_ids, {"attachments": attachment_metadata_list}

    def _build_execution_context(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        user_id: str | None,
        turn_id: str,
        db_context: DatabaseContext,
        chat_interface: ChatInterface | None,
        chat_interfaces: dict[str, ChatInterface] | None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None,
        request_confirmation_callback: RequestConfirmationCallback | None,
        subconversation_id: str | None,
        processing_service: ProcessingService | None,
        home_assistant_client: HomeAssistantClientWrapper | None,
        camera_backend: CameraBackend | None,
        event_sources: EventSourcesById | None,
    ) -> ToolExecutionContext:
        chat_interfaces_dict = chat_interfaces
        if chat_interfaces_dict is None and chat_interface:
            chat_interfaces_dict = {interface_type: chat_interface}

        return ToolExecutionContext(
            interface_type=interface_type,
            conversation_id=conversation_id,
            user_name=user_name,
            user_id=user_id,
            turn_id=turn_id,
            db_context=db_context,
            chat_interface=chat_interface,
            chat_interfaces=chat_interfaces_dict,
            confirmation_ui_managers=confirmation_ui_managers,
            timezone=self.config.timezone,
            processing_profile_id=self.config.id,
            subconversation_id=subconversation_id,
            request_confirmation_callback=request_confirmation_callback,
            processing_service=processing_service,
            clock=self.clock,
            home_assistant_client=home_assistant_client,
            event_sources=event_sources,
            indexing_source=(
                cast("IndexingSource | None", event_sources.get("indexing"))
                if event_sources
                else None
            ),
            attachment_registry=self.attachment_registry,
            camera_backend=camera_backend,
            visibility_grants=self.config.visibility_grants,
            default_note_visibility_labels=self.config.default_note_visibility_labels,
            note_registry=self.config.note_registry,
        )

    @staticmethod
    def _build_error_result(
        *,
        call_id: str,
        function_name: str,
        error_content: str,
        error_traceback: str,
    ) -> ToolExecutionResult:
        """Build a standardized tool error result for stream and history."""
        return ToolExecutionResult(
            stream_event=LLMStreamEvent(
                type="tool_result",
                tool_call_id=call_id,
                tool_result=error_content,
                error=error_traceback,
            ),
            llm_message=ToolMessage(
                tool_call_id=call_id,
                content=error_content,
                error_traceback=error_traceback,
                name=function_name,
            ),
            auto_attachment_ids=None,
            explicit_attachment_ids=None,
        )

    async def _execute_tool_with_error_mapping(
        self,
        *,
        function_name: str,
        arguments: dict[str, object],
        tool_execution_context: ToolExecutionContext,
        call_id: str,
        span: Span,
    ) -> ToolResult | object | ToolExecutionResult:
        """Execute a tool and map tool runtime failures to tool_result errors."""
        try:
            result = await self.tools_provider.execute_tool(
                function_name, arguments, tool_execution_context, call_id
            )
            logger.info("Tool '%s' executed successfully.", function_name)
            return result
        except ToolPolicyDeniedError as e:
            logger.warning("Tool '%s' denied by policy: %s", function_name, e.reason)
            error_content = f"Error: Tool '{function_name}' is not allowed. {e.reason}"
            error_traceback = traceback.format_exc()
            span.set_status(StatusCode.ERROR, error_content)
            span.set_attribute("tool.status", "error")
            return self._build_error_result(
                call_id=call_id,
                function_name=function_name,
                error_content=error_content,
                error_traceback=error_traceback,
            )
        except ToolNotFoundError:
            logger.error("Tool '%s' not found.", function_name)
            error_content = f"Error: Tool '{function_name}' not found."
            error_traceback = traceback.format_exc()
            span.set_status(StatusCode.ERROR, f"Tool '{function_name}' not found.")
            span.set_attribute("tool.status", "error")
            return self._build_error_result(
                call_id=call_id,
                function_name=function_name,
                error_content=error_content,
                error_traceback=error_traceback,
            )
        except Exception as exc:  # Tool implementation/runtime error
            logger.error(
                "Error executing tool '%s': %s",
                function_name,
                exc,
                exc_info=exc,
            )
            error_content = f"Error executing {function_name}: {exc}"
            error_traceback = traceback.format_exc()
            span.set_status(StatusCode.ERROR, str(exc))
            span.record_exception(exc)
            span.set_attribute("tool.status", "error")
            return self._build_error_result(
                call_id=call_id,
                function_name=function_name,
                error_content=error_content,
                error_traceback=error_traceback,
            )

    @staticmethod
    def _parse_arguments(
        function_name: str,
        function_args: object,
    ) -> dict[str, object]:
        """Parse tool-call arguments and enforce object shape."""
        arguments: object
        if isinstance(function_args, str):
            try:
                arguments = json.loads(function_args)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON arguments for tool '{function_name}' "
                    f"(line {exc.lineno}, column {exc.colno})"
                ) from exc
        else:
            arguments = function_args

        if not isinstance(arguments, dict):
            raise TypeError(
                f"Expected JSON object for tool arguments to '{function_name}', got {type(arguments).__name__}"
            )

        return cast("dict[str, object]", arguments)

    async def _handle_large_text_result(
        self,
        *,
        db_context: DatabaseContext,
        content: str,
        function_name: str,
        conversation_id: str,
        call_id: str,
    ) -> tuple[str, list[str]]:
        """Convert oversized text results into attachment references."""
        (
            new_content,
            auto_attachment_id,
        ) = await self.attachment_processor.handle_large_result(
            db_context,
            content,
            function_name,
            conversation_id,
            call_id,
        )
        if auto_attachment_id is None:
            return new_content, []
        return new_content, [auto_attachment_id]

    async def _process_tool_attachments(
        self,
        *,
        db_context: DatabaseContext,
        attachments: list[ToolAttachment],
        function_name: str,
        conversation_id: str,
        call_id: str,
    ) -> tuple[list[dict[str, str | int | None]], list[str]]:
        """Store/normalize ToolResult attachments for streaming and history."""
        attachments_data: list[dict[str, str | int | None]] = []
        auto_attachment_ids: list[str] = []

        for attachment in attachments:
            attachment_data: dict[str, str | int | None] = {
                "type": "tool_result",
                "mime_type": attachment.mime_type,
                "description": attachment.description,
            }

            if attachment.content and self.attachment_registry:
                file_extension = get_file_extension_from_mime_type(attachment.mime_type)
                registered_metadata = (
                    await self.attachment_registry.store_and_register_tool_attachment(
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
                )

                attachment_data["content_url"] = registered_metadata.content_url or ""
                attachment_data["attachment_id"] = registered_metadata.attachment_id
                auto_attachment_ids.append(registered_metadata.attachment_id)
                attachment.attachment_id = registered_metadata.attachment_id
                logger.info(
                    "Stored and registered tool attachment: %s",
                    registered_metadata.attachment_id,
                )
            elif attachment.attachment_id:
                attachment_data["attachment_id"] = attachment.attachment_id
                auto_attachment_ids.append(attachment.attachment_id)
                logger.info(
                    "Queuing existing attachment reference: %s",
                    attachment.attachment_id,
                )

            attachments_data.append(attachment_data)

        return attachments_data, auto_attachment_ids

    async def _build_output_for_tool_result(
        self,
        *,
        db_context: DatabaseContext,
        result: ToolResult,
        function_name: str,
        conversation_id: str,
        call_id: str,
        provider_metadata: GeminiProviderMetadata | ProviderMetadataDict | None,
    ) -> tuple[str, ToolMessage, StreamEventMetadata | None, list[str]]:
        """Convert ToolResult into stream payload, message, and attachment IDs."""
        content_for_stream = result.get_text()
        content_for_stream, auto_attachment_ids = await self._handle_large_text_result(
            db_context=db_context,
            content=content_for_stream,
            function_name=function_name,
            conversation_id=conversation_id,
            call_id=call_id,
        )
        if auto_attachment_ids:
            # Result data is now persisted as attachment; keep content as hint text.
            result.text = content_for_stream
            result.data = None

        attachments_data: list[dict[str, str | int | None]] = []
        if result.attachments:
            (
                attachments_data,
                new_attachment_ids,
            ) = await self._process_tool_attachments(
                db_context=db_context,
                attachments=result.attachments,
                function_name=function_name,
                conversation_id=conversation_id,
                call_id=call_id,
            )
            auto_attachment_ids.extend(new_attachment_ids)

        llm_message = tool_result_to_llm_message(
            result,
            call_id,
            function_name,
            provider_metadata=provider_metadata,
        )

        if auto_attachment_ids:
            attachment_id_list = ", ".join(auto_attachment_ids)
            llm_message = llm_message.model_copy(
                update={
                    "content": llm_message.content
                    + f"\n[Attachment ID(s): {attachment_id_list}]"
                }
            )

        stream_metadata: StreamEventMetadata | None = None
        if attachments_data:
            stream_metadata = {"attachments": attachments_data}
            llm_message = llm_message.model_copy(
                update={"attachments": attachments_data}
            )

        return content_for_stream, llm_message, stream_metadata, auto_attachment_ids

    async def _build_output_for_string_result(
        self,
        *,
        db_context: DatabaseContext,
        result: object,
        function_name: str,
        conversation_id: str,
        call_id: str,
    ) -> tuple[str, ToolMessage, StreamEventMetadata | None, list[str]]:
        """Convert plain string-like tool output into stream/message payload."""
        content_for_stream = str(result)
        content_for_stream, auto_attachment_ids = await self._handle_large_text_result(
            db_context=db_context,
            content=content_for_stream,
            function_name=function_name,
            conversation_id=conversation_id,
            call_id=call_id,
        )
        return (
            content_for_stream,
            ToolMessage(
                tool_call_id=call_id,
                content=content_for_stream,
                name=function_name,
            ),
            None,
            auto_attachment_ids,
        )

    async def execute(
        self,
        tool_call_item_obj: ToolCallItem,
        *,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        db_context: DatabaseContext,
        chat_interface: ChatInterface | None,
        user_id: str | None = None,
        chat_interfaces: dict[str, ChatInterface] | None = None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None = None,
        request_confirmation_callback: RequestConfirmationCallback | None = None,
        subconversation_id: str | None = None,
        processing_service: ProcessingService | None = None,
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        event_sources: EventSourcesById | None = None,
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
            processing_service: The processing service instance
            home_assistant_client: Home Assistant client wrapper
            camera_backend: Camera backend instance
            event_sources: Event sources mapping

        Returns:
            ToolExecutionResult with stream event, LLM message, and attachment IDs
        """
        call_id = tool_call_item_obj.id
        if not call_id:
            raise ValueError("Tool call must include a non-empty id")

        function_name = tool_call_item_obj.function.name
        if not function_name:
            raise ValueError(
                f"Tool call '{call_id}' must include a non-empty function name"
            )

        function_args = tool_call_item_obj.function.arguments

        with tracer.start_as_current_span(
            f"tool.execute.{function_name}",
            attributes={
                "tool.name": function_name,
                "tool.call_id": call_id,
            },
        ) as span:
            # Parse arguments
            try:
                arguments = self._parse_arguments(function_name, function_args)
            except ValueError as exc:
                logger.error("Failed to parse arguments for %s: %s", function_name, exc)
                return self._build_error_result(
                    call_id=call_id,
                    function_name=function_name,
                    error_content=f"Error: Invalid arguments format for {function_name}.",
                    error_traceback=str(exc),
                )
            except TypeError as exc:
                logger.error(
                    "Tool '%s' received non-object arguments: %s",
                    function_name,
                    exc,
                )
                return self._build_error_result(
                    call_id=call_id,
                    function_name=function_name,
                    error_content=f"Error: Invalid arguments format for {function_name}.",
                    error_traceback=str(exc),
                )

            # Execute tool
            logger.info(
                "Executing tool '%s' with argument keys: %s",
                function_name,
                sorted(arguments.keys()),
            )

            tool_execution_context = self._build_execution_context(
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
            )

            result_or_error = await self._execute_tool_with_error_mapping(
                function_name=function_name,
                arguments=arguments,
                tool_execution_context=tool_execution_context,
                call_id=call_id,
                span=span,
            )
            if isinstance(result_or_error, ToolExecutionResult):
                return result_or_error

            # Post-execution processing failures (attachment IO/enrichment/metadata
            # handling) should fail fast and propagate to callers.
            result = result_or_error
            explicit_attachment_ids: list[str] | None = None

            if isinstance(result, ToolResult):
                (
                    content_for_stream,
                    llm_message,
                    stream_metadata,
                    auto_attachment_ids,
                ) = await self._build_output_for_tool_result(
                    db_context=db_context,
                    result=result,
                    function_name=function_name,
                    conversation_id=conversation_id,
                    call_id=call_id,
                    provider_metadata=tool_call_item_obj.provider_metadata,
                )
            else:
                (
                    content_for_stream,
                    llm_message,
                    stream_metadata,
                    auto_attachment_ids,
                ) = await self._build_output_for_string_result(
                    db_context=db_context,
                    result=result,
                    function_name=function_name,
                    conversation_id=conversation_id,
                    call_id=call_id,
                )

            if function_name == "attach_to_response":
                (
                    explicit_attachment_ids,
                    explicit_stream_metadata,
                ) = await self._build_attach_to_response_output(
                    db_context, content_for_stream
                )
                if explicit_stream_metadata is not None:
                    stream_metadata = explicit_stream_metadata

            span.set_attribute("tool.status", "success")
            span.set_attribute("tool.result_size", len(content_for_stream))

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=content_for_stream,
                    metadata=stream_metadata,
                ),
                llm_message=llm_message,
                auto_attachment_ids=auto_attachment_ids
                if auto_attachment_ids
                else None,
                explicit_attachment_ids=explicit_attachment_ids,
            )
