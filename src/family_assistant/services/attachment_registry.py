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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

import aiofiles
from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, delete, insert, or_, select, update

from family_assistant.storage.base import attachment_metadata_table
from family_assistant.storage.database import (
    Database,
    DatabaseExecutor,
    DatabaseTransaction,
)
from family_assistant.storage.email import (
    parse_attachment_infos_with_raw,
    received_emails_table,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.sql.elements import ColumnElement


class AttachmentRegistryConfig(TypedDict, total=False):
    """Configuration dictionary for AttachmentRegistry (from AttachmentConfig.model_dump())."""

    max_file_size: int
    max_multimodal_size: int
    storage_path: str
    # Stable base directory for externally-managed email attachments.
    # When set, a relative ``storage_path`` on an email row is resolved
    # against this base (instead of the worker's cwd), so legacy rows
    # from installs that configured ``attachment_storage_path``
    # relatively still resolve after a restart.
    email_attachment_base_path: str
    large_tool_result_threshold_kb: int
    allowed_mime_types: list[str]


class AttachmentRowDict(TypedDict):
    """Row dictionary from the attachment_metadata database table."""

    attachment_id: str
    source_type: str
    source_id: str
    mime_type: str
    description: str
    size: int
    content_url: str | None
    storage_path: str | None
    email_identity_hash: str | None
    conversation_id: str | None
    message_id: int | None
    owner_user_id: str | None
    created_at: datetime
    accessed_at: datetime | None
    # ast-grep-ignore: no-dict-any - Free-form JSON metadata column stored in DB
    metadata: dict[str, Any] | None


def compute_email_identity_hash(source_id: str, storage_path: str) -> str:
    """Return the bounded SHA-256 hex digest used to dedup email attachments.

    Email registrations key their partial unique index on this digest of
    ``f"{source_id}\\0{storage_path}"`` so that arbitrarily long
    ``Message-Id`` headers or external paths cannot exceed Postgres'
    btree index-row size limit. Call sites outside email ingestion do
    not populate this column; it remains ``NULL``.
    """
    return hashlib.sha256(f"{source_id}\0{storage_path}".encode()).hexdigest()


class AttachmentMetadataDict(TypedDict):
    """Dictionary representation of AttachmentMetadata (from to_dict())."""

    attachment_id: str
    source_type: str
    source_id: str
    mime_type: str
    description: str
    size: int
    content_url: str | None
    storage_path: str | None
    conversation_id: str | None
    message_id: int | None
    owner_user_id: str | None
    created_at: str | None
    accessed_at: str | None
    # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
    metadata: dict[str, Any]


logger = logging.getLogger(__name__)

# Default configuration values (fallbacks if not specified in config.yaml)
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
DEFAULT_MAX_MULTIMODAL_SIZE = 20 * 1024 * 1024  # 20MB

# MIME classes with no use except being handed to a model, so
# `max_multimodal_size` is the binding limit on them rather than `max_file_size`.
#
# PDFs are deliberately absent even though the Responses API is now sent their
# bytes: a PDF too large for a model is still useful to `read_text_attachment`,
# which extracts its text without a model seeing the file at all. Bounding them
# here would refuse an upload that has a working use.
MULTIMODAL_MIME_PREFIXES = ("image/", "audio/", "video/")
# NOTE: In production, allowed_mime_types is configured in config.yaml under
# attachments.allowed_mime_types. This default is only used when config is not provided
# (e.g., in tests). To add new MIME types, update config.yaml.
DEFAULT_ALLOWED_MIME_TYPES: set[str] = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/pdf",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "audio/webm",
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
        email_identity_hash: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        owner_user_id: str | None = None,
        created_at: datetime | None = None,
        accessed_at: datetime | None = None,
        # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.attachment_id = attachment_id
        self.source_type = source_type  # "user", "tool", "script", "email"
        self.source_id = source_id  # user_id, tool_name, script_id, email Message-Id
        self.mime_type = mime_type
        self.description = description
        self.size = size
        self.content_url = content_url
        self.storage_path = storage_path
        self.email_identity_hash = email_identity_hash
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.owner_user_id = owner_user_id
        self.created_at = created_at or datetime.now(UTC)
        self.accessed_at = accessed_at
        self.metadata = metadata or {}

    def to_dict(self) -> AttachmentMetadataDict:
        """Convert to dictionary representation.

        ``storage_path`` is redacted for externally-managed sources (for
        example, email attachments living under the mailbox directory) to
        avoid leaking absolute server filesystem paths through tool output
        like ``get_attachment_info`` or HTTP metadata responses. Only the
        basename is returned in that case; registry-managed attachments
        keep their (already-relative) sharded path.
        """
        path = self.storage_path
        if path is not None and self.source_type == "email":
            path = Path(path).name
        return {
            "attachment_id": self.attachment_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "mime_type": self.mime_type,
            "description": self.description,
            "size": self.size,
            "content_url": self.content_url,
            "storage_path": path,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "owner_user_id": self.owner_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "accessed_at": self.accessed_at.isoformat() if self.accessed_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_row(cls, row: AttachmentRowDict) -> AttachmentMetadata:
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
            email_identity_hash=row.get("email_identity_hash"),
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            owner_user_id=row.get("owner_user_id"),
            created_at=row["created_at"],
            accessed_at=row["accessed_at"],
            metadata=row["metadata"],
        )


async def _clear_email_attachment_id(
    *,
    db_context: DatabaseExecutor,
    message_id_header: str,
    attachment_id: str,
) -> None:
    """Clear the given ``attachment_id`` from ``received_emails.attachment_info``.

    Called from ``delete_attachment`` when an email-sourced attachment is
    removed from the registry. The external mailbox file stays on disk, but
    the email row must no longer advertise the now-dangling registry ID, or
    ``get_full_document_content`` would surface a broken handle that 404s.
    """
    row = await db_context.fetch_one(
        select(
            received_emails_table.c.id,
            received_emails_table.c.attachment_info,
        ).where(received_emails_table.c.message_id_header == message_id_header)
    )
    if not row or not row.get("attachment_info"):
        return

    # Per-entry validation: a malformed legacy sibling entry must not
    # make the post-delete cleanup raise (the registry row is already
    # gone, so a 500 here would leave a broken-but-advertised id in the
    # email row). Preserve the raw bytes for entries we don't touch so
    # we don't accidentally drop data we can't regenerate.
    pairs = parse_attachment_infos_with_raw(
        row["attachment_info"], context=f"message_id={message_id_header}"
    )
    updated = False
    new_entries: list[Any] = []
    for raw, parsed in pairs:
        if parsed is not None and parsed.attachment_id == attachment_id:
            parsed.attachment_id = None
            new_entries.append(parsed.model_dump())
            updated = True
        else:
            new_entries.append(raw)

    if updated:
        await db_context.execute(
            update(received_emails_table)
            .where(received_emails_table.c.id == row["id"])
            .values(attachment_info=new_entries)
        )


class AttachmentRegistry:
    """Registry for managing attachment metadata and file storage."""

    def __init__(
        self,
        storage_path: str,
        db_engine: AsyncEngine,
        config: AttachmentRegistryConfig | None = None,
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
        allowed_types = attachment_config.get("allowed_mime_types")
        if allowed_types and isinstance(allowed_types, list):
            self.allowed_mime_types = set(allowed_types)
        else:
            self.allowed_mime_types = DEFAULT_ALLOWED_MIME_TYPES

        email_base = attachment_config.get("email_attachment_base_path")
        self.email_attachment_base_path: Path | None = (
            Path(email_base).resolve() if email_base else None
        )

        logger.info(
            f"AttachmentRegistry initialized with storage path: {self.storage_path}, "
            f"max_file_size: {self.max_file_size // (1024 * 1024)}MB, "
            f"max_multimodal_size: {self.max_multimodal_size // (1024 * 1024)}MB, "
            f"allowed_types: {len(self.allowed_mime_types)} types"
        )

    @property
    def media_size_limit(self) -> int:
        """The bound on any MIME type in ``MULTIMODAL_MIME_PREFIXES``."""
        return min(self.max_file_size, self.max_multimodal_size)

    def size_limit_for_mime(self, content_type: str | None) -> int:
        """The largest accepted size for an attachment of this MIME type.

        Media is bounded by ``max_multimodal_size`` because there is nothing to do
        with an oversized image, recording or video but send it to a model, and the
        provider rejects it there. Enforcing the bound at registration turns that
        into an explicit size error at upload rather than a failed turn later.

        A document keeps ``max_file_size``: its text can be extracted without a
        model, so a size only a model objects to is not a reason to refuse it.
        """
        if content_type and content_type.startswith(MULTIMODAL_MIME_PREFIXES):
            return self.media_size_limit
        return self.max_file_size

    @staticmethod
    def _owner_visibility_clause(acting_user_id: str | None) -> ColumnElement[bool]:
        """SQL predicate restricting rows to those the actor may see/operate on.

        Ownerless rows (``owner_user_id IS NULL``) are visible to everyone. An
        owned row is visible only to a matching actor; ``acting_user_id=None``
        (no user context) sees ownerless rows only. This is the single
        chokepoint the registry enforces on every public read/mutate accessor.
        """
        ownerless = attachment_metadata_table.c.owner_user_id.is_(None)
        if acting_user_id is None:
            return ownerless
        return or_(
            ownerless,
            attachment_metadata_table.c.owner_user_id == acting_user_id,
        )

    async def register_attachment(
        self,
        db_context: DatabaseExecutor,
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
        owner_user_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Register a new attachment in the metadata database.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Unique attachment identifier
            source_type: Source of attachment ("user", "tool", "script", "email")
            source_id: Source identifier (user_id, tool_name, email message-id,
                etc.)
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
        # Email attachments uniquely identify themselves by a bounded hash
        # of ``(source_id, storage_path)``; see
        # ``uix_attachment_metadata_email_identity``. Other source types
        # leave the hash NULL.
        email_identity_hash: str | None = None
        if source_type == "email" and storage_path is not None:
            email_identity_hash = compute_email_identity_hash(source_id, storage_path)

        attachment_metadata = AttachmentMetadata(
            attachment_id=attachment_id,
            source_type=source_type,
            source_id=source_id,
            mime_type=mime_type,
            description=description,
            size=size,
            content_url=content_url,
            storage_path=storage_path,
            email_identity_hash=email_identity_hash,
            conversation_id=conversation_id,
            message_id=message_id,
            owner_user_id=owner_user_id,
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
            email_identity_hash=attachment_metadata.email_identity_hash,
            conversation_id=attachment_metadata.conversation_id,
            message_id=attachment_metadata.message_id,
            owner_user_id=attachment_metadata.owner_user_id,
            created_at=attachment_metadata.created_at,
            metadata=attachment_metadata.metadata,
        )

        await db_context.execute(insert_stmt)

        logger.info(
            f"Registered attachment {attachment_id} from {source_type}:{source_id}"
        )
        logger.info(f"register_attachment return type: {type(attachment_metadata)}")
        return attachment_metadata

    async def get_attachment(
        self,
        db_context: DatabaseExecutor,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> AttachmentMetadata | None:
        """
        Get attachment metadata by ID, enforcing owner scoping.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. An owned attachment is returned only when it
                matches; a mismatch (including ``None`` actor) is reported as
                not-found (``None``).

        Returns:
            AttachmentMetadata if found and visible to the actor, None otherwise
        """
        query = select(attachment_metadata_table).where(
            and_(
                attachment_metadata_table.c.attachment_id == attachment_id,
                self._owner_visibility_clause(acting_user_id),
            )
        )

        row = await db_context.fetch_one(query)
        if not row:
            return None

        return AttachmentMetadata.from_row(cast("AttachmentRowDict", row))

    async def get_attachments(
        self,
        db_context: DatabaseExecutor,
        attachment_ids: list[str],
        *,
        acting_user_id: str | None,
    ) -> dict[str, AttachmentMetadata]:
        """Fetch metadata for multiple attachments in a single query.

        ``acting_user_id`` acts as a filter: owned rows appear only for a
        matching actor, ownerless rows appear for everyone, and ``None`` sees
        ownerless rows only. Returns a mapping of attachment_id to metadata;
        ids without a visible row are omitted from the result.
        """
        if not attachment_ids:
            return {}

        query = select(attachment_metadata_table).where(
            and_(
                attachment_metadata_table.c.attachment_id.in_(attachment_ids),
                self._owner_visibility_clause(acting_user_id),
            )
        )
        rows = await db_context.fetch_all(query)
        return {
            row["attachment_id"]: AttachmentMetadata.from_row(
                cast("AttachmentRowDict", row)
            )
            for row in rows
        }

    async def list_attachments(
        self,
        db_context: DatabaseExecutor,
        *,
        acting_user_id: str | None,
        conversation_id: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
    ) -> list[AttachmentMetadata]:
        """
        List attachments with optional filtering.

        Args:
            db_context: DatabaseExecutor context
            acting_user_id: Acts as an owner filter — owned rows appear only for
                a matching actor, ownerless rows for everyone, ``None`` for
                ownerless only.
            conversation_id: Filter by conversation
            source_type: Filter by source type ("user", "tool", "script")
            limit: Maximum number of results

        Returns:
            List of AttachmentMetadata objects
        """
        # Build query with optional filters
        query = select(attachment_metadata_table).where(
            self._owner_visibility_clause(acting_user_id)
        )

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
        return [
            AttachmentMetadata.from_row(cast("AttachmentRowDict", row)) for row in rows
        ]

    async def get_recent_attachments_for_conversation(
        self,
        db_context: DatabaseExecutor,
        conversation_id: str,
        max_age: datetime,
        *,
        acting_user_id: str | None,
    ) -> list[AttachmentMetadata]:
        """
        Get recent attachments for a conversation within a time window.

        Args:
            db_context: DatabaseExecutor context
            conversation_id: Conversation identifier
            max_age: Cutoff time - only attachments created after this time are returned
            acting_user_id: Acts as an owner filter — owned rows appear only for
                a matching actor, ownerless rows for everyone, ``None`` for
                ownerless only.

        Returns:
            List of AttachmentMetadata objects ordered by creation time (newest first)
        """
        query = (
            select(attachment_metadata_table)
            .where(attachment_metadata_table.c.conversation_id == conversation_id)
            .where(attachment_metadata_table.c.created_at >= max_age)
            .where(self._owner_visibility_clause(acting_user_id))
            .order_by(attachment_metadata_table.c.created_at.desc())
        )

        rows = await db_context.fetch_all(query)
        return [
            AttachmentMetadata.from_row(cast("AttachmentRowDict", row)) for row in rows
        ]

    async def register_user_attachment(
        self,
        db_context: DatabaseExecutor,
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
            db_context: DatabaseExecutor context
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
        attachment_data = await self._store_file_only(
            content, filename, mime_type, media_limited=True
        )

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
        db_context: DatabaseExecutor,
        attachment_id: str,
        tool_name: str,
        mime_type: str,
        description: str,
        size: int,
        content_url: str,
        storage_path: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        owner_user_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Register a tool-generated attachment.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier (from AttachmentService)
            tool_name: Name of the tool that created it
            mime_type: MIME type
            description: Description
            size: Size in bytes
            content_url: URL for retrieval
            storage_path: File system path
            conversation_id: Associated conversation
            message_id: Associated message
            owner_user_id: Canonical owner (personal-data tools set this;
                ``None`` keeps the attachment ownerless).
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
            owner_user_id=owner_user_id,
            metadata=metadata or {},
        )

    async def get_attachment_content(
        self,
        db_context: DatabaseExecutor,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> bytes | None:
        """
        Get attachment content by ID, enforcing owner scoping.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. Owned content is returned only for a matching
                actor; otherwise reported as not-found.

        Returns:
            File content bytes if found, None otherwise
        """
        # First verify access
        metadata = await self.get_attachment(
            db_context, attachment_id, acting_user_id=acting_user_id
        )
        if not metadata:
            return None

        # Get content from file system
        file_path = self.get_attachment_path(
            attachment_id,
            stored_path=metadata.storage_path,
            source_type=metadata.source_type,
        )
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
        db_context: DatabaseExecutor,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> bool:
        """
        Delete an attachment (metadata and file), enforcing owner scoping.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. An owned attachment is deleted only by a matching
                actor; a mismatch is reported as not-found (``False``) and
                leaves the row untouched.

        Returns:
            True if deleted, False if not found
        """
        # Load metadata before deletion so we can locate the file (including
        # external paths like email attachments) after the row is gone. The
        # owner-scoped read here also means a non-matching actor sees no
        # metadata and the scoped DELETE below cannot remove the row.
        metadata = await self.get_attachment(
            db_context, attachment_id, acting_user_id=acting_user_id
        )
        stored_path = metadata.storage_path if metadata else None
        source_type = metadata.source_type if metadata else None
        source_id = metadata.source_id if metadata else None

        conditions = [
            attachment_metadata_table.c.attachment_id == attachment_id,
            self._owner_visibility_clause(acting_user_id),
        ]

        delete_stmt = delete(attachment_metadata_table).where(and_(*conditions))

        async def _delete_row_and_references(txn: DatabaseTransaction) -> bool:
            """Remove the registry row and any back-reference to it, together.

            For an email attachment the ``received_emails.attachment_info`` JSON
            still names the deleted ``attachment_id``; if that cleanup were a
            separate commit, a failure would leave the email advertising a
            handle that no longer resolves.
            """
            result = await txn.execute(delete_stmt)
            if result.rowcount == 0:
                return False
            if source_type == "email" and source_id:
                await _clear_email_attachment_id(
                    db_context=txn,
                    message_id_header=source_id,
                    attachment_id=attachment_id,
                )
            return True

        success = await db_context.atomic(_delete_row_and_references)
        file_deleted = False

        if success:
            # The file is deleted only once the database state is consistent,
            # since this part cannot be rolled back.
            file_deleted = self._delete_attachment_file(
                attachment_id,
                stored_path=stored_path,
                source_type=source_type,
            )
            logger.info(
                f"Deleted attachment {attachment_id} (db: {success}, file: {file_deleted})"
            )
        else:
            logger.info(
                f"Failed to delete attachment {attachment_id} - not found or access denied"
            )

        return success

    async def _update_access_time(
        self,
        db_context: DatabaseExecutor,
        attachment_id: str,
        acting_user_id: str | None,
    ) -> None:
        """Update the access time for an attachment (owner-scoped).

        Silently ignores cancellation and database errors during shutdown,
        since access time tracking is not critical to application functionality.
        """
        try:
            update_stmt = (
                update(attachment_metadata_table)
                .where(
                    and_(
                        attachment_metadata_table.c.attachment_id == attachment_id,
                        self._owner_visibility_clause(acting_user_id),
                    )
                )
                .values(accessed_at=datetime.now(UTC))
            )
            await db_context.execute(update_stmt)
        except asyncio.CancelledError:
            # Operation cancelled during shutdown - this is fine, access time isn't critical
            pass
        except Exception:
            # Silently ignore other errors (e.g., connection closed during teardown)
            # Access time tracking is informational and shouldn't break operations
            pass

    async def update_access_time_background(
        self,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> None:
        """
        Update attachment access time in a background task (owner-scoped).

        Creates its own database context since this is called from FastAPI
        BackgroundTasks after the request context is closed.

        Args:
            attachment_id: The attachment ID to update
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. An owned row is touched only for a matching actor.
        """
        try:
            db = Database(engine=self.db_engine)
            await self._update_access_time(db, attachment_id, acting_user_id)
        except Exception as e:
            # Log but don't fail - access time tracking is not critical
            logger.debug(
                f"Background access time update failed for {attachment_id}: {e}"
            )

    async def cleanup_orphaned_attachments(self, db_context: DatabaseExecutor) -> int:
        """
        Clean up file system attachments that are no longer referenced in the database.
        Uses AttachmentService to clean up orphaned files based on current database references.

        Args:
            db_context: DatabaseExecutor context

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
        db_context: DatabaseExecutor,
        attachment_id: str,
        conversation_id: str,
        *,
        acting_user_id: str | None,
    ) -> bool:
        """
        Update an attachment's conversation_id for security linking.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier
            conversation_id: New conversation ID to link to
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. An owned row is relinked only by a matching actor.

        Returns:
            True if updated successfully, False if attachment not found
        """
        update_stmt = (
            update(attachment_metadata_table)
            .where(
                and_(
                    attachment_metadata_table.c.attachment_id == attachment_id,
                    self._owner_visibility_clause(acting_user_id),
                )
            )
            .values(conversation_id=conversation_id)
        )

        result = await db_context.execute(update_stmt)
        success = result.rowcount > 0

        if success:
            logger.info(
                f"Linked attachment {attachment_id} to conversation {conversation_id}"
            )

        return success

    async def claim_unlinked_attachment(
        self,
        db_context: DatabaseExecutor,
        attachment_id: str,
        conversation_id: str,
        *,
        acting_user_id: str | None,
        required_source_id: str = "api_user",
    ) -> AttachmentMetadata | None:
        """
        Atomically claim an unlinked attachment for a conversation.

        This prevents race conditions by using a single atomic update operation
        that only succeeds if the attachment is still unlinked and matches criteria.

        Args:
            db_context: DatabaseExecutor context
            attachment_id: Attachment identifier
            conversation_id: Conversation to link the attachment to
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. An owned row is claimable only by a matching
                actor.
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
                    self._owner_visibility_clause(acting_user_id),
                )
            )
            .values(
                conversation_id=conversation_id,
                accessed_at=datetime.now(UTC),
            )
        )

        async def _claim(txn: DatabaseTransaction) -> AttachmentMetadata | None:
            """Atomically claim and fetch the attachment in one statement.

            UPDATE ... RETURNING makes the claim and metadata read one operation,
            so a retry can't find the attachment already claimed (conversation_id
            no longer NULL) — preventing orphaned claims on fetch failure.
            """
            stmt_with_returning = update_stmt.returning(attachment_metadata_table)
            result = await txn.execute(stmt_with_returning)
            row = result.one_or_none()

            if row:
                logger.info(
                    f"Successfully claimed attachment {attachment_id} for conversation {conversation_id}"
                )
                return AttachmentMetadata.from_row(cast("AttachmentRowDict", row))

            return None

        return await db_context.atomic(_claim)

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
        owner_user_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
        metadata: dict[str, Any] | None = None,
    ) -> AttachmentMetadata:
        """
        Register a tool-generated attachment using internal database context.

        This is a convenience method that creates its own Database.
        Use this from processing.py and other places that don't already have a context.
        """
        db_context = Database(self.db_engine)
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
            owner_user_id=owner_user_id,
            metadata=metadata,
        )

    async def get_attachment_with_context(
        self,
        attachment_id: str,
        *,
        acting_user_id: str | None,
    ) -> AttachmentMetadata | None:
        """
        Get attachment metadata by ID using internal database context.

        This is a convenience method that creates its own Database.

        Args:
            attachment_id: Attachment identifier
            acting_user_id: Canonical id of the acting user, or ``None`` for no
                user context. Owned rows are visible only to a matching actor.
        """
        db_context = Database(self.db_engine)
        return await self.get_attachment(
            db_context, attachment_id, acting_user_id=acting_user_id
        )

    async def store_and_register_tool_attachment(
        self,
        file_content: bytes,
        filename: str,
        content_type: str,
        tool_name: str,
        description: str | None = None,
        conversation_id: str | None = None,
        message_id: int | None = None,
        owner_user_id: str | None = None,
        # ast-grep-ignore: no-dict-any - Free-form JSON metadata with arbitrary keys from various callers
        metadata: dict[str, Any] | None = None,
        db_context: DatabaseExecutor | None = None,
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
            owner_user_id: Canonical owner (personal-data tools set this;
                ``None`` keeps the attachment ownerless).
            metadata: Additional metadata
            db_context: Optional Database to use for registration

        Returns:
            AttachmentMetadata for the stored and registered attachment
        """
        # First store the file
        file_metadata = await self._store_file_only(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            media_limited=False,
        )

        # Merge metadata from file storage (contains original_filename) with provided metadata
        final_metadata = file_metadata.metadata.copy() if file_metadata.metadata else {}
        if metadata:
            final_metadata.update(metadata)

        # Then register it in the database
        if db_context:
            return await self.register_tool_attachment(
                db_context=db_context,
                attachment_id=file_metadata.attachment_id,
                tool_name=tool_name,
                mime_type=content_type,
                description=description or f"Output from {tool_name}",
                size=len(file_content),
                content_url=file_metadata.content_url
                or f"/api/attachments/{file_metadata.attachment_id}",
                storage_path=str(file_metadata.storage_path)
                if file_metadata.storage_path
                else None,
                conversation_id=conversation_id,
                message_id=message_id,
                owner_user_id=owner_user_id,
                metadata=final_metadata,
            )
        else:
            return await self.register_tool_attachment_with_context(
                attachment_id=file_metadata.attachment_id,
                tool_name=tool_name,
                mime_type=content_type,
                description=description or f"Output from {tool_name}",
                size=len(file_content),
                content_url=file_metadata.content_url
                or f"/api/attachments/{file_metadata.attachment_id}",
                storage_path=str(file_metadata.storage_path)
                if file_metadata.storage_path
                else None,
                conversation_id=conversation_id,
                message_id=message_id,
                owner_user_id=owner_user_id,
                metadata=final_metadata,
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

            size_limit = self.size_limit_for_mime(file.content_type)
            if file_size > size_limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"File size {file_size} bytes exceeds maximum allowed size of {size_limit} bytes",
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
        *,
        media_limited: bool,
    ) -> AttachmentMetadata:
        """
        Store raw bytes as an attachment file (private method for internal use).

        Args:
            file_content: Raw file content bytes
            filename: Original filename
            content_type: MIME type of the file
            media_limited: Whether `max_multimodal_size` applies. True for what a
                user sends in, since oversized media has nowhere to go but a model
                that will refuse it. False for what a tool produces: a generated
                video too large to inject is still the result the user asked for,
                and discarding it to protect a later injection would lose the work.

        Returns:
            AttachmentMetadata object

        Raises:
            ValueError: If file validation fails
        """
        # Basic validation
        size_limit = (
            self.size_limit_for_mime(content_type)
            if media_limited
            else self.max_file_size
        )
        if len(file_content) > size_limit:
            raise ValueError(
                f"File size {len(file_content)} bytes exceeds maximum allowed size of {size_limit} bytes"
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
                status_code=500, detail=f"Failed to store attachment: {e!s}"
            ) from e

    async def resolve_attachment_path(
        self,
        attachment_id: str,
        db_context: DatabaseExecutor | None = None,
        *,
        acting_user_id: str | None,
    ) -> Path | None:
        """Async counterpart of ``get_attachment_path`` that resolves the
        ``storage_path`` column internally, enforcing owner scoping.

        Use this from call sites that don't already have the attachment
        metadata handy (for example, URL→data-URI conversion in the
        processing layer). External-path attachments such as email files
        are surfaced transparently without the caller needing to pre-fetch
        metadata. Owned attachments resolve only for a matching actor.
        """
        metadata: AttachmentMetadata | None = None
        if db_context is None:
            own_db_context = Database(engine=self.db_engine)
            metadata = await self.get_attachment(
                own_db_context, attachment_id, acting_user_id=acting_user_id
            )
        else:
            metadata = await self.get_attachment(
                db_context, attachment_id, acting_user_id=acting_user_id
            )

        # A row invisible to the actor (owned by someone else, or absent) must
        # not resolve to a file path — otherwise the sharded-storage fallback in
        # ``get_attachment_path`` would serve an owned attachment to a non-owner.
        if metadata is None:
            return None
        return self.get_attachment_path(
            attachment_id,
            stored_path=metadata.storage_path,
            source_type=metadata.source_type,
        )

    def get_attachment_path(
        self,
        attachment_id: str,
        stored_path: str | None = None,
        source_type: str | None = None,
    ) -> Path | None:
        """
        Get the file system path for an attachment by ID.

        Args:
            attachment_id: The attachment UUID
            stored_path: Optional externally-managed file path taken from
                ``attachment_metadata.storage_path``. When provided and the file
                exists, it is returned directly. This supports attachments whose
                files live outside the registry's sharded storage (for example,
                email attachments saved to the mailbox directory). For call
                sites without pre-fetched metadata, use
                :meth:`resolve_attachment_path` instead, which performs the
                lookup internally.
            source_type: Optional ``attachment_metadata.source_type``. The
                ``stored_path`` fast-path is gated to ``"email"`` only:
                those attachments live in an externally-managed mailbox
                directory. For every other source type (``"user"``,
                ``"tool"``, ``"script"``), ``stored_path`` is ignored
                here and we fall through to the sharded
                ``{self.storage_path}/{prefix}/{id}.*`` lookup so rows
                containing arbitrary absolute paths can't serve files
                from outside the registry-managed directory.

        Returns:
            Path to the attachment file, or None if not found
        """
        # Only honor ``stored_path`` for email attachments — every other
        # caller is managed by the sharded storage layout below, and
        # trusting ``stored_path`` for them would let a poisoned
        # ``attachment_metadata.storage_path`` serve arbitrary files
        # through ``/api/attachments/{id}`` / ``get_attachment_content``.
        if stored_path and source_type == "email":
            candidate = Path(stored_path)
            # Current email rows persist absolute paths; legacy rows
            # from deployments that configured ``attachment_storage_path``
            # relatively may still be relative. Resolve those against
            # ``email_attachment_base_path`` (which ``AppConfig`` pins to
            # an absolute path at load time) so restarts from a different
            # cwd don't break them. When no base is configured we fall
            # back to using the relative path as-is, preserving pre-PR
            # behavior.
            if (
                not candidate.is_absolute()
                and self.email_attachment_base_path is not None
            ):
                candidate = self.email_attachment_base_path / candidate
            if candidate.is_file():
                return candidate

        try:
            uuid.UUID(attachment_id)
        except ValueError:
            logger.warning(f"Invalid attachment ID format: {attachment_id}")
            return None

        hash_prefix = attachment_id[:2]
        hash_dir = self.storage_path / hash_prefix

        if not hash_dir.is_dir():
            logger.info(f"Attachment file not found: {attachment_id}")
            return None

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

    def _delete_attachment_file(
        self,
        attachment_id: str,
        stored_path: str | None = None,
        source_type: str | None = None,
    ) -> bool:
        """
        Delete an attachment file (private method).

        Only unlinks files the registry owns. Files for externally-managed
        sources stay on disk so the producer can keep using them — for
        example, an email attachment lives under the mailbox directory and
        the ``received_emails.attachment_info`` record still references it,
        so the registry must not remove it here.

        Ownership is decided from ``source_type`` (the authoritative
        producer marker) with a defensive path-containment fallback: even
        if some future ``source_type`` is flagged as registry-owned, we
        still refuse to unlink a file outside ``self.storage_path``.

        Args:
            attachment_id: The attachment UUID
            stored_path: Optional ``storage_path`` from the attachment
                metadata.
            source_type: Optional ``attachment_metadata.source_type``. When
                this is ``"email"`` the file is externally owned and the
                unlink is skipped regardless of where it lives.

        Returns:
            True if a registry-managed file was deleted, False otherwise.
        """
        if source_type == "email":
            logger.info(
                f"Skipping file unlink for email attachment {attachment_id} "
                f"(externally owned by received_emails; storage_path={stored_path})"
            )
            return False
        if stored_path and not self._path_is_managed(stored_path):
            logger.info(
                f"Skipping file unlink for externally-managed attachment "
                f"{attachment_id} (storage_path={stored_path})"
            )
            return False

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

    def _path_is_managed(self, candidate: str) -> bool:
        """Return True when ``candidate`` is inside the registry's storage dir.

        ``_store_file_only`` writes ``storage_path`` as a relative path (e.g.
        ``"c7/<uuid>.txt"``) for registry-managed uploads, while email
        attachments are registered with an absolute external path (e.g.
        ``"/mnt/data/mailbox/attachments/..."``). Treat relative paths as
        managed by convention; resolve absolute paths against
        ``self.storage_path`` to decide.
        """
        path = Path(candidate)
        if not path.is_absolute():
            return True
        try:
            resolved = path.resolve()
            base = self.storage_path.resolve()
        except (OSError, ValueError):
            return False
        try:
            resolved.relative_to(base)
        except ValueError:
            return False
        return True

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
