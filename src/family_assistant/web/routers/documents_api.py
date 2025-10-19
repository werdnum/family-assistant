import json
import logging
import pathlib
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.sql import functions as func

from family_assistant.indexing.ingestion import process_document_ingestion_request
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import DocumentRecord, get_document_by_id
from family_assistant.web.dependencies import get_db
from family_assistant.web.models import DocumentUploadResponse

logger = logging.getLogger(__name__)
documents_api_router = APIRouter()


class DocumentModel(BaseModel):
    """Basic document information."""

    id: int
    source_type: str
    source_id: str
    title: str | None = None
    source_uri: str | None = None
    created_at: datetime | None = None
    added_at: datetime
    doc_metadata: dict | None = None


class DocumentListResponse(BaseModel):
    documents: list[DocumentModel]
    total: int


@documents_api_router.get("/")
async def list_documents(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    limit: int = 100,
    offset: int = 0,
    source_type: str | None = None,
) -> DocumentListResponse:
    """List stored documents."""
    stmt = (
        select(
            DocumentRecord.id,
            DocumentRecord.source_type,
            DocumentRecord.source_id,
            DocumentRecord.title,
            DocumentRecord.source_uri,
            DocumentRecord.created_at,
            DocumentRecord.added_at,
            DocumentRecord.doc_metadata,
        )
        .order_by(DocumentRecord.added_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if source_type:
        stmt = stmt.where(DocumentRecord.source_type == source_type)
    rows = await db_context.fetch_all(stmt)
    documents = [DocumentModel(**row) for row in rows]
    total_stmt = select(func.count().label("count")).select_from(DocumentRecord)
    if source_type:
        total_stmt = total_stmt.where(DocumentRecord.source_type == source_type)
    total_row = await db_context.fetch_one(total_stmt)
    total = total_row["count"] if total_row else 0
    return DocumentListResponse(documents=documents, total=total)


@documents_api_router.get("/{document_id}")
async def get_document(
    document_id: int, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> dict:
    """Get a document by ID with detailed information including embeddings."""
    record = await get_document_by_id(db_context, document_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    # Prepare embeddings data
    embeddings_data = []
    full_text = None
    full_text_type = None
    full_text_warning = None

    if record.embeddings:
        # Look for raw content types first (these should contain full text)
        raw_types = [
            "raw_note_text",
            "raw_body_text",
            "raw_file_text",
            "extracted_markdown_content",  # Full PDF text
            "fetched_content_markdown",  # Full web page content
            "original_document_file",  # Might have text content
        ]

        # First try to find raw content
        for embedding in record.embeddings:
            if embedding.embedding_type in raw_types and embedding.content:
                full_text = embedding.content
                full_text_type = embedding.embedding_type
                # Check if this was stored without embedding due to size
                if embedding.embedding_model in {
                    "text_only_too_long",
                    "text_only_error",
                }:
                    full_text_warning = (
                        f"Note: This content was too large to embed "
                        f"(reason: {embedding.embedding_model})"
                    )
                break

        # If no raw content found, fall back to chunk reconstruction
        if not full_text:
            chunks = [
                e
                for e in record.embeddings
                if e.embedding_type == "content_chunk" and e.content
            ]
            if chunks:
                # Sort by chunk index
                chunks.sort(key=lambda x: x.chunk_index or 0)
                full_text = "".join(c.content for c in chunks if c.content)
                full_text_type = "reconstructed_from_chunks"
                full_text_warning = (
                    "Note: This text was reconstructed from overlapping chunks. "
                    "There may be duplicate content at chunk boundaries."
                )

        # Prepare all embeddings for display
        for embedding in record.embeddings:
            embeddings_data.append({
                "id": embedding.id,
                "embedding_type": embedding.embedding_type,
                "embedding_model": embedding.embedding_model,
                "content": embedding.content,
                "chunk_index": embedding.chunk_index,
                "metadata": embedding.embedding_metadata,  # Fixed: use embedding_metadata not metadata
            })

    return {
        "id": record.id,
        "source_type": record.source_type,
        "source_id": record.source_id,
        "title": record.title,
        "source_uri": record.source_uri,
        "created_at": record.created_at,
        "added_at": record.added_at,
        "doc_metadata": record.doc_metadata,
        "embeddings": embeddings_data,
        "full_text": full_text,
        "full_text_type": full_text_type,
        "full_text_warning": full_text_warning,
    }


@documents_api_router.post(
    "/{document_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-index a document",
    description="Enqueues a background task to re-index a document.",
)
async def reindex_document(
    document_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
) -> dict[str, Any]:
    """
    API endpoint to re-index a document.
    """
    await db_context.tasks.enqueue(
        task_id=f"reindex_document_{document_id}_{uuid.uuid4()}",
        task_type="reindex_document",
        payload={"document_id": document_id},
    )
    return {"message": "Re-indexing task enqueued."}


@documents_api_router.post(
    "/upload",  # Path relative to the prefix in api.py
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload and index a document",
    description="Accepts document metadata and content parts via multipart/form-data, "
    "stores the document record, and enqueues a background task for embedding generation.",
)
async def upload_document(
    # Dependencies without Python default values
    request: Request,  # Inject Request to access app state
    # Required Form fields
    source_type: Annotated[
        str,
        Form(
            ...,
            description="Type of the source (e.g., 'manual_upload', 'scanned_receipt').",
        ),
    ],
    source_id: Annotated[
        str,
        Form(
            ...,
            description="Unique identifier for the document within its source type.",
        ),
    ],
    source_uri: Annotated[
        str, Form(..., description="Canonical URI/URL of the original document.")
    ],
    title: Annotated[
        str,
        Form(
            ...,
            description="Primary title for the document (can also be in content_parts).",
        ),
    ],
    # Other dependencies
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    # Optional Form fields
    content_parts_json: Annotated[
        str | None,
        Form(
            alias="content_parts",
            description='Optional JSON string representing a dictionary of content parts to be indexed. Keys determine embedding type (e.g., {"title": "Doc Title", "content_chunk_0": "First paragraph..."}). Required if no file is uploaded or URL provided.',
        ),
    ] = None,
    uploaded_file: Annotated[
        UploadFile | None,
        File(
            description="The document file to upload (e.g., PDF, TXT, DOCX). Required if no content_parts or URL provided.",
        ),
    ] = None,
    url: Annotated[
        str | None,
        Form(
            description="URL to scrape and index. Required if no file or content_parts provided."
        ),
    ] = None,
    created_at_str: Annotated[
        str | None,
        Form(
            alias="created_at",
            description="Original creation timestamp (ISO 8601 format string, e.g., 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD'). Timezone assumed UTC if missing.",
        ),
    ] = None,
    metadata_json: Annotated[
        str | None,
        Form(
            alias="metadata",
            description="JSON string representing a dictionary of additional metadata.",
        ),
    ] = None,
) -> DocumentUploadResponse:
    """
    API endpoint to upload document metadata and content parts for indexing.
    """
    logger.info(
        f"Received document upload request for source_id: {source_id} (type: {source_type}). "
        f"File provided: {uploaded_file is not None}. "
        f"Content parts provided: {content_parts_json is not None}. "
        f"URL provided: {url is not None}"
    )

    # --- 0. Get Document Storage Path from Config ---
    app_config = getattr(request.app.state, "config", {})
    document_storage_path_str = app_config.get("document_storage_path")
    if not document_storage_path_str:
        logger.error("Document storage path not configured. Upload will fail.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: Document storage path not set.",
        )
    document_storage_path = pathlib.Path(document_storage_path_str)

    # --- 1. Validate at least one input type is provided ---
    if not uploaded_file and not content_parts_json and not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either a file must be uploaded, content_parts_json must be provided, or a URL must be provided.",
        )

    # --- 2. Parse and Prepare Inputs for Service Function ---
    content_parts: dict[str, str] | None = None
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
    doc_metadata: dict[str, Any] = {}
    created_at_dt: datetime | None = None
    uploaded_file_content_bytes: bytes | None = None
    original_filename: str | None = None
    client_content_type: str | None = None

    try:
        if content_parts_json:
            content_parts = json.loads(content_parts_json)
            if not isinstance(content_parts, dict):
                raise ValueError("'content_parts' must be a valid JSON object string.")
            for key, value in content_parts.items():
                if not isinstance(value, str):
                    raise ValueError(
                        f"Value for content part '{key}' must be a string."
                    )
        elif not uploaded_file and not url:
            raise ValueError("'content_parts' must be provided if no file or URL.")

        if metadata_json:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                raise ValueError("'metadata' must be a valid JSON object string.")

        if created_at_str:
            try:
                created_at_dt = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    created_date = date.fromisoformat(created_at_str)
                    created_at_dt = datetime.combine(
                        created_date, datetime.min.time(), tzinfo=timezone.utc
                    )
                except ValueError:
                    raise ValueError(
                        "Invalid 'created_at' format. Use ISO 8601 datetime or date."
                    ) from None

        if uploaded_file:
            original_filename = uploaded_file.filename
            client_content_type = uploaded_file.content_type
            uploaded_file_content_bytes = await uploaded_file.read()

    except json.JSONDecodeError as json_err:
        logger.error(f"JSON parsing error for upload {source_id}: {json_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {json_err}"
        ) from json_err
    except ValueError as val_err:
        logger.error(f"Validation error for upload {source_id}: {val_err}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err)
        ) from val_err
    except Exception as e:  # Catch errors during file read
        logger.error(f"Error reading uploaded file for {source_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing uploaded file.",
        ) from e
    finally:
        if uploaded_file:
            await uploaded_file.close()

    # --- 3. Call the Ingestion Service Function ---
    ingestion_result = await process_document_ingestion_request(
        db_context=db_context,
        document_storage_path=document_storage_path,
        source_type=source_type,
        source_id=source_id,
        source_uri=source_uri,
        title=title,
        content_parts=content_parts,
        uploaded_file_content=uploaded_file_content_bytes,
        uploaded_file_filename=original_filename,
        uploaded_file_content_type=client_content_type,
        url_to_scrape=url,
        created_at_dt=created_at_dt,
        doc_metadata=doc_metadata,
    )

    # --- 4. Handle Result and Return Response ---
    if ingestion_result.get("error_detail"):
        status_code = ingestion_result.get(
            "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        # Ensure status_code is a valid HTTP status int
        if not isinstance(status_code, int) or not (100 <= status_code <= 599):
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

        raise HTTPException(
            status_code=status_code,
            detail=ingestion_result["message"],  # Use message from result as detail
        )

    return DocumentUploadResponse(
        message=ingestion_result["message"],
        document_id=ingestion_result["document_id"],
        task_enqueued=ingestion_result["task_enqueued"],
    )
