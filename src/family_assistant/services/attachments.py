"""
Service for handling chat attachment file operations.
Provides functionality to store, retrieve, and serve attachment files.
"""

import hashlib
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

logger = logging.getLogger(__name__)

# Maximum file size: 100MB
MAX_FILE_SIZE = 100 * 1024 * 1024

# Allowed MIME types for attachments
ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "text/plain",
    "text/markdown",
    "application/pdf",
}


class AttachmentService:
    """Service for managing chat attachment files."""

    def __init__(self, storage_path: str) -> None:
        """
        Initialize the attachment service.

        Args:
            storage_path: Base directory for storing attachment files
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"AttachmentService initialized with storage path: {self.storage_path}"
        )

    def _calculate_content_hash(self, content: bytes) -> str:
        """Calculate SHA-256 hash of file content."""
        return hashlib.sha256(content).hexdigest()

    def _get_file_path(self, attachment_id: str, filename: str) -> Path:
        """
        Generate file storage path for an attachment.

        Uses date-based directory structure: YYYY/MM/attachment_id.ext
        """
        now = datetime.now(timezone.utc)
        year_month_dir = self.storage_path / str(now.year) / f"{now.month:02d}"
        year_month_dir.mkdir(parents=True, exist_ok=True)

        # Use attachment_id as filename with original extension
        file_ext = Path(filename).suffix.lower()
        return year_month_dir / f"{attachment_id}{file_ext}"

    def _validate_file(self, file: UploadFile) -> None:
        """
        Validate uploaded file for type and size restrictions.

        Args:
            file: The uploaded file to validate

        Raises:
            HTTPException: If file validation fails
        """
        # Check MIME type
        if file.content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{file.content_type}' not allowed. "
                f"Allowed types: {', '.join(ALLOWED_MIME_TYPES)}",
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

            if file_size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File size {file_size} bytes exceeds maximum allowed size of {MAX_FILE_SIZE} bytes",
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

    async def store_attachment(self, file: UploadFile) -> dict[str, Any]:
        """
        Store an uploaded attachment file.

        Args:
            file: The uploaded file

        Returns:
            Dictionary containing attachment metadata

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

            # Calculate content hash
            content_hash = self._calculate_content_hash(file_content)

            # Get storage path
            file_path = self._get_file_path(attachment_id, safe_filename)

            # Write file to disk
            with open(file_path, "wb") as f:
                f.write(file_content)

            # Create attachment metadata
            attachment_metadata = {
                "id": attachment_id,
                "name": safe_filename,
                "type": file.content_type,
                "size": len(file_content),
                "hash": f"sha256:{content_hash}",
                "storage_path": str(file_path.relative_to(self.storage_path)),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }

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
        # Search for the file in the directory structure
        # Since we don't store the exact path mapping, we need to search
        try:
            # Parse as UUID to validate format
            uuid.UUID(attachment_id)
        except ValueError:
            logger.warning(f"Invalid attachment ID format: {attachment_id}")
            return None

        # Search in year/month subdirectories
        for year_dir in self.storage_path.glob("*/"):
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.glob("*/"):
                if not month_dir.is_dir():
                    continue
                # Look for files starting with the attachment ID
                for file_path in month_dir.glob(f"{attachment_id}.*"):
                    if file_path.is_file():
                        return file_path

        logger.warning(f"Attachment file not found: {attachment_id}")
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

    def delete_attachment(self, attachment_id: str) -> bool:
        """
        Delete an attachment file.

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

    def cleanup_orphaned_files(self, referenced_attachment_ids: set[str]) -> int:
        """
        Clean up attachment files that are no longer referenced in the database.

        Args:
            referenced_attachment_ids: Set of attachment IDs that are still referenced

        Returns:
            Number of files deleted
        """
        deleted_count = 0

        for year_dir in self.storage_path.glob("*/"):
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.glob("*/"):
                if not month_dir.is_dir():
                    continue
                for file_path in month_dir.glob("*.*"):
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
