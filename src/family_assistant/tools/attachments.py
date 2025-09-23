"""Attachment manipulation tools for LLM usage."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.services.attachment_registry import AttachmentRegistry

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
ATTACHMENT_TOOLS_DEFINITION: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "attach_to_response",
            "description": (
                "Attach files/images to your current response being sent to the user. "
                "The attachments will be sent along with your text response. "
                "Only use this with attachment IDs that are accessible in the current conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of attachment UUIDs to include with this response",
                    }
                },
                "required": ["attachment_ids"],
            },
        },
    }
]


# Tool Implementations
async def attach_to_response_tool(
    exec_context: ToolExecutionContext,
    attachment_ids: list[str],
) -> str:
    """
    Attach files/images to the current LLM response.

    This tool signals to the processing service that the specified attachments
    should be sent along with the current response to the user.

    Args:
        exec_context: The execution context
        attachment_ids: List of attachment UUIDs to attach to the response

    Returns:
        A JSON string indicating the attachments have been queued
    """

    logger.info(
        f"Executing attach_to_response_tool with {len(attachment_ids)} attachment(s)"
    )

    # Validate that we have attachment service
    if not exec_context.attachment_service:
        logger.error("AttachmentService not available in execution context")
        return json.dumps({
            "status": "error",
            "message": "AttachmentService not available",
        })

    # Create attachment registry
    attachment_registry = AttachmentRegistry(exec_context.attachment_service)

    # Validate attachment IDs exist and are accessible
    validated_ids = []
    for attachment_id in attachment_ids:
        try:
            attachment = await attachment_registry.get_attachment(
                exec_context.db_context, attachment_id
            )

            if not attachment:
                logger.warning(f"Attachment {attachment_id} not found")
                continue

            # Check conversation scoping
            if (
                exec_context.conversation_id
                and attachment.conversation_id != exec_context.conversation_id
            ):
                logger.warning(
                    f"Attachment {attachment_id} not accessible from conversation {exec_context.conversation_id}"
                )
                continue

            validated_ids.append(attachment_id)
            logger.debug(
                f"Validated attachment {attachment_id}: {attachment.description}"
            )

        except Exception as e:
            logger.error(f"Error validating attachment {attachment_id}: {e}")
            continue

    if not validated_ids:
        return json.dumps({"status": "error", "message": "No valid attachments found"})

    logger.info(
        f"Successfully validated {len(validated_ids)} attachment(s) for response"
    )

    # Return the validated attachment IDs for the processing service to capture
    return json.dumps({
        "status": "attachments_queued",
        "attachment_ids": validated_ids,
        "count": len(validated_ids),
    })
