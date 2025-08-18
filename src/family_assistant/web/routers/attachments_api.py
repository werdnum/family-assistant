"""API endpoints for chat attachment management."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from family_assistant.services.attachments import AttachmentService

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


def get_attachment_service(request: Request) -> AttachmentService:
    """Dependency to get the AttachmentService from app state."""
    if not hasattr(request.app.state, "attachment_service"):
        # Initialize attachment service if not already done
        config = getattr(request.app.state, "config", {})
        attachment_storage_path = config.get(
            "chat_attachment_storage_path", "/mnt/data/chat_attachments"
        )
        request.app.state.attachment_service = AttachmentService(
            attachment_storage_path
        )

    return request.app.state.attachment_service


@attachments_api_router.post(
    "/upload",
    summary="Upload attachment",
    description="Upload a file to be used as a chat attachment. Returns attachment metadata and URL for serving.",
)
async def upload_attachment(
    file: Annotated[UploadFile, File(description="File to upload as attachment")],
    attachment_service: Annotated[AttachmentService, Depends(get_attachment_service)],
) -> AttachmentUploadResponse:
    """
    Upload a file as a chat attachment.

    The file is validated for type and size, then stored with a unique ID.
    Returns the attachment metadata including a URL for serving the file.
    """
    try:
        # Store the attachment
        attachment_metadata = await attachment_service.store_attachment(file)

        # Create response with serving URL
        attachment_url = f"/api/v1/attachments/{attachment_metadata['id']}"

        return AttachmentUploadResponse(
            attachment_id=attachment_metadata["id"],
            filename=attachment_metadata["name"],
            content_type=attachment_metadata["type"],
            size=attachment_metadata["size"],
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
    description="Serve an attachment file by its ID. Returns the file with appropriate content-type headers.",
)
async def serve_attachment(
    attachment_id: str,
    attachment_service: Annotated[AttachmentService, Depends(get_attachment_service)],
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

    # Get file path
    file_path = attachment_service.get_attachment_path(attachment_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Get content type
    content_type = attachment_service.get_content_type(file_path)

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
    attachment_service: Annotated[AttachmentService, Depends(get_attachment_service)],
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

    # Delete the file
    deleted = attachment_service.delete_attachment(attachment_id)
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
    attachment_service: Annotated[AttachmentService, Depends(get_attachment_service)],
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
    file_path = attachment_service.get_attachment_path(attachment_id)
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Get basic metadata from file
    stat = file_path.stat()
    content_type = attachment_service.get_content_type(file_path)

    # Return basic metadata (in production, this would come from database)
    return AttachmentMetadata(
        id=attachment_id,
        name=file_path.name,
        type=content_type,
        size=stat.st_size,
        hash="unknown",  # Would need to recalculate or store in DB
        storage_path=str(file_path.relative_to(attachment_service.storage_path)),
        uploaded_at="unknown",  # Would need to be stored in DB
    )
