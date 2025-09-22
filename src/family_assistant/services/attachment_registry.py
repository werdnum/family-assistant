"""
Attachment registry for unified attachment tracking and lifecycle management.

This module provides the AttachmentRegistry class that manages attachment metadata,
lifecycle, and access control across user-sourced and tool-generated attachments.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiofiles
from sqlalchemy import and_, delete, insert, select, update

from family_assistant.storage.base import attachment_metadata_table

if TYPE_CHECKING:
    from family_assistant.services.attachments import AttachmentService
    from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)


class AttachmentMetadata:
    """Metadata container for attachment information."""

    def __init__(
        self,
        attachment_id: str,
        source_type: str,
        source_id: str,
        mime_type: str,
        description: str,
        size: int,
        content_url: str | None = None,
        storage_path: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        created_at: datetime | None = None,
        accessed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.attachment_id = attachment_id
        self.source_type = source_type  # "user", "tool", "script"
        self.source_id = source_id  # user_id, tool_name, script_id
        self.mime_type = mime_type
        self.description = description
        self.size = size
        self.content_url = content_url
        self.storage_path = storage_path
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.created_at = created_at or datetime.now(timezone.utc)
        self.accessed_at = accessed_at
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "attachment_id": self.attachment_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "mime_type": self.mime_type,
            "description": self.description,
            "size": self.size,
            "content_url": self.content_url,
            "storage_path": self.storage_path,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "accessed_at": self.accessed_at.isoformat() if self.accessed_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AttachmentMetadata:
        """Create from database row."""
        return cls(
            attachment_id=row["attachment_id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            mime_type=row["mime_type"],
            description=row["description"],
            size=row["size"],
            content_url=row["content_url"],
            storage_path=row["storage_path"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
            metadata=row["metadata"],
        )


class AttachmentRegistry:
    """Registry for managing attachment metadata and lifecycle."""

    def __init__(self, attachment_service: AttachmentService) -> None:
        """
        Initialize the attachment registry.

        Args:
            attachment_service: The attachment service for file operations
        """
        self.attachment_service = attachment_service

    async def register_attachment(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        source_type: str,
        source_id: str,
        mime_type: str,
        description: str,
        size: int,
        content_url: str | None = None,
        storage_path: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Register a new attachment in the metadata database.

        Args:
            db_context: Database context
            attachment_id: Unique attachment identifier
            source_type: Source of attachment ("user", "tool", "script")
            source_id: Source identifier (user_id, tool_name, etc.)
            mime_type: MIME type of the attachment
            description: Human-readable description
            size: Size in bytes
            content_url: URL for content retrieval
            storage_path: File system storage path
            conversation_id: Associated conversation ID
            message_id: Associated message ID
            metadata: Additional metadata

        Returns:
            AttachmentMetadata object
        """
        attachment_metadata = AttachmentMetadata(
            attachment_id=attachment_id,
            source_type=source_type,
            source_id=source_id,
            mime_type=mime_type,
            description=description,
            size=size,
            content_url=content_url,
            storage_path=storage_path,
            conversation_id=conversation_id,
            message_id=message_id,
            metadata=metadata,
        )

        # Insert into database
        insert_stmt = insert(attachment_metadata_table).values(
            attachment_id=attachment_metadata.attachment_id,
            source_type=attachment_metadata.source_type,
            source_id=attachment_metadata.source_id,
            mime_type=attachment_metadata.mime_type,
            description=attachment_metadata.description,
            size=attachment_metadata.size,
            content_url=attachment_metadata.content_url,
            storage_path=attachment_metadata.storage_path,
            conversation_id=attachment_metadata.conversation_id,
            message_id=attachment_metadata.message_id,
            created_at=attachment_metadata.created_at,
            metadata=attachment_metadata.metadata,
        )

        await db_context.execute_with_retry(insert_stmt)

        logger.info(
            f"Registered attachment {attachment_id} from {source_type}:{source_id}"
        )
        return attachment_metadata

    async def get_attachment(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
    ) -> AttachmentMetadata | None:
        """
        Get attachment metadata by ID.

        Args:
            db_context: Database context
            attachment_id: Attachment identifier

        Returns:
            AttachmentMetadata if found and accessible, None otherwise
        """
        # Since API endpoints are public and we don't worry excessively about
        # security between authenticated users, allow access to any attachment
        query = select(attachment_metadata_table).where(
            attachment_metadata_table.c.attachment_id == attachment_id
        )

        row = await db_context.fetch_one(query)
        if not row:
            return None

        # Update access time
        await self._update_access_time(db_context, attachment_id)

        return AttachmentMetadata.from_row(row)

    async def list_attachments(
        self,
        db_context: DatabaseContext,
        conversation_id: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
    ) -> list[AttachmentMetadata]:
        """
        List attachments with optional filtering.

        Args:
            db_context: Database context
            conversation_id: Filter by conversation
            source_type: Filter by source type ("user", "tool", "script")
            limit: Maximum number of results

        Returns:
            List of AttachmentMetadata objects
        """
        # Build query with optional filters
        query = select(attachment_metadata_table)

        if conversation_id:
            query = query.where(
                attachment_metadata_table.c.conversation_id == conversation_id
            )

        if source_type:
            query = query.where(attachment_metadata_table.c.source_type == source_type)

        query = query.order_by(attachment_metadata_table.c.created_at.desc()).limit(
            limit
        )

        rows = await db_context.fetch_all(query)
        return [AttachmentMetadata.from_row(row) for row in rows]

    async def register_user_attachment(
        self,
        db_context: DatabaseContext,
        content: bytes,
        filename: str,
        mime_type: str,
        conversation_id: str | None = None,
        message_id: int | None = None,
        user_id: str = "api_user",
        description: str | None = None,
    ) -> AttachmentMetadata:
        """
        Register a user-uploaded attachment.

        Args:
            db_context: Database context
            content: File content bytes
            filename: Original filename
            mime_type: MIME type
            conversation_id: Associated conversation
            message_id: Associated message ID
            user_id: User identifier
            description: Optional description

        Returns:
            AttachmentMetadata object
        """
        # Store the attachment file
        attachment_data = self.attachment_service.store_bytes_as_attachment(
            content, filename, mime_type
        )

        # Register in metadata database
        return await self.register_attachment(
            db_context=db_context,
            attachment_id=attachment_data["attachment_id"],
            source_type="user",
            source_id=user_id,
            mime_type=mime_type,
            description=description or f"User uploaded: {filename}",
            size=len(content),
            content_url=attachment_data["url"],
            storage_path=attachment_data["storage_path"],
            conversation_id=conversation_id,
            message_id=message_id,
            metadata={"original_filename": filename, "upload_method": "api"},
        )

    async def register_tool_attachment(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        tool_name: str,
        mime_type: str,
        description: str,
        size: int,
        content_url: str,
        storage_path: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Register a tool-generated attachment.

        Args:
            db_context: Database context
            attachment_id: Attachment identifier (from AttachmentService)
            tool_name: Name of the tool that created it
            mime_type: MIME type
            description: Description
            size: Size in bytes
            content_url: URL for retrieval
            storage_path: File system path
            conversation_id: Associated conversation
            message_id: Associated message
            metadata: Additional metadata

        Returns:
            AttachmentMetadata object
        """
        return await self.register_attachment(
            db_context=db_context,
            attachment_id=attachment_id,
            source_type="tool",
            source_id=tool_name,
            mime_type=mime_type,
            description=description,
            size=size,
            content_url=content_url,
            storage_path=storage_path,
            conversation_id=conversation_id,
            message_id=message_id,
            metadata=metadata or {},
        )

    async def get_attachment_content(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        conversation_id: str | None = None,
    ) -> bytes | None:
        """
        Get attachment content by ID.

        Args:
            db_context: Database context
            attachment_id: Attachment identifier
            conversation_id: Conversation scope for access control

        Returns:
            File content bytes if found and accessible, None otherwise
        """
        # First verify access
        metadata = await self.get_attachment(db_context, attachment_id)
        if not metadata:
            return None

        # Get content from file system
        file_path = self.attachment_service.get_attachment_path(attachment_id)
        if not file_path or not file_path.exists():
            logger.warning(f"Attachment file not found: {attachment_id}")
            return None

        try:
            async with aiofiles.open(file_path, "rb") as f:
                return await f.read()
        except Exception as e:
            logger.error(f"Error reading attachment {attachment_id}: {e}")
            return None

    async def delete_attachment(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        conversation_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """
        Delete an attachment (metadata and file).

        Args:
            db_context: Database context
            attachment_id: Attachment identifier
            conversation_id: Conversation scope for access control (for linked attachments)
            user_id: User identifier for deleting unlinked attachments

        Returns:
            True if deleted, False if not found or access denied
        """
        # Build conditions based on what's provided
        conditions = [attachment_metadata_table.c.attachment_id == attachment_id]

        if conversation_id is not None:
            # Delete linked attachment - require exact conversation match
            conditions.append(
                attachment_metadata_table.c.conversation_id == conversation_id
            )
            logger.debug(
                f"Deleting linked attachment {attachment_id} from conversation {conversation_id}"
            )
        elif user_id is not None:
            # Delete unlinked attachment - allow user to delete their own unlinked attachments
            conditions.extend([
                attachment_metadata_table.c.conversation_id.is_(
                    None
                ),  # Must be unlinked
                attachment_metadata_table.c.source_type
                == "user",  # Must be user attachment
                attachment_metadata_table.c.source_id
                == user_id,  # Must be owned by user
            ])
            logger.debug(
                f"Deleting unlinked attachment {attachment_id} for user {user_id}"
            )
        else:
            logger.warning(
                f"Delete attempt without conversation_id or user_id for attachment {attachment_id}"
            )
            return False

        # Atomic delete with authorization check - prevents TOCTOU race condition
        delete_stmt = delete(attachment_metadata_table).where(and_(*conditions))
        result = await db_context.execute_with_retry(delete_stmt)

        success = result.rowcount > 0
        file_deleted = False

        if success:
            # Only delete file if database deletion succeeded
            file_deleted = self.attachment_service.delete_attachment(attachment_id)
            logger.info(
                f"Deleted attachment {attachment_id} (db: {success}, file: {file_deleted})"
            )
        else:
            logger.info(
                f"Failed to delete attachment {attachment_id} - not found or access denied"
            )

        return success

    async def _update_access_time(
        self, db_context: DatabaseContext, attachment_id: str
    ) -> None:
        """Update the access time for an attachment."""
        update_stmt = (
            update(attachment_metadata_table)
            .where(attachment_metadata_table.c.attachment_id == attachment_id)
            .values(accessed_at=datetime.now(timezone.utc))
        )
        await db_context.execute_with_retry(update_stmt)

    async def cleanup_orphaned_attachments(self, db_context: DatabaseContext) -> int:
        """
        Clean up file system attachments that are no longer referenced in the database.
        Uses AttachmentService to clean up orphaned files based on current database references.

        Args:
            db_context: Database context

        Returns:
            Number of attachments cleaned up
        """
        # Get attachment IDs that are still referenced in the database
        referenced_query = select(attachment_metadata_table.c.attachment_id).distinct()
        referenced_rows = await db_context.fetch_all(referenced_query)
        referenced_ids = {row["attachment_id"] for row in referenced_rows}

        # Use AttachmentService to clean up orphaned files
        return self.attachment_service.cleanup_orphaned_files(referenced_ids)

    async def update_attachment_conversation(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        conversation_id: str,
    ) -> bool:
        """
        Update an attachment's conversation_id for security linking.

        Args:
            db_context: Database context
            attachment_id: Attachment identifier
            conversation_id: New conversation ID to link to

        Returns:
            True if updated successfully, False if attachment not found
        """
        update_stmt = (
            update(attachment_metadata_table)
            .where(attachment_metadata_table.c.attachment_id == attachment_id)
            .values(conversation_id=conversation_id)
        )

        result = await db_context.execute_with_retry(update_stmt)
        success = result.rowcount > 0

        if success:
            logger.info(
                f"Linked attachment {attachment_id} to conversation {conversation_id}"
            )

        return success

    async def claim_unlinked_attachment(
        self,
        db_context: DatabaseContext,
        attachment_id: str,
        conversation_id: str,
        required_source_id: str = "api_user",
    ) -> AttachmentMetadata | None:
        """
        Atomically claim an unlinked attachment for a conversation.

        This prevents race conditions by using a single atomic update operation
        that only succeeds if the attachment is still unlinked and matches criteria.

        Args:
            db_context: Database context
            attachment_id: Attachment identifier
            conversation_id: Conversation to link the attachment to
            required_source_id: Required source_id for security validation

        Returns:
            AttachmentMetadata if successfully claimed, None if not available or access denied
        """
        # Atomic update that only claims unlinked attachments from the correct source
        update_stmt = (
            update(attachment_metadata_table)
            .where(
                and_(
                    attachment_metadata_table.c.attachment_id == attachment_id,
                    attachment_metadata_table.c.conversation_id.is_(
                        None
                    ),  # Only unlinked
                    attachment_metadata_table.c.source_type == "user",
                    attachment_metadata_table.c.source_id == required_source_id,
                )
            )
            .values(conversation_id=conversation_id)
        )

        result = await db_context.execute_with_retry(update_stmt)

        if result.rowcount == 0:
            # Either attachment doesn't exist, already claimed, or access denied
            return None

        # Successfully claimed, now fetch the updated record
        query = select(attachment_metadata_table).where(
            attachment_metadata_table.c.attachment_id == attachment_id
        )
        row = await db_context.fetch_one(query)

        if row:
            logger.info(
                f"Successfully claimed attachment {attachment_id} for conversation {conversation_id}"
            )
            # Update access time since we're accessing it
            await self._update_access_time(db_context, attachment_id)
            return AttachmentMetadata.from_row(row)

        return None
