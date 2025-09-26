"""Attachment manipulation tools for LLM usage."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
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
                        "items": {"type": "attachment"},
                        "description": "List of attachment IDs to include with this response",
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
    attachment_ids: list[ScriptAttachment],
) -> str:
    """
    Attach files/images to the current LLM response.

    This tool signals to the processing service that the specified attachments
    should be sent along with the current response to the user.

    Args:
        exec_context: The execution context
        attachment_ids: List of ScriptAttachment objects to attach to the response

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

    # Extract attachment IDs from ScriptAttachment objects
    validated_ids = []
    for attachment in attachment_ids:
        try:
            # Get the attachment ID from the ScriptAttachment object
            attachment_id = attachment.get_id()

            # Basic validation - the ScriptAttachment object already validates access
            # when it was created, so we just need to extract the ID
            validated_ids.append(attachment_id)
            logger.debug(
                f"Added attachment {attachment_id}: {attachment.get_description()}"
            )

        except Exception as e:
            logger.error(f"Error processing attachment object: {e}")
            continue

    if not validated_ids:
        return json.dumps({"status": "error", "message": "No valid attachments found"})

    logger.info(
        f"Successfully processed {len(validated_ids)} attachment(s) for response"
    )

    # Return JSON with a clear status message that the LLM can understand
    attachment_word = "attachment" if len(validated_ids) == 1 else "attachments"
    return json.dumps({
        "status": "attachments_queued",
        "attachment_ids": validated_ids,
        "count": len(validated_ids),
        "message": f"Successfully attached {len(validated_ids)} {attachment_word} to this response. The {attachment_word} will be sent with this message. No further action needed.",
    })
