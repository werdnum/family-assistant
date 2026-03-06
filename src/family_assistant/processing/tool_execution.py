from __future__ import annotations

import json
import logging
import traceback
import uuid
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from family_assistant.llm import LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.messages import ToolMessage, tool_result_to_llm_message
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolsProvider,
)
from family_assistant.tools.types import ToolResult

from .types import ProcessingServiceConfig, ToolExecutionResult
from .utils import get_file_extension_from_mime_type

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.utils.clock import Clock

    from .attachments import AttachmentProcessor

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ToolExecutor:
    """Executes individual tool calls with result/error handling."""

    def __init__(
        self,
        tools_provider: ToolsProvider,
        service_config: ProcessingServiceConfig,
        attachment_processor: AttachmentProcessor,
        attachment_registry: AttachmentRegistry | None,
        clock: Clock,
    ) -> None:
        self.tools_provider = tools_provider
        self.service_config = service_config
        self.attachment_processor = attachment_processor
        self.attachment_registry = attachment_registry
        self.clock = clock

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
        request_confirmation_callback: (
            Callable[
                # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
                [
                    str,
                    str,
                    str | None,
                    str,
                    str,
                    # ast-grep-ignore: no-dict-any - Legacy callback signature from original code
                    dict[str, Any],
                    float,
                    ToolExecutionContext,
                ],
                Awaitable[bool],
            ]
            | None
        ) = None,
        subconversation_id: str | None = None,
        processing_service: Any = None,  # noqa: ANN401 - Circular import with ProcessingService
        home_assistant_client: HomeAssistantClientWrapper | None = None,
        camera_backend: CameraBackend | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        event_sources: dict[str, Any] | None = None,
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
        function_name = tool_call_item_obj.function.name
        function_args = tool_call_item_obj.function.arguments

        with tracer.start_as_current_span(
            f"tool.execute.{function_name}",
            attributes={
                "tool.name": function_name,
                "tool.call_id": call_id or "",
            },
        ) as span:
            # Validate tool call
            if not call_id or not function_name:
                logger.error(
                    f"Invalid tool call: id='{call_id}', name='{function_name}'"
                )
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

                return ToolExecutionResult(
                    stream_event=LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=call_id,
                        tool_result=error_content,
                        error=error_traceback,
                    ),
                    llm_message=llm_message,
                    auto_attachment_ids=None,
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

                return ToolExecutionResult(
                    stream_event=LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=call_id,
                        tool_result=error_content,
                        error=error_traceback,
                    ),
                    llm_message=llm_message,
                    auto_attachment_ids=None,
                )

            # Execute tool
            logger.info(f"Executing tool '{function_name}' with args: {arguments}")

            chat_interfaces_dict = chat_interfaces
            if chat_interfaces_dict is None and chat_interface:
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
                timezone=self.service_config.timezone,
                processing_profile_id=self.service_config.id,
                subconversation_id=subconversation_id,
                request_confirmation_callback=request_confirmation_callback,
                processing_service=processing_service,
                clock=self.clock,
                home_assistant_client=home_assistant_client,
                event_sources=event_sources,
                indexing_source=(
                    event_sources.get("indexing") if event_sources else None
                ),
                attachment_registry=self.attachment_registry,
                camera_backend=camera_backend,
                visibility_grants=self.service_config.visibility_grants,
                default_note_visibility_labels=self.service_config.default_note_visibility_labels,
                note_registry=self.service_config.note_registry,
            )

            try:
                # Execute the tool
                result = await self.tools_provider.execute_tool(
                    function_name, arguments, tool_execution_context, call_id
                )
                logger.info(f"Tool '{function_name}' executed successfully.")

                # Handle both string and ToolResult
                if isinstance(result, ToolResult):
                    content_for_stream = result.get_text()
                    auto_attachment_ids: list[
                        str
                    ] = []  # Track attachment IDs for auto-queuing

                    # Auto-convert large text result to attachment
                    (
                        new_content,
                        auto_att_id,
                    ) = await self.attachment_processor.handle_large_result(
                        db_context,
                        content_for_stream,
                        function_name,
                        conversation_id,
                        call_id,
                    )
                    if auto_att_id:
                        content_for_stream = new_content
                        auto_attachment_ids.append(auto_att_id)
                        # Update ToolResult so get_text() returns the hint.
                        # We also clear data to free memory, as it is now stored as an attachment.
                        result.text = new_content
                        result.data = None

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
                                    file_extension = get_file_extension_from_mime_type(
                                        attachment.mime_type
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
                                attachment_data["attachment_id"] = (
                                    attachment.attachment_id
                                )
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

                    if attachments_data:
                        llm_message = llm_message.model_copy(
                            update={"attachments": attachments_data}
                        )
                else:
                    # Plain string result (many tools return str directly)
                    content_for_stream = str(result)
                    auto_attachment_ids = []  # String results don't generate attachments

                    # Auto-convert large text result to attachment
                    (
                        new_content,
                        auto_att_id,
                    ) = await self.attachment_processor.handle_large_result(
                        db_context,
                        content_for_stream,
                        function_name,
                        conversation_id,
                        call_id,
                    )
                    if auto_att_id:
                        content_for_stream = new_content
                        auto_attachment_ids.append(auto_att_id)

                    llm_message = ToolMessage(
                        role="tool",
                        tool_call_id=call_id,
                        content=content_for_stream,
                        name=function_name,
                    )
                    stream_metadata: StreamEventMetadata | None = None

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
                                            attachment_info = await attachment_registry.get_attachment(
                                                db_context, attachment_id
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
                )

            except ToolNotFoundError:
                logger.error(f"Tool '{function_name}' not found.")
                error_content = f"Error: Tool '{function_name}' not found."
                error_traceback = traceback.format_exc()
                span.set_status(StatusCode.ERROR, f"Tool '{function_name}' not found.")
                span.set_attribute("tool.status", "error")

                llm_message = ToolMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=error_content,
                    error_traceback=error_traceback,
                    name=function_name,
                )

                return ToolExecutionResult(
                    stream_event=LLMStreamEvent(
                        type="tool_result",
                        tool_call_id=call_id,
                        tool_result=error_content,
                        error=error_traceback,
                    ),
                    llm_message=llm_message,
                    auto_attachment_ids=None,
                )

            except Exception as e:
                logger.error(
                    f"Error executing tool '{function_name}': {e}", exc_info=True
                )
                error_content = f"Error executing {function_name}: {str(e)}"
                error_traceback = traceback.format_exc()
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                span.set_attribute("tool.status", "error")

                llm_message = ToolMessage(
                    role="tool",
                    tool_call_id=call_id,
                    content=error_content,
                    error_traceback=error_traceback,
                    name=function_name,
                )

            return ToolExecutionResult(
                stream_event=LLMStreamEvent(
                    type="tool_result",
                    tool_call_id=call_id,
                    tool_result=error_content,
                    error=error_traceback,
                ),
                llm_message=llm_message,
                auto_attachment_ids=None,
            )
