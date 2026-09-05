from __future__ import annotations

import contextlib
import json
import logging
import traceback
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from family_assistant.llm import LLMStreamEvent, StreamEventMetadata
from family_assistant.llm.messages import (
    ProviderMetadataDict,
    ToolMessage,
    tool_result_to_llm_message,
)
from family_assistant.security.taint import (
    TurnTaintState,
    merge_taint_state_into_tracker,
)
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolPolicyDeniedError,
    ToolsProvider,
)
from family_assistant.tools.attachment_utils import is_attachment_id
from family_assistant.tools.computer_use_names import COMPUTER_USE_FUNCTION_NAMES
from family_assistant.tools.confirmation import confirmation_payload_block_reason
from family_assistant.tools.infrastructure import (
    ToolDescriptorProvider,
    confirmation_outcome_to_tool_result,
)
from family_assistant.tools.types import (
    ToolAttachment,
    ToolCallBatch,
    ToolCallReviewTurnState,
    ToolResult,
)

from .types import (
    RequestConfirmationCallback,
    ToolExecutionResult,
    ToolExecutorConfig,
)
from .utils import get_file_extension_from_mime_type

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from family_assistant.camera.protocol import CameraBackend
    from family_assistant.events.indexing_source import IndexingSource
    from family_assistant.home_assistant_wrapper import HomeAssistantClientWrapper
    from family_assistant.interfaces import ChatInterface
    from family_assistant.llm.google_types import GeminiProviderMetadata
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.llm.tool_call import ToolCallItem
    from family_assistant.security.taint import (
        TaintMetadata,
        TurnTaintTracker,
    )
    from family_assistant.services.api_backend import ApiBackend
    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.services.tool_call_review import TriggerReviewInput
    from family_assistant.storage.database import Database
    from family_assistant.telegram.protocols import ConfirmationUIManager
    from family_assistant.tools.types import EventSourcesById
    from family_assistant.utils.clock import Clock

    from .attachments import AttachmentProcessor
    from .service import ProcessingService

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class _PrecomputedToolResult:
    """Result returned by a confirmation path that may have run the tool."""

    result: ToolResult | str
    action_attempted: bool


@dataclass(frozen=True)
class _ToolOutput:
    """Rendered tool output: stream payload, LLM message, and attachment IDs.

    ``auto_attachment_ids`` are queued for display in the assistant's reply;
    ``large_result_attachment_ids`` are the auto-converted oversized results,
    which stay out of the display queue because they are working data for the
    model rather than something the user asked to see.
    """

    content_for_stream: str
    llm_message: ToolMessage
    stream_metadata: StreamEventMetadata | None
    auto_attachment_ids: list[str]
    large_result_attachment_ids: list[str]


@contextlib.contextmanager
def _batch_completion(batch: ToolCallBatch | None, call_id: str) -> Iterator[None]:
    """Report this call's completion to its batch however the call ends.

    Siblings that wait for issue order wait on this, so a denied, failed or
    declined call must report too — otherwise it leaves the rest of the batch
    waiting on a call that will never run.
    """
    try:
        yield
    finally:
        if batch is not None:
            batch.mark_done(call_id)


def _argument_attachment_ids(value: object) -> set[str]:
    """Collect attachment-id-shaped strings from a tool-argument structure."""
    found: set[str] = set()
    if isinstance(value, str):
        if is_attachment_id(value):
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            found |= _argument_attachment_ids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _argument_attachment_ids(item)
    return found


class ToolExecutor:
    """Executes individual tool calls with result/error handling."""

    def __init__(
        self,
        tools_provider: ToolsProvider,
        config: ToolExecutorConfig,
        attachment_processor: AttachmentProcessor,
        attachment_registry: AttachmentRegistry | None,
        clock: Clock,
        credential_resolvers: Mapping[str, OAuthCredentialResolver] | None,
        api_backend: ApiBackend | None,
    ) -> None:
        self.tools_provider = tools_provider
        self.config = config
        self.attachment_processor = attachment_processor
        self.attachment_registry = attachment_registry
        self.clock = clock
        self.credential_resolvers = credential_resolvers
        self.api_backend = api_backend

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
        db_context: Database,
        attachment_ids: list[str],
        *,
        acting_user_id: str | None,
    ) -> list[dict[str, str | int | None]]:
        """Fetch metadata for queued attachments to enrich stream output."""
        if not self.attachment_registry:
            raise RuntimeError(
                "attach_to_response metadata enrichment requires AttachmentRegistry"
            )

        attachment_metadata_list: list[dict[str, str | int | None]] = []
        for attachment_id in attachment_ids:
            attachment_info = await self.attachment_registry.get_attachment(
                db_context, attachment_id, acting_user_id=acting_user_id
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
        db_context: Database,
        result_payload: str,
        *,
        acting_user_id: str | None,
    ) -> tuple[list[str] | None, StreamEventMetadata | None]:
        """Build explicit attachment IDs and metadata for attach_to_response output."""
        queued_attachment_ids = self._extract_queued_attachment_ids(result_payload)
        if not queued_attachment_ids:
            return None, None

        attachment_metadata_list = await self._build_attach_to_response_metadata(
            db_context, queued_attachment_ids, acting_user_id=acting_user_id
        )
        logger.info(
            "Enriched attach_to_response result with %d attachment metadata entries",
            len(attachment_metadata_list),
        )
        return queued_attachment_ids, {"attachments": attachment_metadata_list}

    def build_execution_context(
        self,
        *,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        user_id: str | None,
        turn_id: str,
        db_context: Database,
        chat_interface: ChatInterface | None,
        chat_interfaces: dict[str, ChatInterface] | None,
        confirmation_ui_managers: dict[str, ConfirmationUIManager] | None,
        request_confirmation_callback: RequestConfirmationCallback | None,
        subconversation_id: str | None,
        processing_service: ProcessingService | None,
        home_assistant_client: HomeAssistantClientWrapper | None,
        camera_backend: CameraBackend | None,
        event_sources: EventSourcesById | None,
        taint_tracker: TurnTaintTracker | None,
        taint_policy_snapshot: TurnTaintState | None,
        tool_call_review_state: ToolCallReviewTurnState | None,
        tool_call_review_messages: Sequence[LLMMessage] | None,
        tool_call_review_trigger: TriggerReviewInput | None,
        tool_call_id: str | None = None,
        tool_call_batch: ToolCallBatch | None = None,
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
            credential_resolvers=self.credential_resolvers,
            api_backend=self.api_backend,
            visibility_grants=self.config.visibility_grants,
            default_note_visibility_labels=self.config.default_note_visibility_labels,
            required_note_visibility_labels=self.config.required_note_visibility_labels,
            allowed_note_visibility_labels=self.config.allowed_note_visibility_labels,
            allow_wake_llm=self.config.allow_wake_llm,
            note_registry=self.config.note_registry,
            taint_tracker=taint_tracker,
            taint_policy_snapshot=taint_policy_snapshot,
            tool_call_review_state=(
                tool_call_review_state
                if tool_call_review_state is not None
                else ToolCallReviewTurnState()
            ),
            tool_call_review_messages=tool_call_review_messages,
            tool_call_review_trigger=tool_call_review_trigger,
            tool_call_id=tool_call_id,
            tool_call_batch=tool_call_batch,
        )

    @staticmethod
    def _merge_safety_acknowledgement(result: ToolResult) -> None:
        """Merge safety_acknowledgement into a successful tool result.

        Modifies the result in-place to include the safety acknowledgement in
        the JSON data that will be parsed by the LLM provider. The Gemini
        protocol expects the JSON boolean ``true``.
        """
        if result.data is None:
            if result.text is not None:
                result.data = {
                    "result": result.text,
                    "safety_acknowledgement": True,
                }
                result.text = None
        elif isinstance(result.data, dict):
            result.data = {**result.data, "safety_acknowledgement": True}

    async def _handle_safety_confirmation(
        self,
        *,
        call_id: str,
        function_name: str,
        safety_decision: dict[str, object],
        arguments: dict[str, object],
        request_confirmation_callback: RequestConfirmationCallback,
        tool_execution_context: ToolExecutionContext,
        taint_metadata: TaintMetadata,
    ) -> ToolExecutionResult | _PrecomputedToolResult | None:
        """Handle safety confirmation for a tool call.

        Returns:
            ``None`` when approval was granted and the caller should execute
            the tool; a ``_PrecomputedToolResult`` when a durable confirmation
            returned a result, carrying whether the action was actually
            attempted; a ``ToolExecutionResult`` for declined / timed-out /
            failed confirmations where the action never ran.
        """
        logger.info(
            "Tool '%s' requires safety confirmation: %s",
            function_name,
            safety_decision.get("explanation"),
        )

        tool_args_with_safety = {
            **arguments,
            "safety_decision": safety_decision,
        }

        try:
            confirmation_result = await request_confirmation_callback(
                interface_type=tool_execution_context.interface_type,
                conversation_id=tool_execution_context.conversation_id,
                turn_id=tool_execution_context.turn_id,
                tool_name=function_name,
                call_id=call_id,
                tool_args=tool_args_with_safety,
                timeout_seconds=self.config.tools_config.confirmation_timeout_seconds,
                context=tool_execution_context,
            )
        except TimeoutError:
            # A timed-out confirmation is an expected outcome: the action is
            # simply not taken. Any other failure (programming errors,
            # interface errors, cancellation) propagates per fail-fast policy.
            logger.warning(
                "Safety confirmation for tool '%s' timed out.",
                function_name,
            )
            error_msg = f"Action cancelled: Safety confirmation for tool '{function_name}' timed out."
            return self._build_error_result(
                call_id=call_id,
                function_name=function_name,
                error_content=error_msg,
                error_traceback="",
                taint_metadata=taint_metadata,
            )

        if confirmation_result.kind == "approved":
            return None

        result_taint_metadata = taint_metadata
        if confirmation_result.taint_metadata is not None:
            result_taint_metadata = confirmation_result.taint_metadata
            tool_execution_context.tool_result_taint_metadata[call_id] = (
                confirmation_result.taint_metadata
            )
            if tool_execution_context.taint_tracker is not None:
                merge_taint_state_into_tracker(
                    tool_execution_context.taint_tracker,
                    TurnTaintState.from_metadata(confirmation_result.taint_metadata),
                )

        if confirmation_result.kind == "completed":
            # A durable confirmation normally completed after the task worker
            # attempted the tool. A deferred callback can also return a queued
            # placeholder explicitly marked action_attempted=False.
            return _PrecomputedToolResult(
                result=confirmation_outcome_to_tool_result(
                    name=function_name,
                    outcome=confirmation_result,
                ),
                action_attempted=confirmation_result.action_attempted,
            )

        logger.info(
            "Safety confirmation for tool '%s' was not approved: %s",
            function_name,
            confirmation_result.kind,
        )
        outcome_result = confirmation_outcome_to_tool_result(
            name=function_name,
            outcome=confirmation_result,
        )
        result_content = (
            outcome_result
            if isinstance(outcome_result, str)
            else outcome_result.get_text()
        )
        if confirmation_result.kind == "failed":
            # "failed" means the user approved and the durable execution was
            # attempted but errored — like a local post-approval failure, the
            # acknowledgement must accompany the error so the model can move
            # past the safety gate.
            result_content = json.dumps({
                "error": result_content,
                "safety_acknowledgement": True,
            })
        return ToolExecutionResult(
            stream_event=LLMStreamEvent(
                type="tool_result",
                tool_call_id=call_id,
                tool_result=result_content,
            ),
            llm_message=ToolMessage(
                tool_call_id=call_id,
                content=result_content,
                name=function_name,
                taint_metadata=result_taint_metadata,
            ),
            auto_attachment_ids=None,
            explicit_attachment_ids=None,
        )

    @staticmethod
    def _build_error_result(
        *,
        call_id: str,
        function_name: str,
        error_content: str,
        error_traceback: str,
        taint_metadata: TaintMetadata,
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
                taint_metadata=taint_metadata,
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
        # Not counted here: MeteredToolsProvider wraps the provider itself, so
        # every entry path is counted once, including the ones that never reach
        # this executor.
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
                taint_metadata=(
                    tool_execution_context.taint_tracker.snapshot().to_metadata()
                    if tool_execution_context.taint_tracker is not None
                    else TurnTaintState.empty().to_metadata()
                ),
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
                taint_metadata=(
                    tool_execution_context.taint_tracker.snapshot().to_metadata()
                    if tool_execution_context.taint_tracker is not None
                    else TurnTaintState.empty().to_metadata()
                ),
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
                taint_metadata=(
                    tool_execution_context.taint_tracker.snapshot().to_metadata()
                    if tool_execution_context.taint_tracker is not None
                    else TurnTaintState.empty().to_metadata()
                ),
            )

    @staticmethod
    def _parse_arguments(
        function_name: str,
        function_args: object,
    ) -> dict[str, object]:
        """Parse tool-call arguments and enforce object shape.

        Always returns a fresh dict: when a provider hands arguments over as a
        dict it is the SAME object stored on the ToolCallItem, and the caller
        mutates the returned dict (e.g. popping computer-use safety_decision).
        Mutating in place would silently rewrite the assistant message that is
        replayed to the LLM on the next iteration.
        """
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

        return dict(cast("dict[str, object]", arguments))

    async def _large_result_owner(
        self,
        function_name: str,
        acting_user_id: str | None,
        *,
        arguments: dict[str, object] | None,
        db_context: Database,
    ) -> str | None:
        """Owner for a large-result auto-conversion.

        Personal-data tools (descriptor tag ``connected_account_data``) own their
        auto-converted large results. Derived results inherit ownership too: a
        helper such as ``jq_query`` or ``execute_script`` run over an
        owner-scoped attachment must not launder its content into an ownerless
        attachment, so when any attachment referenced in the tool's arguments
        is owned, the conversion is owned by the acting user (the only actor
        for whom the owned input was readable). Every other tool's large
        result stays ownerless, preserving prior behavior.
        """
        if acting_user_id is None:
            return None
        if isinstance(self.tools_provider, ToolDescriptorProvider):
            descriptor = await self.tools_provider.get_tool_descriptor(function_name)
            if descriptor is not None and any(
                tag.value == "connected_account_data" for tag in descriptor.tags
            ):
                return acting_user_id
        if arguments and self.attachment_registry is not None:
            candidate_ids = _argument_attachment_ids(arguments)
            if candidate_ids:
                metadata_by_id = await self.attachment_registry.get_attachments(
                    db_context,
                    sorted(candidate_ids),
                    acting_user_id=acting_user_id,
                )
                if any(
                    metadata.owner_user_id is not None
                    for metadata in metadata_by_id.values()
                ):
                    return acting_user_id
        return None

    async def _handle_large_text_result(
        self,
        *,
        db_context: Database,
        content: str,
        function_name: str,
        conversation_id: str,
        call_id: str,
        taint_metadata: TaintMetadata | None,
        acting_user_id: str | None,
        arguments: dict[str, object] | None,
    ) -> tuple[str, list[str]]:
        """Convert oversized text results into attachment references."""
        owner_user_id = await self._large_result_owner(
            function_name, acting_user_id, arguments=arguments, db_context=db_context
        )
        (
            new_content,
            auto_attachment_id,
        ) = await self.attachment_processor.handle_large_result(
            db_context,
            content,
            function_name,
            conversation_id,
            call_id,
            taint_metadata,
            owner_user_id=owner_user_id,
        )
        if auto_attachment_id is None:
            return new_content, []
        return new_content, [auto_attachment_id]

    async def _process_tool_attachments(
        self,
        *,
        db_context: Database,
        attachments: list[ToolAttachment],
        function_name: str,
        conversation_id: str,
        call_id: str,
        taint_metadata: TaintMetadata | None,
        acting_user_id: str | None,
        arguments: dict[str, object] | None,
    ) -> tuple[list[dict[str, str | int | None]], list[str]]:
        """Store/normalize ToolResult attachments for streaming and history."""
        attachments_data: list[dict[str, str | int | None]] = []
        auto_attachment_ids: list[str] = []
        owner_user_id = await self._large_result_owner(
            function_name, acting_user_id, arguments=arguments, db_context=db_context
        )

        for attachment in attachments:
            attachment_data: dict[str, str | int | None] = {
                "type": "tool_result",
                "mime_type": attachment.mime_type,
                "description": attachment.description,
            }

            if attachment.content and self.attachment_registry:
                file_extension = get_file_extension_from_mime_type(attachment.mime_type)
                metadata: dict[str, object] = {
                    "tool_call_id": call_id,
                    "auto_display": True,
                }
                if taint_metadata is not None:
                    metadata["taint_metadata"] = taint_metadata

                registered_metadata = (
                    await self.attachment_registry.store_and_register_tool_attachment(
                        file_content=attachment.content,
                        filename=f"tool_result_{uuid.uuid4()}{file_extension}",
                        content_type=attachment.mime_type,
                        tool_name=function_name,
                        description=attachment.description
                        or f"Output from {function_name}",
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                        metadata=metadata,
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
        db_context: Database,
        result: ToolResult,
        function_name: str,
        conversation_id: str,
        call_id: str,
        provider_metadata: GeminiProviderMetadata | ProviderMetadataDict | None,
        taint_metadata: TaintMetadata | None,
        acting_user_id: str | None,
        arguments: dict[str, object] | None,
    ) -> _ToolOutput:
        """Convert ToolResult into stream payload, message, and attachment IDs."""
        content_for_stream = result.get_text()
        (
            content_for_stream,
            large_result_attachment_ids,
        ) = await self._handle_large_text_result(
            db_context=db_context,
            content=content_for_stream,
            function_name=function_name,
            conversation_id=conversation_id,
            call_id=call_id,
            taint_metadata=taint_metadata,
            acting_user_id=acting_user_id,
            arguments=arguments,
        )
        auto_attachment_ids: list[str] = []
        if large_result_attachment_ids:
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
                taint_metadata=taint_metadata,
                acting_user_id=acting_user_id,
                arguments=arguments,
            )
            auto_attachment_ids.extend(new_attachment_ids)

        llm_message = tool_result_to_llm_message(
            result,
            call_id,
            function_name,
            provider_metadata=provider_metadata,
            taint_metadata=taint_metadata,
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

        return _ToolOutput(
            content_for_stream=content_for_stream,
            llm_message=llm_message,
            stream_metadata=stream_metadata,
            auto_attachment_ids=auto_attachment_ids,
            large_result_attachment_ids=large_result_attachment_ids,
        )

    async def _build_output_for_string_result(
        self,
        *,
        db_context: Database,
        result: object,
        function_name: str,
        conversation_id: str,
        call_id: str,
        taint_metadata: TaintMetadata | None,
        acting_user_id: str | None,
        arguments: dict[str, object] | None,
    ) -> _ToolOutput:
        """Convert plain string-like tool output into stream/message payload."""
        content_for_stream = str(result)
        (
            content_for_stream,
            large_result_attachment_ids,
        ) = await self._handle_large_text_result(
            db_context=db_context,
            content=content_for_stream,
            function_name=function_name,
            conversation_id=conversation_id,
            call_id=call_id,
            taint_metadata=taint_metadata,
            acting_user_id=acting_user_id,
            arguments=arguments,
        )
        return _ToolOutput(
            content_for_stream=content_for_stream,
            llm_message=ToolMessage(
                tool_call_id=call_id,
                content=content_for_stream,
                name=function_name,
            ),
            stream_metadata=None,
            auto_attachment_ids=[],
            large_result_attachment_ids=large_result_attachment_ids,
        )

    async def execute(
        self,
        tool_call_item_obj: ToolCallItem,
        *,
        interface_type: str,
        conversation_id: str,
        user_name: str,
        turn_id: str,
        db_context: Database,
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
        taint_tracker: TurnTaintTracker | None = None,
        taint_policy_snapshot: TurnTaintState | None = None,
        tool_call_review_state: ToolCallReviewTurnState | None = None,
        tool_call_review_messages: Sequence[LLMMessage] | None = None,
        tool_call_review_trigger: TriggerReviewInput | None = None,
        tool_call_batch: ToolCallBatch | None = None,
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
        initial_taint_metadata = (
            taint_tracker.snapshot().to_metadata()
            if taint_tracker is not None
            else TurnTaintState.empty().to_metadata()
        )

        with (
            _batch_completion(tool_call_batch, call_id),
            tracer.start_as_current_span(
                f"tool.execute.{function_name}",
                attributes={
                    "tool.name": function_name,
                    "tool.call_id": call_id,
                },
            ) as span,
        ):
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
                    taint_metadata=initial_taint_metadata,
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
                    taint_metadata=initial_taint_metadata,
                )

            # Gemini computer-use models may attach a safety_decision to the
            # arguments of computer-use action calls; it is always popped for
            # those calls since the tool signatures don't accept it. The key is
            # interpreted ONLY for the computer-use action space — other tools
            # may legitimately define a parameter with this name. Only an
            # absent safety_decision, an explicit allow, or a user-approved
            # require_confirmation may execute; anything else ("blocked",
            # unknown values, malformed payloads, a dict missing its decision)
            # is refused outright — safety decisions fail closed.
            safety_decision = (
                arguments.pop("safety_decision", None)
                if function_name in COMPUTER_USE_FUNCTION_NAMES
                else None
            )
            decision_value = (
                safety_decision.get("decision")
                if isinstance(safety_decision, dict)
                else None
            )
            safety_confirmation_required = decision_value == "require_confirmation"
            safety_decision_permits_execution = safety_decision is None or (
                isinstance(safety_decision, dict)
                and decision_value in {"allowed", "allow", "regular"}
            )
            if (
                not safety_confirmation_required
                and not safety_decision_permits_execution
            ):
                logger.warning(
                    "Tool '%s' refused: unrecognized safety decision %r.",
                    function_name,
                    safety_decision,
                )
                explanation = (
                    safety_decision.get("explanation", "")
                    if isinstance(safety_decision, dict)
                    else ""
                )
                return self._build_error_result(
                    call_id=call_id,
                    function_name=function_name,
                    error_content=(
                        f"Action not executed: the safety decision attached to "
                        f"'{function_name}' was {decision_value!r}, which does not "
                        f"permit execution. {explanation}".rstrip()
                    ),
                    error_traceback="",
                    taint_metadata=initial_taint_metadata,
                )

            logger.info(
                "Executing tool '%s' with argument keys: %s",
                function_name,
                sorted(arguments.keys()),
            )

            tool_execution_context = self.build_execution_context(
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
                tool_call_review_state=tool_call_review_state,
                tool_call_review_messages=tool_call_review_messages,
                tool_call_review_trigger=tool_call_review_trigger,
                tool_call_id=call_id,
                tool_call_batch=tool_call_batch,
            )

            # Result of a durable "completed" confirmation that already
            # executed the tool elsewhere; substitutes for local execution.
            precomputed_result: _PrecomputedToolResult | None = None
            if safety_confirmation_required and isinstance(safety_decision, dict):
                if not request_confirmation_callback:
                    logger.warning(
                        "Tool '%s' requires safety confirmation but no callback available.",
                        function_name,
                    )
                    error_content = (
                        f"Action cannot be executed: Tool '{function_name}' requires safety confirmation, "
                        "but this interface does not support confirmations. "
                        f"Explanation: {safety_decision.get('explanation', 'No explanation provided')}"
                    )
                    return self._build_error_result(
                        call_id=call_id,
                        function_name=function_name,
                        error_content=error_content,
                        error_traceback="",
                        taint_metadata=initial_taint_metadata,
                    )

                # Refuse when the confirmation prompt could not show the
                # approver the full payload (same rule as policy confirms).
                block_reason = confirmation_payload_block_reason(
                    function_name, arguments
                )
                if block_reason is not None:
                    logger.info(
                        "Refusing safety-gated tool '%s': %s",
                        function_name,
                        block_reason,
                    )
                    return self._build_error_result(
                        call_id=call_id,
                        function_name=function_name,
                        error_content=block_reason,
                        error_traceback="",
                        taint_metadata=initial_taint_metadata,
                    )

                confirmation_result = await self._handle_safety_confirmation(
                    call_id=call_id,
                    function_name=function_name,
                    safety_decision=safety_decision,
                    arguments=arguments,
                    request_confirmation_callback=request_confirmation_callback,
                    tool_execution_context=tool_execution_context,
                    taint_metadata=initial_taint_metadata,
                )
                if isinstance(confirmation_result, ToolExecutionResult):
                    return confirmation_result
                if confirmation_result is not None:
                    precomputed_result = confirmation_result

            # The acknowledgement asserts to Gemini that the user consented AND
            # the action ran (or was attempted). That holds for local execution
            # after a live approval, and for a durable confirmation explicitly
            # marked as attempted. Deferred-pending placeholders are marked
            # action_attempted=False and must NOT claim consent.
            acknowledge_safety = False
            result_or_error: ToolResult | object | ToolExecutionResult
            if precomputed_result is not None:
                result_or_error = precomputed_result.result
                acknowledge_safety = precomputed_result.action_attempted
            else:
                if safety_confirmation_required:
                    acknowledge_safety = True
                result_or_error = await self._execute_tool_with_error_mapping(
                    function_name=function_name,
                    arguments=arguments,
                    tool_execution_context=tool_execution_context,
                    call_id=call_id,
                    span=span,
                )
            if isinstance(result_or_error, ToolExecutionResult):
                if acknowledge_safety:
                    # The user approved the safety-gated action, so the
                    # acknowledgement must reach the API even when the attempt
                    # then failed — otherwise the model cannot process the
                    # error and move past the safety gate.
                    error_payload = json.dumps({
                        "error": result_or_error.llm_message.content,
                        "safety_acknowledgement": True,
                    })
                    result_or_error.llm_message = (
                        result_or_error.llm_message.model_copy(
                            update={"content": error_payload}
                        )
                    )
                return result_or_error

            # Post-execution processing failures (attachment IO/enrichment/metadata
            # handling) should fail fast and propagate to callers.
            result = result_or_error
            explicit_attachment_ids: list[str] | None = None

            if acknowledge_safety and not isinstance(result, ToolResult):
                # String results have no data dict to carry the acknowledgement;
                # wrap them so the approved action is acknowledged to the API.
                result = ToolResult(data={"result": str(result)})

            if isinstance(result, ToolResult):
                if acknowledge_safety:
                    self._merge_safety_acknowledgement(result)
                result_taint_metadata = (
                    tool_execution_context.tool_result_taint_metadata.get(call_id)
                )
                output = await self._build_output_for_tool_result(
                    db_context=db_context,
                    result=result,
                    function_name=function_name,
                    conversation_id=conversation_id,
                    call_id=call_id,
                    provider_metadata=tool_call_item_obj.provider_metadata,
                    taint_metadata=result_taint_metadata,
                    acting_user_id=user_id,
                    arguments=arguments,
                )
            else:
                result_taint_metadata = (
                    tool_execution_context.tool_result_taint_metadata.get(call_id)
                )
                output = await self._build_output_for_string_result(
                    db_context=db_context,
                    result=result,
                    function_name=function_name,
                    conversation_id=conversation_id,
                    call_id=call_id,
                    taint_metadata=result_taint_metadata,
                    acting_user_id=user_id,
                    arguments=arguments,
                )
                if result_taint_metadata is not None:
                    output = replace(
                        output,
                        llm_message=output.llm_message.model_copy(
                            update={"taint_metadata": result_taint_metadata}
                        ),
                    )

            content_for_stream = output.content_for_stream
            llm_message = output.llm_message
            stream_metadata = output.stream_metadata
            auto_attachment_ids = output.auto_attachment_ids

            if function_name == "attach_to_response":
                (
                    explicit_attachment_ids,
                    explicit_stream_metadata,
                ) = await self._build_attach_to_response_output(
                    db_context, content_for_stream, acting_user_id=user_id
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
                large_result_attachment_ids=output.large_result_attachment_ids or None,
                explicit_attachment_ids=explicit_attachment_ids,
            )
