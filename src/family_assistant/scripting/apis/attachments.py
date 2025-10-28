"""Attachment API for Starlark scripts.

This module provides attachment-related functions for Starlark scripts to work with
user and tool attachments within conversations.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, Any

from family_assistant.storage.context import DatabaseContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.attachment_registry import (
        AttachmentMetadata,
        AttachmentRegistry,
    )
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)


class ScriptAttachment:
    """
    Wrapper for attachment metadata that provides lazy content loading.

    This class represents an attachment object that tools can work with,
    providing methods to access ID, MIME type, description, and content
    without needing to know about the underlying storage system.
    """

    def __init__(
        self,
        metadata: AttachmentMetadata,
        registry: AttachmentRegistry,
        db_context_getter: Callable,
    ) -> None:
        """
        Initialize a ScriptAttachment.

        Args:
            metadata: The attachment metadata
            registry: The attachment registry for content access
            db_context_getter: Function that returns a DatabaseContext
        """
        self._metadata = metadata
        self._registry = registry
        self._db_context_getter = db_context_getter
        self._content_cache: bytes | None = None

    def get_id(self) -> str:
        """Get the attachment UUID."""
        return self._metadata.attachment_id

    def get_mime_type(self) -> str:
        """Get the MIME type of the attachment."""
        return self._metadata.mime_type

    def get_description(self) -> str:
        """Get the description of the attachment."""
        return self._metadata.description

    def get_size(self) -> int:
        """Get the size of the attachment in bytes."""
        return self._metadata.size

    def get_source_type(self) -> str:
        """Get the source type (user, tool, script)."""
        return self._metadata.source_type

    def get_filename(self) -> str | None:
        """Get the original filename if available."""
        # Extract filename from metadata dict if available
        metadata_dict = self._metadata.to_dict()
        return metadata_dict.get("metadata", {}).get("original_filename")

    def get_content(self) -> bytes:
        """
        Get the attachment content as bytes.

        This loads the content lazily and caches it for subsequent calls.

        Returns:
            The attachment content as bytes

        Raises:
            RuntimeError: If the content cannot be retrieved
        """
        if self._content_cache is None:
            try:
                # We need to run async code from sync context
                # This will work in the script execution environment
                async def _get_content() -> bytes:
                    async with self._db_context_getter() as db_context:
                        content = await self._registry.get_attachment_content(
                            db_context, self._metadata.attachment_id
                        )
                        if content is None:
                            raise RuntimeError(
                                f"Could not retrieve content for attachment {self._metadata.attachment_id}"
                            )
                        return content

                # Try to get running loop, fall back to new loop if none
                try:
                    loop = asyncio.get_running_loop()
                    # We're in an async context, but sync method called
                    # This should not happen in normal script execution
                    logger.debug(
                        f"Running in async context, using run_coroutine_threadsafe for attachment {self._metadata.attachment_id}"
                    )
                    future = asyncio.run_coroutine_threadsafe(_get_content(), loop)
                    self._content_cache = future.result(timeout=30)
                except RuntimeError:
                    # No running loop, use asyncio.run
                    logger.debug(
                        f"No running loop, using asyncio.run for attachment {self._metadata.attachment_id}"
                    )
                    self._content_cache = asyncio.run(_get_content())

            except Exception as e:
                raise RuntimeError(f"Failed to get attachment content: {e}") from e

        return self._content_cache

    async def get_content_async(self) -> bytes:
        """
        Get the attachment content as bytes (async version).

        This method is for use in async contexts like tool execution.
        It avoids the complex sync/async bridge used in get_content().

        Returns:
            The attachment content as bytes

        Raises:
            RuntimeError: If the content cannot be retrieved
        """
        if self._content_cache is None:
            try:
                # Get the database context - it might already be active, so don't use 'async with'
                db_context = self._db_context_getter()
                logger.debug(
                    f"Retrieving content for attachment {self._metadata.attachment_id} using registry"
                )
                content = await self._registry.get_attachment_content(
                    db_context, self._metadata.attachment_id
                )
                if content is None:
                    logger.error(
                        f"AttachmentRegistry returned None for attachment {self._metadata.attachment_id}"
                    )
                    raise RuntimeError(
                        f"Could not retrieve content for attachment {self._metadata.attachment_id}"
                    )
                logger.debug(
                    f"Successfully retrieved {len(content)} bytes for attachment {self._metadata.attachment_id}"
                )
                self._content_cache = content
            except Exception as e:
                raise RuntimeError(f"Failed to get attachment content: {e}") from e

        return self._content_cache

    def get_content_stream(self) -> io.BytesIO:
        """
        Get the attachment content as a BytesIO stream.

        This is useful for memory-efficient processing of large attachments.

        Returns:
            A BytesIO stream containing the attachment content
        """
        return io.BytesIO(self.get_content())

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    def to_dict(self) -> dict[str, Any]:
        """
        Get the attachment metadata as a dictionary.

        Returns:
            Dictionary representation of the attachment metadata
        """
        return self._metadata.to_dict()

    def __str__(self) -> str:
        """String representation of the attachment."""
        return f"ScriptAttachment(id={self.get_id()}, mime_type={self.get_mime_type()}, size={self.get_size()})"

    def __repr__(self) -> str:
        """Developer representation of the attachment."""
        return self.__str__()


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

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    async def _get_async(self, attachment_id: str) -> dict[str, Any] | None:
        """Async implementation of get."""

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
        self,
        source_type: str | None = None,
        limit: int = 20,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
        self,
        source_type: str | None = None,
        limit: int = 20,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    ) -> list[dict[str, Any]]:
        """Async implementation of list."""

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

    def create(
        self,
        content: bytes | str,
        filename: str,
        description: str = "",
        mime_type: str = "application/octet-stream",
    ) -> str:
        """
        Create a new attachment from script-generated content.

        Args:
            content: File content as bytes or string (will be UTF-8 encoded if string)
            filename: Filename for the attachment
            description: Description of the attachment
            mime_type: MIME type of the content (default: application/octet-stream)

        Returns:
            The attachment_id (UUID) of the created attachment

        Raises:
            ValueError: If content validation fails or storage fails
        """
        try:
            # Starlark scripts run in worker threads, so use run_coroutine_threadsafe
            if self.main_loop:
                future = asyncio.run_coroutine_threadsafe(
                    self._create_async(content, filename, description, mime_type),
                    self.main_loop,
                )
                return future.result(timeout=30)
            else:
                # No main loop provided, use asyncio.run (works in tests and standalone contexts)
                return asyncio.run(
                    self._create_async(content, filename, description, mime_type)
                )

        except Exception as e:
            logger.error(f"Error creating attachment: {e}")
            raise ValueError(f"Failed to create attachment: {e}") from e

    async def _create_async(
        self,
        content: bytes | str,
        filename: str,
        description: str,
        mime_type: str,
    ) -> str:
        """Async implementation of create."""
        # Convert string content to bytes if needed
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content

        # Store the file first (this validates size and mime type)
        file_metadata = await self.attachment_registry._store_file_only(
            file_content=content_bytes,
            filename=filename,
            content_type=mime_type,
        )

        # Register it in the database with source_type="script"
        async with DatabaseContext(engine=self.db_engine) as db_context:
            attachment_metadata = await self.attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=file_metadata.attachment_id,
                source_type="script",
                source_id="script_execution",
                mime_type=mime_type,
                description=description or f"Script-generated: {filename}",
                size=len(content_bytes),
                content_url=file_metadata.content_url,
                storage_path=file_metadata.storage_path,
                conversation_id=self.conversation_id,
                message_id=None,
                metadata={"original_filename": filename, "created_by": "script"},
            )

        logger.info(
            f"Script created attachment {attachment_metadata.attachment_id}: {filename}"
        )

        return attachment_metadata.attachment_id


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
        RuntimeError: If attachment_registry is not available in context
    """
    if not execution_context.attachment_registry:
        raise RuntimeError("AttachmentRegistry not available in execution context")

    # Get conversation ID from execution context
    conversation_id = execution_context.conversation_id

    # Get attachment registry from the execution context
    attachment_registry = execution_context.attachment_registry

    return AttachmentAPI(
        attachment_registry=attachment_registry,
        conversation_id=conversation_id,
        main_loop=main_loop,
        db_engine=execution_context.db_context.engine,
    )
