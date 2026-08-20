"""API endpoints for chat attachment management."""

import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from family_assistant.services.attachment_registry import (
    AttachmentMetadata as RegistryAttachmentMetadata,
)
from family_assistant.services.attachment_registry import (
    AttachmentRegistry,
    AttachmentTooLargeError,
)
from family_assistant.storage.database import Database
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
)

logger = logging.getLogger(__name__)

attachments_api_router = APIRouter()


# An attachment is stored whatever its type, so a served file may be one the
# browser would happily run on this origin (HTML, SVG) if it decided the type
# for itself. It is handed over as a download rather than rendered -- Starlette
# sends `Content-Disposition: attachment` whenever a filename is given -- and
# this stops the type being second-guessed either way.
_NOSNIFF = {"X-Content-Type-Options": "nosniff"}


class AttachmentUploadResponse(BaseModel):
    """Response model for attachment upload."""

    attachment_id: str
    filename: str
    content_type: str
    size: int
    url: str


class AttachmentMetadata(BaseModel):
    """Attachment metadata model."""

    id: str
    name: str
    type: str
    size: int
    hash: str
    storage_path: str
    uploaded_at: str


@attachments_api_router.post(
    "/upload",
    summary="Upload attachment",
    description="Upload a file to be used as a chat attachment. Returns attachment metadata and URL for serving.",
)
async def upload_attachment(
    file: Annotated[UploadFile, File(description="File to upload as attachment")],
    current_user: Annotated[dict, Depends(get_current_user)],
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
    db_context: Annotated[Database, Depends(get_db)],
) -> AttachmentUploadResponse:
    """
    Upload a file as a chat attachment.

    The file is validated for type and size, then stored with a unique ID.
    Returns the attachment metadata including a URL for serving the file.
    """
    try:
        # Read file content
        content = await file.read()

        # Register attachment in database via AttachmentRegistry
        # Note: conversation_id is None for uploads, will be linked when used in chat
        attachment_record = await attachment_registry.register_user_attachment(
            db_context=db_context,
            content=content,
            filename=file.filename or "uploaded_file",
            mime_type=file.content_type or "application/octet-stream",
            conversation_id=None,  # Not linked to conversation yet
            message_id=None,
            user_id=current_user["user_identifier"],
            description=f"User uploaded: {file.filename or 'file'}",
        )

        # Create response with serving URL
        attachment_url = f"/api/attachments/{attachment_record.attachment_id}"

        return AttachmentUploadResponse(
            attachment_id=attachment_record.attachment_id,
            filename=file.filename or "uploaded_file",
            content_type=attachment_record.mime_type,
            size=attachment_record.size,
            url=attachment_url,
        )

    except HTTPException:
        # Re-raise HTTPExceptions from the service
        raise
    except AttachmentTooLargeError as e:
        # The size and the limit are the whole of what a user can act on, and
        # media is held to a tighter limit than other files, so the message has
        # to reach them rather than becoming a generic failure.
        raise HTTPException(
            status_code=413,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.exception(f"Unexpected error during attachment upload: {e}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while uploading the attachment",
        ) from e


@attachments_api_router.get(
    "/{attachment_id}",
    response_class=FileResponse,
    summary="Serve attachment file",
    description="Serve an attachment file by its ID.",
)
async def serve_attachment(
    attachment_id: str,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
    db_context: Annotated[Database, Depends(get_db)],
) -> FileResponse:
    """
    Serve an attachment file by its ID.

    Args:
        attachment_id: UUID of the attachment to serve

    Returns:
        FileResponse with the attachment file

    Raises:
        HTTPException: If attachment not found or invalid ID format
    """
    # Validate UUID format
    try:
        uuid.UUID(attachment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="Invalid attachment ID format"
        ) from e

    acting_user_id = current_user["user_identifier"]

    # Check access via attachment registry (owner-scoped: an owned attachment
    # is only visible to its owner; a mismatch reads as not-found).
    # Note: get_attachment() no longer updates access time synchronously
    attachment_metadata = await attachment_registry.get_attachment(
        db_context, attachment_id, acting_user_id=acting_user_id
    )
    if not attachment_metadata:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Schedule access time update as background task (non-blocking)
    background_tasks.add_task(
        attachment_registry.update_access_time_background,
        attachment_id,
        acting_user_id=acting_user_id,
    )

    # Get file path (honoring externally-managed storage_path for e.g. email attachments)
    file_path = attachment_registry.get_attachment_path(
        attachment_id,
        stored_path=attachment_metadata.storage_path,
        source_type=attachment_metadata.source_type,
    )
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")

    # Get content type
    content_type = attachment_registry.get_content_type(file_path)

    # Prefer the original filename from metadata over the on-disk name:
    # the mailbox write path deliberately prefixes the index to
    # disambiguate duplicates (``1-image.png``), but clients asked for
    # the attachment by ID and expect the original name back.
    display_filename = _display_filename(attachment_metadata, file_path)

    # Owned attachments must never be shareable through a cache: a shared
    # cache keyed only on the URL could hand user A's personal file to user B
    # without ever reaching the ownership check above. Ownerless attachments
    # keep the long-lived immutable cache.
    if attachment_metadata.owner_user_id is not None:
        cache_headers = {
            "Cache-Control": "private, no-store",
            "ETag": f'"{attachment_id}"',
            **_NOSNIFF,
        }
    else:
        cache_headers = {
            "Cache-Control": "public, max-age=31536000, immutable",  # Cache for 1 year (files are immutable)
            "ETag": f'"{attachment_id}"',  # Use attachment ID as ETag
            **_NOSNIFF,
        }

    # Return file response with proper headers
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=display_filename,
        headers=cache_headers,
    )


@attachments_api_router.delete(
    "/{attachment_id}",
    summary="Delete attachment",
    description="Delete an attachment file by its ID.",
)
async def delete_attachment(
    attachment_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
    db_context: Annotated[Database, Depends(get_db)],
) -> dict[str, str]:
    """
    Delete an attachment file by its ID.

    Args:
        attachment_id: UUID of the attachment to delete

    Returns:
        Success message

    Raises:
        HTTPException: If attachment not found or invalid ID format
    """
    # Validate UUID format
    try:
        uuid.UUID(attachment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="Invalid attachment ID format"
        ) from e

    # Use attachment registry for deletion (owner-scoped: an owned attachment
    # is only deletable by its owner; a mismatch reads as not-found).
    # This handles both database deletion and file cleanup in the correct order
    deleted = await attachment_registry.delete_attachment(
        db_context, attachment_id, acting_user_id=current_user["user_identifier"]
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return {"message": f"Attachment {attachment_id} deleted successfully"}


@attachments_api_router.get(
    "/{attachment_id}/metadata",
    summary="Get attachment metadata",
    description="Get metadata for an attachment without downloading the file.",
)
async def get_attachment_metadata(
    attachment_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
    db_context: Annotated[Database, Depends(get_db)],
) -> AttachmentMetadata:
    """
    Get metadata for an attachment.

    Note: This is a placeholder implementation. In a production system,
    metadata would be stored in the database for efficient retrieval.

    Args:
        attachment_id: UUID of the attachment

    Returns:
        Attachment metadata

    Raises:
        HTTPException: If attachment not found or invalid ID format
    """
    # Validate UUID format
    try:
        uuid.UUID(attachment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="Invalid attachment ID format"
        ) from e

    # Look up the registry row so we honor externally-managed ``storage_path``
    # (for example, email attachments saved to the mailbox directory). The read
    # is owner-scoped, so an owned attachment is invisible to a non-owner and
    # must not fall through to the sharded-path lookup below.
    registry_metadata = await attachment_registry.get_attachment(
        db_context, attachment_id, acting_user_id=current_user["user_identifier"]
    )
    if registry_metadata is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = attachment_registry.get_attachment_path(
        attachment_id,
        stored_path=registry_metadata.storage_path if registry_metadata else None,
        source_type=registry_metadata.source_type if registry_metadata else None,
    )
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Get basic metadata from file
    stat = file_path.stat()
    content_type = attachment_registry.get_content_type(file_path)

    # Return basic metadata (in production, this would come from database)
    return AttachmentMetadata(
        id=attachment_id,
        name=_display_filename(registry_metadata, file_path),
        type=content_type,
        size=stat.st_size,
        hash="unknown",  # Would need to recalculate or store in DB
        storage_path=_format_storage_path(file_path, attachment_registry.storage_path),
        uploaded_at="unknown",  # Would need to be stored in DB
    )


def _display_filename(
    registry_metadata: RegistryAttachmentMetadata | None,
    file_path: Path,
) -> str:
    """Return the client-facing filename for an attachment.

    Prefers ``metadata.metadata["original_filename"]`` so downloads/
    metadata responses surface the name the user uploaded (e.g.
    ``image.png``) rather than the internal disambiguated on-disk name
    (e.g. ``1-image.png``). Falls back to the basename when the
    original name isn't stored.
    """
    if registry_metadata is not None:
        original = registry_metadata.metadata.get("original_filename")
        if isinstance(original, str) and original:
            return original
    return file_path.name


def _format_storage_path(file_path: Path, base_path: Path) -> str:
    """Return a relative path for registry-managed files; redact others.

    Registry-managed uploads are written inside ``base_path`` and expose a
    sharded relative path. For externally-managed files (for example, email
    attachments stored under the mailbox directory), the absolute server
    path must not be leaked over the public API — return just the basename.
    """
    try:
        return str(file_path.relative_to(base_path))
    except ValueError:
        return file_path.name
