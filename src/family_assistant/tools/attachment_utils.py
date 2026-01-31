"""Utility functions for attachment processing in tools and scripts.

This module provides common functionality for converting attachment IDs
to ScriptAttachment objects, with proper security checks and error handling.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.apis.attachments import ScriptAttachment

if TYPE_CHECKING:
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext

logger = logging.getLogger(__name__)


def is_attachment_id(value: object) -> bool:
    """Check if a value looks like an attachment UUID."""
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


async def fetch_attachment_object(
    attachment_id: str, context: ToolExecutionContext
) -> ScriptAttachment | None:
    """
    Fetch attachment object by ID with proper security checks.

    Args:
        attachment_id: The attachment ID to fetch
        context: The tool execution context

    Returns:
        ScriptAttachment object if found, None otherwise
    """
    try:
        # Get attachment registry from context
        attachment_registry = context.attachment_registry
        if not attachment_registry:
            logger.error("AttachmentRegistry not available in execution context")
            return None

        # Fetch attachment metadata
        logger.debug(f"Looking up attachment {attachment_id} in registry")
        metadata = await attachment_registry.get_attachment(
            context.db_context, attachment_id
        )

        if metadata is None:
            logger.warning(f"Attachment not found or access denied: {attachment_id}")
            return None

        logger.debug(
            f"Found attachment {attachment_id}: {metadata.description}, conversation_id: {metadata.conversation_id}"
        )

        # Create a DatabaseContext getter for the ScriptAttachment
        # Use the existing database context from the execution context to maintain transaction consistency
        def db_context_getter() -> DatabaseContext:
            return context.db_context

        # Create and return ScriptAttachment object
        return ScriptAttachment(
            metadata=metadata,
            registry=attachment_registry,
            db_context_getter=db_context_getter,
        )

    except Exception as e:
        logger.error(f"Error fetching attachment {attachment_id}: {e}", exc_info=True)
        return None


async def process_attachment_arguments(
    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON from LLM
    arguments: dict[str, Any],
    context: ToolExecutionContext,
    tool_definition: ToolDefinition | None = None,
    # ast-grep-ignore: no-dict-any - Tool arguments are dynamic JSON
) -> dict[str, Any]:
    """
    Process arguments and convert attachment IDs to ScriptAttachment objects.

    Args:
        arguments: Raw arguments from tool call
        context: Tool execution context
        tool_definition: Tool definition to check which parameters are attachment types

    Returns:
        Processed arguments with ScriptAttachment objects
    """

    def is_attachment_parameter(param_name: str, param_value: object) -> bool:
        """Check if a parameter is defined as an attachment type."""
        if not tool_definition:
            # Fallback: if we don't have the tool definition, use the old heuristic
            return is_attachment_id(param_value)

        # Get the parameter definition from the tool schema
        properties = (
            tool_definition
            .get("function", {})
            .get("parameters", {})
            .get("properties", {})
        )
        param_def = properties.get(param_name, {})

        # Check if this parameter is defined as attachment type
        if param_def.get("type") == "attachment":
            return True

        # Check if it's an array of attachments
        if param_def.get("type") == "array":
            items = param_def.get("items", {})
            if items.get("type") == "attachment":
                return True

        return False

    processed_args = {}

    for key, value in arguments.items():
        if isinstance(value, list) and is_attachment_parameter(key, value):
            # Array of attachment IDs
            processed_values = []
            for item in value:
                if is_attachment_id(item):
                    logger.debug(
                        f"Processing attachment ID {item} from array parameter {key}"
                    )
                    attachment = await fetch_attachment_object(item, context)
                    if attachment is not None:
                        processed_values.append(attachment)
                        logger.debug(
                            f"Replaced attachment ID {item} with attachment object in array {key}"
                        )
                    else:
                        raise ValueError(
                            f"Attachment '{item}' not found or access denied in array parameter '{key}'"
                        )
                elif isinstance(item, dict) and "attachments" in item:
                    # ScriptToolResult dict format: {"text": "...", "attachments": [{"id": "...", ...}, ...]}
                    # Extract attachment IDs from the nested attachments list
                    logger.debug(
                        f"Processing ScriptToolResult dict in array parameter {key}"
                    )
                    nested_attachments = item.get("attachments", [])
                    for nested_att in nested_attachments:
                        if isinstance(nested_att, dict) and "id" in nested_att:
                            attachment_id = nested_att["id"]
                            if is_attachment_id(attachment_id):
                                logger.debug(
                                    f"Processing nested attachment ID {attachment_id} from ScriptToolResult"
                                )
                                attachment = await fetch_attachment_object(
                                    attachment_id, context
                                )
                                if attachment is not None:
                                    processed_values.append(attachment)
                                    logger.debug(
                                        f"Replaced nested attachment ID {attachment_id} with attachment object"
                                    )
                                else:
                                    raise ValueError(
                                        f"Nested attachment '{attachment_id}' not found or access denied"
                                    )
                else:
                    processed_values.append(item)
            processed_args[key] = processed_values
        elif is_attachment_parameter(key, value) and isinstance(value, str):
            # Attachment parameter with string value
            if is_attachment_id(value):
                # Valid UUID - fetch the attachment
                logger.debug(
                    f"Processing single attachment ID {value} for parameter {key}"
                )
                attachment = await fetch_attachment_object(value, context)
                if attachment is not None:
                    processed_args[key] = attachment
                    logger.debug(
                        f"Replaced attachment ID {value} with attachment object for parameter {key}"
                    )
                else:
                    raise ValueError(
                        f"Attachment '{value}' not found or access denied for parameter '{key}'"
                    )
            else:
                # Not a valid UUID
                raise ValueError(
                    f"Parameter '{key}' requires a valid attachment UUID, got: {value!r}. "
                    f"Attachment IDs are shown in tool result messages as '[Attachment ID: ...]'."
                )
        else:
            # Regular parameter, keep as-is
            processed_args[key] = value

    return processed_args
