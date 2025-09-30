"""
Attachment registry for unified attachment tracking and lifecycle management.

This module provides the AttachmentRegistry class that manages attachment metadata,
lifecycle, and access control across user-sourced and tool-generated attachments.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, delete, insert, select, update

from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.context import DatabaseContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# Default configuration values (fallbacks)
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
DEFAULT_MAX_MULTIMODAL_SIZE = 20 * 1024 * 1024  # 20MB
DEFAULT_ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "application/pdf",
}


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
    """Registry for managing attachment metadata and file storage."""

    def __init__(
        self,
        storage_path: str,
        db_engine: AsyncEngine,
        config: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialize the attachment registry.

        Args:
            storage_path: Base directory for storing attachment files
            db_engine: Database engine for creating contexts
            config: Optional configuration dictionary (attachment_config section)
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.db_engine = db_engine

        # Set up configuration with defaults
        attachment_config = config or {}
        self.max_file_size = attachment_config.get(
            "max_file_size", DEFAULT_MAX_FILE_SIZE
        )
        self.max_multimodal_size = attachment_config.get(
            "max_multimodal_size", DEFAULT_MAX_MULTIMODAL_SIZE
        )
        allowed_types = attachment_config.get(
            "allowed_mime_types", list(DEFAULT_ALLOWED_MIME_TYPES)
        )
        self.allowed_mime_types = (
            set(allowed_types)
            if isinstance(allowed_types, list)
            else DEFAULT_ALLOWED_MIME_TYPES
        )

        logger.info(
            f"AttachmentRegistry initialized with storage path: {self.storage_path}, "
            f"max_file_size: {self.max_file_size // (1024 * 1024)}MB, "
            f"max_multimodal_size: {self.max_multimodal_size // (1024 * 1024)}MB, "
            f"allowed_types: {len(self.allowed_mime_types)} types"
        )

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
        logger.info(f"register_attachment return type: {type(attachment_metadata)}")
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
        attachment_data = await self._store_file_only(content, filename, mime_type)

        # Register in metadata database
        return await self.register_attachment(
            db_context=db_context,
            attachment_id=attachment_data.attachment_id,
            source_type="user",
            source_id=user_id,
            mime_type=mime_type,
            description=description or f"User uploaded: {filename}",
            size=len(content),
            content_url=attachment_data.content_url,
            storage_path=attachment_data.storage_path,
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
        file_path = self.get_attachment_path(attachment_id)
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
            file_deleted = self._delete_attachment_file(attachment_id)
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
        """Update the access time for an attachment.

        Silently ignores cancellation and database errors during shutdown,
        since access time tracking is not critical to application functionality.
        """
        try:
            update_stmt = (
                update(attachment_metadata_table)
                .where(attachment_metadata_table.c.attachment_id == attachment_id)
                .values(accessed_at=datetime.now(timezone.utc))
            )
            await db_context.execute_with_retry(update_stmt)
        except asyncio.CancelledError:
            # Operation cancelled during shutdown - this is fine, access time isn't critical
            pass
        except Exception:
            # Silently ignore other errors (e.g., connection closed during teardown)
            # Access time tracking is informational and shouldn't break operations
            pass

    async def update_access_time_background(self, attachment_id: str) -> None:
        """
        Update attachment access time in a background task.

        Creates its own database context since this is called from FastAPI
        BackgroundTasks after the request context is closed.

        Args:
            attachment_id: The attachment ID to update
        """
        try:
            async with DatabaseContext() as db:
                await self._update_access_time(db, attachment_id)
        except Exception as e:
            # Log but don't fail - access time tracking is not critical
            logger.debug(
                f"Background access time update failed for {attachment_id}: {e}"
            )

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

        # Clean up orphaned files directly
        return self._cleanup_orphaned_files(referenced_ids)

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
            .values(
                conversation_id=conversation_id,
                accessed_at=datetime.now(timezone.utc),
            )
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
            # Note: accessed_at is updated by the claim UPDATE statement above
            return AttachmentMetadata.from_row(row)

        return None

    # Convenience methods that create their own database contexts

    async def register_tool_attachment_with_context(
        self,
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
        Register a tool-generated attachment using internal database context.

        This is a convenience method that creates its own DatabaseContext.
        Use this from processing.py and other places that don't already have a context.
        """
        async with DatabaseContext(self.db_engine) as db_context:
            return await self.register_tool_attachment(
                db_context=db_context,
                attachment_id=attachment_id,
                tool_name=tool_name,
                mime_type=mime_type,
                description=description,
                size=size,
                content_url=content_url,
                storage_path=storage_path,
                conversation_id=conversation_id,
                message_id=message_id,
                metadata=metadata,
            )

    async def get_attachment_with_context(
        self, attachment_id: str
    ) -> AttachmentMetadata | None:
        """
        Get attachment metadata by ID using internal database context.

        This is a convenience method that creates its own DatabaseContext.
        """
        async with DatabaseContext(self.db_engine) as db_context:
            return await self.get_attachment(db_context, attachment_id)

    async def store_and_register_tool_attachment(
        self,
        file_content: bytes,
        filename: str,
        content_type: str,
        tool_name: str,
        description: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Store file content and register as a tool attachment in one operation.

        This is a public method that encapsulates the full workflow for tool-generated attachments.

        Args:
            file_content: Raw file content bytes
            filename: Original filename
            content_type: MIME type
            tool_name: Name of the tool that created it
            description: Optional description
            conversation_id: Associated conversation
            message_id: Associated message
            metadata: Additional metadata

        Returns:
            AttachmentMetadata for the stored and registered attachment
        """
        # First store the file
        file_metadata = await self._store_file_only(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
        )

        # Then register it in the database
        return await self.register_tool_attachment_with_context(
            attachment_id=file_metadata.attachment_id,
            tool_name=tool_name,
            mime_type=content_type,
            description=description or f"Tool attachment from {tool_name}",
            size=len(file_content),
            content_url=file_metadata.content_url
            or f"/api/attachments/{file_metadata.attachment_id}",
            storage_path=str(file_metadata.storage_path)
            if file_metadata.storage_path
            else None,
            conversation_id=conversation_id,
            message_id=message_id,
            metadata=metadata,
        )

    # File storage methods (previously from AttachmentService)

    def _calculate_content_hash(self, content: bytes) -> str:
        """Calculate SHA-256 hash of file content."""
        return hashlib.sha256(content).hexdigest()

    def _get_file_path(self, attachment_id: str, filename: str) -> Path:
        """
        Generate file storage path for an attachment.

        Uses hash-based directory structure: XX/attachment_id.ext
        where XX is the first 2 characters of the attachment_id (provides 256 buckets).
        """
        # Use first 2 characters of attachment_id for directory sharding
        hash_prefix = attachment_id[:2]
        hash_dir = self.storage_path / hash_prefix
        hash_dir.mkdir(parents=True, exist_ok=True)

        # Use attachment_id as filename with original extension
        file_ext = Path(filename).suffix.lower()
        return hash_dir / f"{attachment_id}{file_ext}"

    def _validate_file(self, file: UploadFile) -> None:
        """
        Validate uploaded file for type and size restrictions.

        Args:
            file: The uploaded file to validate

        Raises:
            HTTPException: If file validation fails
        """
        # Check MIME type
        if file.content_type not in self.allowed_mime_types:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{file.content_type}' not allowed. "
                f"Allowed types: {', '.join(self.allowed_mime_types)}",
            )

        # Check file size
        if hasattr(file.file, "seek") and hasattr(file.file, "tell"):
            # Get current position
            current_pos = file.file.tell()
            # Seek to end to get size
            file.file.seek(0, 2)
            file_size = file.file.tell()
            # Seek back to original position
            file.file.seek(current_pos)

            if file_size > self.max_file_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"File size {file_size} bytes exceeds maximum allowed size of {self.max_file_size} bytes",
                )

    def _sanitize_filename(self, filename: str) -> str:
        """
        Sanitize filename to prevent path traversal and other security issues.

        Args:
            filename: Original filename

        Returns:
            Sanitized filename
        """
        # Remove any path components
        filename = os.path.basename(filename)

        # Remove any potentially dangerous characters
        dangerous_chars = ["<", ">", ":", '"', "/", "\\", "|", "?", "*", "\0"]
        for char in dangerous_chars:
            filename = filename.replace(char, "_")

        # Ensure filename is not empty and not too long
        if not filename or filename.startswith("."):
            filename = f"attachment{Path(filename).suffix}"

        if len(filename) > 255:
            name_part = filename[:200]
            ext_part = filename[-50:]
            filename = name_part + "..." + ext_part

        return filename

    async def _store_file_only(
        self,
        file_content: bytes,
        filename: str,
        content_type: str = "image/jpeg",
    ) -> AttachmentMetadata:
        """
        Store raw bytes as an attachment file (private method for internal use).

        Args:
            file_content: Raw file content bytes
            filename: Original filename
            content_type: MIME type of the file

        Returns:
            AttachmentMetadata object

        Raises:
            ValueError: If file validation fails
        """
        # Basic validation
        if len(file_content) > self.max_file_size:
            raise ValueError(
                f"File size {len(file_content)} bytes exceeds maximum allowed size of {self.max_file_size} bytes"
            )

        if content_type not in self.allowed_mime_types:
            raise ValueError(
                f"File type '{content_type}' not allowed. Allowed types: {', '.join(self.allowed_mime_types)}"
            )

        # Generate unique attachment ID
        attachment_id = str(uuid.uuid4())

        # Sanitize filename
        safe_filename = self._sanitize_filename(filename)

        try:
            # Calculate content hash for potential future deduplication
            _ = self._calculate_content_hash(file_content)

            # Get storage path
            file_path = self._get_file_path(attachment_id, safe_filename)

            # Write file to disk asynchronously
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(file_content)

            # Create minimal attachment metadata object (caller should provide proper metadata)
            attachment_metadata = AttachmentMetadata(
                attachment_id=attachment_id,
                source_type="file_only",  # Indicates this is just file storage, not registered
                source_id="file_storage",  # Generic source for file-only storage
                mime_type=content_type,
                description=f"File storage: {safe_filename}",
                size=len(file_content),
                content_url=f"/api/attachments/{attachment_id}",
                storage_path=str(file_path.relative_to(self.storage_path)),
                metadata={
                    "original_filename": safe_filename,
                    "storage_method": "file_only",
                },
            )

            logger.info(
                f"Successfully stored attachment {attachment_id}: {safe_filename} ({len(file_content)} bytes)"
            )

            return attachment_metadata

        except Exception as e:
            logger.error(f"Failed to store attachment: {e}")
            raise ValueError(f"Failed to store attachment: {e}") from e

    async def store_attachment(self, file: UploadFile) -> AttachmentMetadata:
        """
        Store an uploaded attachment file.

        Args:
            file: The uploaded file

        Returns:
            AttachmentMetadata object

        Raises:
            HTTPException: If file validation or storage fails
        """
        # Validate the file
        self._validate_file(file)

        # Generate unique attachment ID
        attachment_id = str(uuid.uuid4())

        # Sanitize filename
        safe_filename = self._sanitize_filename(file.filename or "attachment")

        try:
            # Read file content
            file_content = await file.read()

            # Calculate content hash for potential future deduplication
            _ = self._calculate_content_hash(file_content)

            # Get storage path
            file_path = self._get_file_path(attachment_id, safe_filename)

            # Write file to disk asynchronously
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(file_content)

            # Create attachment metadata
            attachment_metadata = AttachmentMetadata(
                attachment_id=attachment_id,
                source_type="user",
                source_id="api_user",
                mime_type=file.content_type or "application/octet-stream",
                description=f"User uploaded: {safe_filename}",
                size=len(file_content),
                content_url=f"/api/attachments/{attachment_id}",
                storage_path=str(file_path.relative_to(self.storage_path)),
                metadata={"original_filename": safe_filename, "upload_method": "api"},
            )

            logger.info(
                f"Successfully stored attachment {attachment_id}: {safe_filename} ({len(file_content)} bytes)"
            )
            return attachment_metadata

        except Exception as e:
            logger.error(f"Failed to store attachment: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to store attachment: {str(e)}"
            ) from e

    def get_attachment_path(self, attachment_id: str) -> Path | None:
        """
        Get the file system path for an attachment by ID.

        Args:
            attachment_id: The attachment UUID

        Returns:
            Path to the attachment file, or None if not found
        """
        try:
            # Parse as UUID to validate format
            uuid.UUID(attachment_id)
        except ValueError:
            logger.warning(f"Invalid attachment ID format: {attachment_id}")
            return None

        # Use hash prefix to directly locate the file
        hash_prefix = attachment_id[:2]
        hash_dir = self.storage_path / hash_prefix

        if not hash_dir.is_dir():
            logger.info(f"Attachment file not found: {attachment_id}")
            return None

        # Look for files starting with the attachment ID in the hash directory
        # Use attachment_id* to find both files with and without extensions
        for file_path in hash_dir.glob(f"{attachment_id}*"):
            if file_path.is_file():
                return file_path

        logger.info(f"Attachment file not found: {attachment_id}")
        return None

    def get_content_type(self, file_path: Path) -> str:
        """
        Get the MIME type for a file.

        Args:
            file_path: Path to the file

        Returns:
            MIME type string
        """
        content_type, _ = mimetypes.guess_type(str(file_path))
        return content_type or "application/octet-stream"

    def _delete_attachment_file(self, attachment_id: str) -> bool:
        """
        Delete an attachment file (private method).

        Args:
            attachment_id: The attachment UUID

        Returns:
            True if file was deleted, False if not found
        """
        file_path = self.get_attachment_path(attachment_id)
        if file_path and file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Deleted attachment file: {attachment_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete attachment {attachment_id}: {e}")
                return False
        return False

    def _cleanup_orphaned_files(self, referenced_attachment_ids: set[str]) -> int:
        """
        Clean up attachment files that are no longer referenced in the database.

        Args:
            referenced_attachment_ids: Set of attachment IDs that are still referenced

        Returns:
            Number of files deleted
        """
        deleted_count = 0

        # Iterate through hash-prefixed directories (00-ff)
        for hash_dir in self.storage_path.glob("*/"):
            if not hash_dir.is_dir():
                continue

            for file_path in hash_dir.glob("*"):
                if not file_path.is_file():
                    continue

                # Extract attachment ID from filename
                file_stem = file_path.stem
                try:
                    uuid.UUID(file_stem)  # Validate it's a UUID
                    if file_stem not in referenced_attachment_ids:
                        file_path.unlink()
                        deleted_count += 1
                        logger.info(f"Deleted orphaned attachment: {file_stem}")
                except (ValueError, OSError) as e:
                    logger.warning(
                        f"Skipping non-UUID file or deletion error: {file_path}: {e}"
                    )
                    continue

        logger.info(f"Cleaned up {deleted_count} orphaned attachment files")
        return deleted_count
