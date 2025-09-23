"""Attachment API for Starlark scripts.

This module provides attachment-related functions for Starlark scripts to work with
user and tool attachments within conversations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.attachment_registry import AttachmentRegistry
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class AttachmentAPI:
    """API for attachment operations in Starlark scripts."""

    def __init__(
        self,
        attachment_registry: AttachmentRegistry,
        conversation_id: str | None = None,
        main_loop: asyncio.AbstractEventLoop | None = None,
        db_engine: AsyncEngine | None = None,
    ) -> None:
        """
        Initialize the attachment API.

        Args:
            attachment_registry: The attachment registry service
            conversation_id: Current conversation ID for scoping
            main_loop: Main event loop for async operations
            db_engine: Database engine for DatabaseContext
        """
        self.attachment_registry = attachment_registry
        self.conversation_id = conversation_id
        self.main_loop = main_loop
        self.db_engine = db_engine

    def get(self, attachment_id: str) -> dict[str, Any] | None:
        """
        Get attachment metadata by ID.

        Args:
            attachment_id: UUID of the attachment

        Returns:
            Dictionary with attachment metadata or None if not found
        """
        try:
            # Starlark scripts run in worker threads, so use run_coroutine_threadsafe
            if self.main_loop:
                future = asyncio.run_coroutine_threadsafe(
                    self._get_async(attachment_id), self.main_loop
                )
                return future.result(timeout=30)
            else:
                # No main loop provided, use asyncio.run (works in tests and standalone contexts)
                return asyncio.run(self._get_async(attachment_id))

        except Exception as e:
            logger.error(f"Error getting attachment {attachment_id}: {e}")
            return None

    async def _get_async(self, attachment_id: str) -> dict[str, Any] | None:
        """Async implementation of get."""
        from family_assistant.storage.context import DatabaseContext

        async with DatabaseContext(engine=self.db_engine) as db_context:
            attachment = await self.attachment_registry.get_attachment(
                db_context, attachment_id
            )

            if not attachment:
                return None

            # Check conversation scoping if we have a conversation ID
            if (
                self.conversation_id
                and attachment.conversation_id != self.conversation_id
            ):
                logger.warning(
                    f"Attachment {attachment_id} not accessible from conversation {self.conversation_id}"
                )
                return None

            return {
                "attachment_id": attachment.attachment_id,
                "source_type": attachment.source_type,
                "source_id": attachment.source_id,
                "mime_type": attachment.mime_type,
                "description": attachment.description,
                "size": attachment.size,
                "content_url": attachment.content_url,
                "created_at": attachment.created_at.isoformat(),
                "conversation_id": attachment.conversation_id,
                "message_id": attachment.message_id,
            }

    def list(
        self, source_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        List attachments in the current conversation.

        Args:
            source_type: Filter by source type ("user", "tool", "script")
            limit: Maximum number of results (default: 20)

        Returns:
            List of attachment metadata dictionaries
        """
        try:
            # Starlark scripts run in worker threads, so use run_coroutine_threadsafe
            if self.main_loop:
                future = asyncio.run_coroutine_threadsafe(
                    self._list_async(source_type, limit), self.main_loop
                )
                return future.result(timeout=30)
            else:
                # No main loop provided, use asyncio.run (works in tests and standalone contexts)
                return asyncio.run(self._list_async(source_type, limit))

        except Exception as e:
            logger.error(f"Error listing attachments: {e}")
            return []

    async def _list_async(
        self, source_type: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Async implementation of list."""
        from family_assistant.storage.context import DatabaseContext

        async with DatabaseContext(engine=self.db_engine) as db_context:
            attachments = await self.attachment_registry.list_attachments(
                db_context,
                conversation_id=self.conversation_id,
                source_type=source_type,
                limit=limit,
            )

            return [
                {
                    "attachment_id": att.attachment_id,
                    "source_type": att.source_type,
                    "source_id": att.source_id,
                    "mime_type": att.mime_type,
                    "description": att.description,
                    "size": att.size,
                    "content_url": att.content_url,
                    "created_at": att.created_at.isoformat(),
                    "conversation_id": att.conversation_id,
                    "message_id": att.message_id,
                }
                for att in attachments
            ]

    def send(self, attachment_id: str, message: str | None = None) -> str:
        """
        Send an attachment to the user.

        Args:
            attachment_id: UUID of the attachment to send
            message: Optional message to include with the attachment

        Returns:
            Status message indicating success or failure
        """
        try:
            # Starlark scripts run in worker threads, so use run_coroutine_threadsafe
            if self.main_loop:
                future = asyncio.run_coroutine_threadsafe(
                    self._send_async(attachment_id, message), self.main_loop
                )
                return future.result(timeout=30)
            else:
                # No main loop provided, use asyncio.run (works in tests and standalone contexts)
                return asyncio.run(self._send_async(attachment_id, message))

        except Exception as e:
            logger.error(f"Error sending attachment {attachment_id}: {e}")
            return f"Error sending attachment: {str(e)}"

    async def _send_async(self, attachment_id: str, message: str | None = None) -> str:
        """Async implementation of send."""
        from family_assistant.storage.context import DatabaseContext

        async with DatabaseContext(engine=self.db_engine) as db_context:
            # Verify attachment exists and is accessible
            attachment = await self.attachment_registry.get_attachment(
                db_context, attachment_id
            )

            if not attachment:
                return f"Attachment {attachment_id} not found"

            # Check conversation scoping if we have a conversation ID
            if (
                self.conversation_id
                and attachment.conversation_id != self.conversation_id
            ):
                return f"Attachment {attachment_id} not accessible from current conversation"

            # For now, we'll just return a success message
            # In the future, this could integrate with the chat system to actually display the attachment
            if message:
                return f"Sent attachment {attachment_id} with message: {message}"
            else:
                return f"Sent attachment {attachment_id}"


def create_attachment_api(
    execution_context: ToolExecutionContext,
    main_loop: asyncio.AbstractEventLoop | None = None,
) -> AttachmentAPI:
    """
    Create an AttachmentAPI instance from execution context.

    Args:
        execution_context: The tool execution context
        main_loop: Main event loop for async operations

    Returns:
        AttachmentAPI instance

    Raises:
        RuntimeError: If attachment_service is not available in context
    """
    if not execution_context.attachment_service:
        raise RuntimeError("AttachmentService not available in execution context")

    # Get conversation ID from execution context
    conversation_id = execution_context.conversation_id

    # Create attachment registry from the service (following the pattern from tools.py)
    from family_assistant.services.attachment_registry import AttachmentRegistry

    attachment_registry = AttachmentRegistry(execution_context.attachment_service)

    return AttachmentAPI(
        attachment_registry=attachment_registry,
        conversation_id=conversation_id,
        main_loop=main_loop,
        db_engine=execution_context.db_context.engine,
    )
