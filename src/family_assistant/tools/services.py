"""Service delegation tools.

This module contains tools for delegating requests to other specialized
assistant profiles (services).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from family_assistant.llm.content_parts import text_content
from family_assistant.tools.types import (
    ConfirmationOutcome,
    ToolAttachment,
    ToolDefinition,
    ToolResult,
)

if TYPE_CHECKING:
    from family_assistant.llm.content_parts import ContentPartDict
    from family_assistant.tools.types import ToolExecutionContext


logger = logging.getLogger(__name__)


def _delegation_confirmation_outcome_result(
    target_service_id: str,
    outcome: ConfirmationOutcome,
) -> ToolResult:
    """Convert a durable delegation confirmation outcome into a tool result."""
    if outcome.kind == "approved":
        return ToolResult(
            text=(
                f"Error: Confirmation for delegation to service "
                f"'{target_service_id}' was approved but not executed."
            ),
            attachments=None,
        )
    if outcome.kind == "completed":
        if isinstance(outcome.result, ToolResult):
            return outcome.result
        return ToolResult(text=str(outcome.result or ""), attachments=None)
    if outcome.kind == "failed":
        if isinstance(outcome.result, ToolResult):
            return outcome.result
        return ToolResult(
            text=str(
                outcome.result
                or f"Error: Failed to delegate task to service '{target_service_id}'."
            ),
            attachments=None,
        )
    if outcome.kind == "timed_out":
        return ToolResult(
            text=f"Error: Confirmation timed out for delegating to '{target_service_id}'.",
            attachments=None,
        )
    return ToolResult(
        text=f"OK. Delegation to service '{target_service_id}' cancelled by user.",
        attachments=None,
    )


# Tool Definitions
SERVICE_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "delegate_to_service",
            "description": (
                "Delegates a specific user request to another specialized assistant profile (service) "
                "that might have different tools or capabilities. Use this if the main assistant "
                "cannot handle a request directly or if a specialized profile is more appropriate "
                "for the task. Profile-to-profile delegation controls are enforced by the "
                "tool policy engine.\n\n"
                "Available service profiles:\n{available_service_profiles_with_descriptions}\n\n"
                "Returns: A string containing the delegated service's response or an error message. "
                "On successful delegation, returns the text response from the target service. "
                "If service returns no text, returns 'Service [id] processed the request but provided no textual response.'. "
                "If service registry unavailable, returns 'Error: Service registry is not available to delegate the task.'. "
                "If target service not found, returns 'Error: Target service profile [id] not found.'. "
                "If delegation blocked by security policy, returns 'Error: Tool \\'delegate_to_service\\' is not allowed. [reason]'. "
                "If confirmation required but unavailable, returns 'Error: Confirmation required to delegate to [id], but no confirmation mechanism is available.'. "
                "If user cancels confirmation, returns 'OK. Delegation to service [id] cancelled by user.'. "
                "If confirmation times out, returns 'Error: Confirmation timed out for delegating to [id].'. "
                "On delegation error, returns 'Error: Failed to delegate task to service [id]. Details: [error]' or 'Error from [id] service: An error occurred during processing.'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_service_id": {
                        "type": "string",
                        "description": "The unique ID of the target service profile to delegate the request to (e.g., 'browser_profile').",
                    },
                    "user_request": {
                        "type": "string",
                        "description": "The specific request, question, or prompt to be processed by the target service.",
                    },
                    "confirm_delegation": {
                        "type": "boolean",
                        "description": "Optional. If true, explicitly ask the user for confirmation before delegating the task. Defaults to false. Policy-level confirmation requirements may also apply.",
                        "default": False,
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of attachment UUIDs to include with the delegated request. These attachments must be accessible in the current conversation and will be passed to the target service for processing.",
                    },
                },
                "required": ["target_service_id", "user_request"],
            },
        },
    },
]


# Tool Implementations
async def delegate_to_service_tool(
    exec_context: ToolExecutionContext,
    target_service_id: str,
    user_request: str,
    confirm_delegation: bool = False,
    attachment_ids: list[str] | None = None,
) -> ToolResult:
    """
    Delegates a user request to another specialized assistant profile (service).

    Args:
        exec_context: The execution context
        target_service_id: ID of the target service profile to delegate to
        user_request: The request text to delegate
        confirm_delegation: Whether to ask for user confirmation
        attachment_ids: Optional list of attachment UUIDs to include with the request

    Returns:
        ToolResult with response text from the target service and any attachments it generated
    """
    logger.info(
        f"Executing delegate_to_service_tool: target='{target_service_id}', request='{user_request[:50]}...', confirm={confirm_delegation}"
    )

    if (
        not exec_context.processing_service
        or not exec_context.processing_service.processing_services_registry
    ):
        logger.error(
            "Processing services registry not available in the current execution context."
        )
        return ToolResult(
            text="Error: Service registry is not available to delegate the task.",
            attachments=None,
        )

    registry = exec_context.processing_service.processing_services_registry
    target_service = registry.get(target_service_id)

    if not target_service:
        logger.error(
            f"Target service profile ID '{target_service_id}' not found in the registry."
        )
        return ToolResult(
            text=f"Error: Target service profile '{target_service_id}' not found.",
            attachments=None,
        )

    confirmation_timeout_seconds = exec_context.processing_service.service_config.tools_config.confirmation_timeout_seconds
    actual_confirm_delegation = confirm_delegation

    if actual_confirm_delegation:
        if not exec_context.request_confirmation_callback:
            logger.error(
                f"Confirmation required for delegating to '{target_service_id}', but no confirmation callback is available. Aborting delegation."
            )
            return ToolResult(
                text=f"Error: Confirmation required to delegate to '{target_service_id}', but no confirmation mechanism is available.",
                attachments=None,
            )
        else:
            try:
                confirmation_outcome = await exec_context.request_confirmation_callback(
                    interface_type=exec_context.interface_type,
                    conversation_id=exec_context.conversation_id,
                    turn_id=exec_context.turn_id,
                    tool_name="delegate_to_service",
                    call_id=f"delegate_to_service_{uuid.uuid4()}",
                    tool_args={
                        "target_service_id": target_service_id,
                        "user_request": user_request,
                        "confirm_delegation": actual_confirm_delegation,
                        **(
                            {"attachment_ids": attachment_ids}
                            if attachment_ids is not None
                            else {}
                        ),
                    },
                    timeout_seconds=confirmation_timeout_seconds,
                    context=exec_context,
                )
                if confirmation_outcome.kind != "approved":
                    return _delegation_confirmation_outcome_result(
                        target_service_id,
                        confirmation_outcome,
                    )
            except TimeoutError:
                logger.warning(
                    f"Confirmation for delegating to '{target_service_id}' timed out."
                )
                return ToolResult(
                    text=f"Error: Confirmation timed out for delegating to '{target_service_id}'.",
                    attachments=None,
                )
            except Exception as e:
                logger.error(
                    f"Error during confirmation for delegating to '{target_service_id}': {e}",
                    exc_info=True,
                )
                return ToolResult(
                    text=f"Error during confirmation for delegating to '{target_service_id}': {e}",
                    attachments=None,
                )

    # Process attachments if provided
    content_parts: list[ContentPartDict] = [text_content(user_request)]

    if attachment_ids:
        if not exec_context.attachment_registry:
            logger.warning(
                "Attachment IDs provided but AttachmentRegistry not available - ignoring attachments"
            )
        else:
            attachment_registry = exec_context.attachment_registry

            for attachment_id in attachment_ids:
                try:
                    # Validate attachment exists and is accessible
                    attachment = await attachment_registry.get_attachment(
                        exec_context.db_context, attachment_id
                    )

                    if not attachment:
                        logger.warning(
                            f"Attachment {attachment_id} not found - skipping"
                        )
                        continue

                    # Add attachment content part
                    content_parts.append({
                        "type": "attachment",
                        "attachment_id": attachment_id,
                    })
                    logger.debug(f"Added attachment {attachment_id} to delegation")

                except Exception as e:
                    logger.error(f"Error validating attachment {attachment_id}: {e}")
                    continue

    # Generate a unique subconversation ID for this delegation
    # This isolates the delegated conversation's history from the main conversation
    subconversation_id = str(uuid.uuid4())

    logger.info(
        f"Delegating request to service profile: '{target_service_id}' with {len(content_parts)} content parts (subconversation_id={subconversation_id})"
    )
    try:
        result = await target_service.handle_chat_interaction(
            db_context=exec_context.db_context,
            interface_type=exec_context.interface_type,  # Use current interface type
            conversation_id=exec_context.conversation_id,  # Use current conversation ID
            trigger_content_parts=content_parts,
            trigger_interface_message_id=None,  # This is an internal trigger
            user_name=exec_context.user_name,  # Pass original user's name
            replied_to_interface_id=None,
            chat_interface=exec_context.chat_interface,  # Pass through for nested actions
            chat_interfaces=exec_context.chat_interfaces,
            confirmation_ui_managers=exec_context.confirmation_ui_managers,
            request_confirmation_callback=exec_context.request_confirmation_callback,  # Pass through
            subconversation_id=subconversation_id,  # Pass subconversation ID for isolation
        )

        final_text_reply = result.text_reply
        _final_assistant_message_id = result.assistant_message_internal_id  # Ignored
        _final_reasoning_info = result.reasoning_info  # Ignored
        error_traceback = result.error_traceback
        response_attachment_ids = result.attachment_ids or []

        if error_traceback:
            logger.error(
                f"Delegated service '{target_service_id}' returned an error: {error_traceback}"
            )
            return ToolResult(
                text=f"Error from '{target_service_id}' service: An error occurred during processing.",
                attachments=None,
            )
        if not final_text_reply:
            logger.info(
                f"Delegated service '{target_service_id}' returned no textual reply."
            )
            return ToolResult(
                text=f"Service '{target_service_id}' processed the request but provided no textual response.",
                attachments=None,
            )

        logger.info(
            f"Received reply from delegated service '{target_service_id}': '{final_text_reply[:100]}...'"
        )

        # Create attachment references for any attachments from the delegated service
        delegated_attachments = None
        if response_attachment_ids and exec_context.attachment_registry:
            delegated_attachments = []
            for att_id in response_attachment_ids:
                try:
                    # Fetch attachment metadata to get the correct MIME type
                    att_metadata = (
                        await exec_context.attachment_registry.get_attachment(
                            exec_context.db_context, att_id
                        )
                    )
                    if att_metadata:
                        delegated_attachments.append(
                            ToolAttachment(
                                mime_type=att_metadata.mime_type,
                                content=None,  # Reference only, no content
                                attachment_id=att_id,
                                description=att_metadata.description
                                or f"Attachment from delegated service '{target_service_id}'",
                            )
                        )
                    else:
                        logger.warning(
                            f"Could not fetch metadata for attachment {att_id}, using fallback"
                        )
                        delegated_attachments.append(
                            ToolAttachment(
                                mime_type="application/octet-stream",  # Fallback for missing metadata
                                content=None,
                                attachment_id=att_id,
                                description=f"Attachment from delegated service '{target_service_id}'",
                            )
                        )
                except Exception as e:
                    logger.error(
                        f"Error fetching attachment metadata for {att_id}: {e}",
                        exc_info=True,
                    )
                    # Still include the attachment with fallback type
                    delegated_attachments.append(
                        ToolAttachment(
                            mime_type="application/octet-stream",
                            content=None,
                            attachment_id=att_id,
                            description=f"Attachment from delegated service '{target_service_id}'",
                        )
                    )
            logger.info(
                f"Propagating {len(delegated_attachments)} attachment(s) from delegated service"
            )

        return ToolResult(text=final_text_reply, attachments=delegated_attachments)

    except Exception as e:
        logger.error(
            f"Failed to delegate request to service '{target_service_id}': {e}",
            exc_info=True,
        )
        return ToolResult(
            text=f"Error: Failed to delegate task to service '{target_service_id}'. Details: {e}",
            attachments=None,
        )
