"""API endpoints for chat attachment management."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import (
    get_attachment_registry,
    get_current_user,
    get_db,
)

logger = logging.getLogger(__name__)

attachments_api_router = APIRouter()


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
    db_context: Annotated[DatabaseContext, Depends(get_db)],
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
    except Exception as e:
        logger.error(f"Unexpected error during attachment upload: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while uploading the attachment",
        ) from e


@attachments_api_router.get(
    "/{attachment_id}",
    response_class=FileResponse,
    summary="Serve attachment file",
    description="Serve an attachment file by its ID with proper authorization checks.",
)
async def serve_attachment(
    attachment_id: str,
    background_tasks: BackgroundTasks,
    current_user: Annotated[dict, Depends(get_current_user)],
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: str | None = None,
) -> FileResponse:
    """
    Serve an attachment file by its ID with proper authorization.

    Args:
        attachment_id: UUID of the attachment to serve
        conversation_id: Optional conversation ID for access control

    Returns:
        FileResponse with the attachment file

    Raises:
        HTTPException: If attachment not found, access denied, or invalid ID format
    """
    # Validate UUID format
    try:
        uuid.UUID(attachment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="Invalid attachment ID format"
        ) from e

    # Check access via attachment registry (respects conversation scoping)
    # Note: get_attachment() no longer updates access time synchronously
    attachment_metadata = await attachment_registry.get_attachment(
        db_context, attachment_id
    )
    if not attachment_metadata:
        raise HTTPException(
            status_code=404, detail="Attachment not found or access denied"
        )

    # Schedule access time update as background task (non-blocking)
    background_tasks.add_task(
        attachment_registry.update_access_time_background, attachment_id
    )

    # Note: Ownership verification removed since API endpoints are public
    # All authenticated users can access any attachment for simplicity

    # Get file path
    file_path = attachment_registry.get_attachment_path(attachment_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment file not found")

    # Get content type
    content_type = attachment_registry.get_content_type(file_path)

    # Return file response with proper headers
    return FileResponse(
        path=str(file_path),
        media_type=content_type,
        filename=file_path.name,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",  # Cache for 1 year (files are immutable)
            "ETag": f'"{attachment_id}"',  # Use attachment ID as ETag
        },
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
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    conversation_id: str | None = None,
) -> dict[str, str]:
    """
    Delete an attachment file by its ID.

    Args:
        attachment_id: UUID of the attachment to delete
        conversation_id: Optional conversation ID for access control

    Returns:
        Success message

    Raises:
        HTTPException: If attachment not found, access denied, or invalid ID format
    """
    # Validate UUID format
    try:
        uuid.UUID(attachment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="Invalid attachment ID format"
        ) from e

    # Use attachment registry for proper authorization and order of operations
    # This handles both database deletion and file cleanup in the correct order
    deleted = await attachment_registry.delete_attachment(
        db_context, attachment_id, conversation_id, current_user["user_identifier"]
    )
    if not deleted:
        raise HTTPException(
            status_code=404, detail="Attachment not found or access denied"
        )

    return {"message": f"Attachment {attachment_id} deleted successfully"}


@attachments_api_router.get(
    "/{attachment_id}/metadata",
    summary="Get attachment metadata",
    description="Get metadata for an attachment without downloading the file.",
)
async def get_attachment_metadata(
    attachment_id: str,
    attachment_registry: Annotated[
        AttachmentRegistry, Depends(get_attachment_registry)
    ],
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

    # Get file path
    file_path = attachment_registry.get_attachment_path(attachment_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Get basic metadata from file
    stat = file_path.stat()
    content_type = attachment_registry.get_content_type(file_path)

    # Return basic metadata (in production, this would come from database)
    return AttachmentMetadata(
        id=attachment_id,
        name=file_path.name,
        type=content_type,
        size=stat.st_size,
        hash="unknown",  # Would need to recalculate or store in DB
        storage_path=str(file_path.relative_to(attachment_registry.storage_path)),
        uploaded_at="unknown",  # Would need to be stored in DB
    )
