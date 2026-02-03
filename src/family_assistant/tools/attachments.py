"""Attachment manipulation tools for LLM usage."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from family_assistant.tools.types import ToolResult

if TYPE_CHECKING:
    from family_assistant.scripting.apis.attachments import ScriptAttachment
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


# Tool Definitions
ATTACHMENT_TOOLS_DEFINITION: list[ToolDefinition] = [
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
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_attachment",
            "description": (
                "Read text content from an attachment with optional filtering and pagination. "
                "Use this to explore large text files or tool results that were auto-converted to attachments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the text attachment to read.",
                    },
                    "grep": {
                        "type": "string",
                        "description": "Optional substring to filter lines (case-insensitive).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of lines to skip from the beginning (default 0).",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return (default 100).",
                        "default": 100,
                    },
                },
                "required": ["attachment_id"],
            },
        },
    },
]


# Tool Implementations
async def read_text_attachment_tool(
    exec_context: ToolExecutionContext,
    attachment_id: ScriptAttachment | str,
    grep: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> ToolResult:
    """
    Read text from an attachment with optional filtering and pagination.

    Args:
        exec_context: The execution context
        attachment_id: UUID of the attachment or ScriptAttachment object
        grep: Optional string to filter lines (case-insensitive)
        offset: Number of lines to skip (default 0)
        limit: Maximum number of lines to return (default 100)

    Returns:
        ToolResult with the text content and metadata
    """
    # Extract ID from ScriptAttachment if needed
    if not isinstance(attachment_id, str):
        # ScriptAttachment object (from script contexts)
        attachment_id_str = attachment_id.get_id()
    else:
        attachment_id_str = attachment_id

    logger.info(
        f"Reading text attachment {attachment_id_str} (grep={grep}, offset={offset}, limit={limit})"
    )

    db_context = exec_context.db_context
    if not exec_context.attachment_registry:
        return ToolResult(text="Error: Attachment registry not available.")

    try:
        # Fetch attachment content
        content_bytes = await exec_context.attachment_registry.get_attachment_content(
            db_context, attachment_id_str
        )

        if content_bytes is None:
            return ToolResult(
                text=f"Error: Attachment {attachment_id_str} not found or has no content."
            )

        try:
            content_text = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(text="Error: Attachment content is not valid UTF-8 text.")

        lines = content_text.splitlines()
        total_lines = len(lines)

        # Filter lines if grep is provided
        if grep:
            grep_lower = grep.lower()
            lines = [line for line in lines if grep_lower in line.lower()]
            filtered_count = len(lines)
        else:
            filtered_count = total_lines

        # Apply pagination
        paged_lines = lines[offset : offset + limit]
        result_text = "\n".join(paged_lines)

        summary = f"Read {len(paged_lines)} lines"
        if grep:
            summary += f" matching '{grep}'"
        summary += f" (offset={offset}, total available={filtered_count})"
        if filtered_count != total_lines:
            summary += f" [out of {total_lines} total lines in file]"

        return ToolResult(
            text=f"--- Attachment {attachment_id_str} ({summary}) ---\n{result_text}",
            data={
                "attachment_id": attachment_id_str,
                "total_lines": total_lines,
                "filtered_count": filtered_count,
                "offset": offset,
                "limit": limit,
                "lines": paged_lines,
            },
        )

    except Exception as e:
        logger.error(
            f"Error reading attachment {attachment_id_str}: {e}", exc_info=True
        )
        return ToolResult(text=f"Error: Failed to read attachment. {str(e)}")


async def attach_to_response_tool(
    exec_context: ToolExecutionContext,
    attachment_ids: list[ScriptAttachment | str],
) -> str:
    """
    Attach files/images to the current LLM response.

    This tool signals to the processing service that the specified attachments
    should be sent along with the current response to the user.

    Args:
        exec_context: The execution context
        attachment_ids: List of ScriptAttachment objects or string IDs to attach to the response

    Returns:
        A JSON string indicating the attachments have been queued
    """

    logger.info(
        f"Executing attach_to_response_tool with {len(attachment_ids)} attachment(s)"
    )

    # Validate that we have attachment registry
    if not exec_context.attachment_registry:
        logger.error("AttachmentRegistry not available in execution context")
        return json.dumps({
            "status": "error",
            "message": "AttachmentRegistry not available",
        })

    # Extract attachment IDs from ScriptAttachment objects or use string IDs directly
    validated_ids = []
    for attachment in attachment_ids:
        try:
            if isinstance(attachment, str):
                # Direct string ID (from LLM tool calls)
                attachment_id = attachment
                logger.debug(f"Processing string attachment ID: {attachment_id}")
            else:
                # ScriptAttachment object (from script contexts)
                attachment_id = attachment.get_id()
                logger.debug(
                    f"Processing ScriptAttachment: {attachment_id} - {attachment.get_description()}"
                )

            # Basic validation - verify attachment exists and is accessible
            # TODO: Add conversation-level access validation here
            validated_ids.append(attachment_id)

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
