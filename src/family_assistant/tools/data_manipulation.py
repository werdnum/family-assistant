"""Data manipulation tools.

This module provides tools for querying and manipulating data attachments,
particularly JSON data using jq queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import jq

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Tool Definitions
DATA_MANIPULATION_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "jq_query",
            "description": "Query JSON data from an attachment using jq syntax. Use this to explore and extract specific fields from large JSON datasets without loading the entire content into context. Returns the query result as formatted JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attachment_id": {
                        "type": "attachment",
                        "description": "The UUID of the attachment containing JSON data to query.",
                    },
                    "jq_program": {
                        "type": "string",
                        "description": "The jq program/query to run on the JSON data. Examples: 'length' (count items), '.[0]' (first item), 'map(.field)' (extract field from all items), '[.[0].date, .[-1].date]' (get date range).",
                    },
                },
                "required": ["attachment_id", "jq_program"],
            },
        },
    },
]


async def jq_query_tool(
    exec_context: ToolExecutionContext,
    attachment_id: ScriptAttachment | str,
    jq_program: str,
) -> ToolResult:
    """
    Query JSON data from an attachment using jq.

    Args:
        exec_context: The execution context
        attachment_id: UUID of the attachment or ScriptAttachment object
        jq_program: The jq query program to execute

    Returns:
        ToolResult with the query result as formatted JSON
    """
    # Extract ID from ScriptAttachment if needed
    if isinstance(attachment_id, ScriptAttachment):
        attachment_id_str = attachment_id.get_id()
    else:
        attachment_id_str = attachment_id

    logger.info(f"Executing jq query on attachment {attachment_id_str}: {jq_program}")

    db_context = exec_context.db_context

    if not exec_context.attachment_registry:
        logger.error("AttachmentRegistry not available in ToolExecutionContext")
        return ToolResult(text="Error: Attachment registry not available.")

    try:
        attachment_registry = exec_context.attachment_registry

        # Retrieve attachment metadata
        attachment = await attachment_registry.get_attachment(
            db_context, attachment_id_str
        )

        if not attachment:
            logger.warning(f"Attachment {attachment_id} not found")
            return ToolResult(
                text=f"Error: Attachment with ID {attachment_id} not found."
            )

        # Check conversation scoping - only allow access to attachments from current conversation
        if (
            exec_context.conversation_id
            and attachment.conversation_id != exec_context.conversation_id
        ):
            logger.warning(
                f"Access denied: attachment {attachment_id} belongs to conversation {attachment.conversation_id}, "
                f"but current conversation is {exec_context.conversation_id}"
            )
            return ToolResult(
                text=f"Error: Access denied. Attachment {attachment_id} is not accessible from the current conversation."
            )

        # Get attachment content
        file_path = attachment_registry.get_attachment_path(attachment_id_str)
        if not file_path or not file_path.exists():
            logger.error(f"Attachment file not found for {attachment_id}")
            return ToolResult(
                text=f"Error: Attachment file not found for {attachment_id}."
            )

        # Read and parse JSON - use asyncio.to_thread to avoid blocking event loop
        # File I/O and JSON parsing can be blocking on large files
        try:

            def _load_and_parse_json() -> Any:  # noqa: ANN401
                content = file_path.read_bytes()
                return json.loads(content.decode("utf-8"))

            json_data = await asyncio.to_thread(_load_and_parse_json)
        except UnicodeDecodeError:
            return ToolResult(
                text="Error: Attachment is not valid UTF-8 text. Cannot process as JSON."
            )
        except json.JSONDecodeError as e:
            return ToolResult(
                text=f"Error: Attachment is not valid JSON. Parse error: {str(e)}"
            )

        # Compile and execute jq query - use asyncio.to_thread to avoid blocking event loop
        # jq.compile() and .input().all() are CPU-intensive synchronous operations
        try:

            def _execute_jq_query() -> list[Any]:
                jq_compiled = jq.compile(jq_program)
                return jq_compiled.input(json_data).all()

            result = await asyncio.to_thread(_execute_jq_query)

            # Return structured data - ToolResult handles conversion to text for LLM
            # If single result, unwrap from list
            if len(result) == 1:
                return ToolResult(data=result[0])
            else:
                return ToolResult(data=result)

        except ValueError as e:
            # jq compilation or execution error
            logger.error(f"jq query error: {e}")
            return ToolResult(text=f"Error: Invalid jq query. {str(e)}")

    except Exception as e:
        logger.error(
            f"Error executing jq query on attachment {attachment_id}: {e}",
            exc_info=True,
        )
        return ToolResult(text=f"Error: Failed to execute jq query. {str(e)}")
