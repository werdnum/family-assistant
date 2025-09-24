"""Utility functions for attachment processing in tools and scripts.

This module provides common functionality for converting attachment IDs
to ScriptAttachment objects, with proper security checks and error handling.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import AttachmentRegistry

if TYPE_CHECKING:
    from family_assistant.storage.context import DatabaseContext
    from family_assistant.tools.types import ToolExecutionContext

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
        # Get attachment service from context
        attachment_service = context.attachment_service
        if not attachment_service:
            logger.error("AttachmentService not available in execution context")
            return None

        # Create attachment registry
        attachment_registry = AttachmentRegistry(attachment_service)

        # Fetch attachment metadata
        metadata = await attachment_registry.get_attachment(
            context.db_context, attachment_id
        )

        if metadata is None:
            logger.warning(f"Attachment not found or access denied: {attachment_id}")
            return None

        # Check conversation scoping - this is a critical security check
        if (
            context.conversation_id
            and metadata.conversation_id != context.conversation_id
        ):
            logger.warning(
                f"Attachment {attachment_id} not accessible from conversation {context.conversation_id}"
            )
            return None

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
    arguments: dict[str, Any], context: ToolExecutionContext
) -> dict[str, Any]:
    """
    Process arguments and convert attachment IDs to ScriptAttachment objects.

    Args:
        arguments: Raw arguments from tool call
        context: Tool execution context

    Returns:
        Processed arguments with ScriptAttachment objects
    """
    processed_args = {}

    for key, value in arguments.items():
        # Check for attachment ID patterns
        if isinstance(value, list):
            # Array of potential attachment IDs
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
                        logger.error(
                            f"Could not fetch attachment {item} from array {key}, skipping"
                        )
                else:
                    processed_values.append(item)
            processed_args[key] = processed_values
        elif is_attachment_id(value):
            # Single attachment ID
            logger.debug(f"Processing single attachment ID {value} for parameter {key}")
            attachment = await fetch_attachment_object(value, context)
            if attachment is not None:
                processed_args[key] = attachment
                logger.debug(
                    f"Replaced attachment ID {value} with attachment object for parameter {key}"
                )
            else:
                logger.error(
                    f"Could not fetch attachment {value} for parameter {key}, removing parameter"
                )
                # Don't add this parameter to processed_args
        else:
            # Regular parameter, keep as-is
            processed_args[key] = value

    return processed_args
