import contextlib
import json
import logging
import os
import pathlib
import re
import shutil
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

import filetype
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from family_assistant import storage
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    ToolExecutionContext,
    ToolNotFoundError,
    ToolsProvider,
)
from family_assistant.web.dependencies import (
    get_db,
    get_tools_provider_dependency,
)
from family_assistant.web.models import DocumentUploadResponse

logger = logging.getLogger(__name__)
api_router = APIRouter()


# --- Pydantic model for Tool Execution API ---
class ToolExecutionRequest(BaseModel):
    arguments: dict[str, Any]


@api_router.post("/tools/execute/{tool_name}", response_class=JSONResponse)
async def execute_tool_api(
    tool_name: str,
    request: Request,  # Keep request for potential context later
    payload: ToolExecutionRequest,
    tools_provider: Annotated[ToolsProvider, Depends(get_tools_provider_dependency)],
    db_context: Annotated[
        DatabaseContext, Depends(get_db)
    ],  # Inject DB context if tools need it
) -> JSONResponse:
    """Executes a specified tool with the given arguments."""
    logger.info(
        f"Received execution request for tool: {tool_name} with args: {payload.arguments}"
    )

    # --- Retrieve necessary config from app state ---
    app_config = getattr(
        request.app.state, "config", {}
    )  # Assuming config is stored in state
    if not app_config:
        logger.error("Main application configuration not found in app state.")
        # Fallback to empty dicts/defaults, but log error
        calendar_config = {}
        timezone_str = "UTC"
    else:
        calendar_config = app_config.get("calendar_config", {})
        timezone_str = app_config.get("timezone", "UTC")

    # --- Create Execution Context ---
    # We need some context, minimum placeholders for now
    # Generate a unique ID for this specific API call context
    # This isn't a persistent conversation like Telegram
    execution_context = ToolExecutionContext(
        interface_type="api",  # Identify interface
        conversation_id=f"api_call_{uuid.uuid4()}",
        db_context=db_context,
        calendar_config=calendar_config,  # Pass fetched calendar config
        timezone_str=timezone_str,  # Pass fetched timezone string
        application=None,  # No Telegram app here
        request_confirmation_callback=None,  # No confirmation from API for now
        processing_service=None,  # API endpoint doesn't have access to this
    )

    try:
        result = await tools_provider.execute_tool(
            name=tool_name, arguments=payload.arguments, context=execution_context
        )
        logger.info(f"Tool '{tool_name}' executed successfully.")

        # Attempt to parse result if it's a JSON string
        final_result = result
        if isinstance(result, str):
            with contextlib.suppress(json.JSONDecodeError):
                final_result = json.loads(result)
        return JSONResponse(
            content={"success": True, "result": final_result}, status_code=200
        )
    except ToolNotFoundError:
        logger.warning(f"Tool '{tool_name}' not found for execution request.")
        raise HTTPException(
            status_code=404, detail=f"Tool '{tool_name}' not found."
        ) from None
    except (
        ValidationError
    ) as ve:  # Catch Pydantic validation errors if execute_tool raises them
        logger.warning(f"Argument validation error for tool '{tool_name}': {ve}")
        raise HTTPException(
            status_code=400, detail=f"Invalid arguments for tool '{tool_name}': {ve}"
        ) from ve
    except (
        TypeError
    ) as te:  # Catch potential argument mismatches within the tool function
        logger.error(
            f"Type error during execution of tool '{tool_name}': {te}", exc_info=True
        )
        raise HTTPException(
            status_code=400,
            detail=f"Argument mismatch or type error in tool '{tool_name}': {te}",
        ) from te
    except Exception as e:
        logger.error(f"Error executing tool '{tool_name}': {e}", exc_info=True)
        # Avoid leaking internal error details unless intended
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while executing tool '{tool_name}'.",
        ) from e


@api_router.post(
    "/documents/upload",
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
    # Other dependencies
    db_context: Annotated[DatabaseContext, Depends(get_db)] = None,  # noqa: B008
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
        logger.error(
            "Document storage path not configured in application state. Upload will fail."
        )
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

    # --- 2. Parse and Validate Inputs ---
    content_parts: dict[str, str] | None = None
    doc_metadata: dict[str, Any] = {}
    created_at_dt: datetime | None = None
    file_ref: str | None = None
    detected_mime_type: str | None = None
    original_filename: str | None = None

    try:
        # Parse JSON strings if provided
        if content_parts_json:
            content_parts = json.loads(content_parts_json)
            if not isinstance(content_parts, dict):  # Allow empty dict if JSON is "{}
                raise ValueError(
                    "'content_parts' must be a valid JSON object string if provided."
                )
            # Validate content parts values are strings
            for key, value in content_parts.items():
                if not isinstance(value, str):
                    raise ValueError(
                        f"Value for content part '{key}' must be a string."
                    )
        elif not uploaded_file and not url:  # Safeguard if primary check is bypassed
            raise ValueError(
                "'content_parts' must be provided if no file or URL is uploaded."
            )

        if metadata_json:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                raise ValueError(
                    "'metadata' must be a valid JSON object string if provided."
                )

        # Parse date string (handle date vs datetime)
        if created_at_str:
            try:
                # Try parsing as full ISO 8601 datetime first
                created_at_dt = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
                # Ensure timezone-aware (assume UTC if naive)
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                # Try parsing as YYYY-MM-DD date
                try:
                    created_date = date.fromisoformat(created_at_str)
                    # Convert date to datetime (start of day, UTC)
                    created_at_dt = datetime.combine(
                        created_date, datetime.min.time(), tzinfo=timezone.utc
                    )
                except ValueError:
                    raise ValueError(
                        "Invalid 'created_at' format. Use ISO 8601 datetime (YYYY-MM-DDTHH:MM:SSZ) or date (YYYY-MM-DD)."
                    ) from None

        # Process uploaded file if present
        if uploaded_file:
            original_filename = uploaded_file.filename

            # Sanitize filename and create a unique name
            # Use os.path.basename to prevent directory traversal from malicious filenames
            safe_basename = re.sub(
                r"[^a-zA-Z0-9_.-]",
                "_",
                os.path.basename(original_filename or "unknown_file"),
            )
            unique_filename = f"{uuid.uuid4()}_{safe_basename}"

            target_file_path = document_storage_path / unique_filename

            # Ensure the storage directory exists
            document_storage_path.mkdir(parents=True, exist_ok=True)

            # Save the file to the persistent location
            with open(target_file_path, "wb") as f:
                shutil.copyfileobj(uploaded_file.file, f)

            file_ref = str(
                target_file_path
            )  # file_ref is the path to the persistently stored file
            logger.info(
                f"Uploaded file '{original_filename}' saved to '{file_ref}' for document {source_id}."
            )

            # Detect MIME type using filetype library from the new persistent path
            try:
                kind = filetype.guess(file_ref)  # Guess from the saved file
                if kind is None:
                    logger.warning(
                        f"Could not determine file type for '{original_filename}' (path: {file_ref}). "
                        f"Falling back to client-provided content type: {uploaded_file.content_type}."
                    )
                    detected_mime_type = uploaded_file.content_type  # Fallback
                else:
                    detected_mime_type = kind.mime
                    logger.info(
                        f"Detected MIME type for '{original_filename}' (path: {file_ref}): {detected_mime_type}"
                    )
            except Exception as fe:
                logger.error(
                    f"Error detecting file type for '{original_filename}' (path: {file_ref}): {fe}",
                    exc_info=True,
                )
                # Fallback or error, for now, let's use client-provided if detection fails
                detected_mime_type = uploaded_file.content_type
                logger.warning(
                    f"Using client-provided content type '{detected_mime_type}' due to detection error for {original_filename}."
                )

    except json.JSONDecodeError as json_err:
        logger.error(f"JSON parsing error for document upload {source_id}: {json_err}")
        # file_ref now points to a persistent location, so we don't remove it here.
        # If saving the file itself failed, file_ref would be None or point to a non-existent path.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON format: {json_err}",
        ) from json_err
    except ValueError as val_err:
        logger.error(f"Validation error for document upload {source_id}: {val_err}")
        # file_ref points to a persistent location.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err)
        ) from val_err
    except Exception as e:
        logger.error(
            f"Unexpected parsing or file handling error for document upload {source_id}: {e}",
            exc_info=True,
        )
        # file_ref points to a persistent location.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing request data or file.",
        ) from e
    finally:
        if uploaded_file:
            await uploaded_file.close()

    # --- 3. Create Document Record in DB ---
    # Create a dictionary conforming to the Document protocol structure
    # Use provided title if available, otherwise None
    document_data = {
        "_source_type": source_type,
        "_source_id": source_id,
        "_source_uri": source_uri,
        "_title": title,  # Use the dedicated title field if provided
        "_created_at": created_at_dt,
        "_base_metadata": doc_metadata,
        # These properties are part of the protocol definition
        "source_type": property(lambda self: self["_source_type"]),
        "source_id": property(lambda self: self["_source_id"]),
        "source_uri": property(lambda self: self["_source_uri"]),
        "title": property(lambda self: self["_title"]),
        "created_at": property(lambda self: self["_created_at"]),
        "metadata": property(lambda self: self["_base_metadata"]),
    }

    # Define a simple class on the fly that behaves like the Document protocol
    # This avoids needing a direct import of a specific Document implementation
    class UploadedDocument:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        @property
        def source_type(self) -> str:
            return self._data["_source_type"]

        @property
        def source_id(self) -> str:
            return self._data["_source_id"]

        @property
        def source_uri(self) -> str | None:
            return self._data["_source_uri"]

        @property
        def title(self) -> str | None:
            return self._data["_title"]

        @property
        def created_at(self) -> datetime | None:
            return self._data["_created_at"]

        @property
        def metadata(self) -> dict[str, Any] | None:
            return self._data["_base_metadata"]

    doc_for_storage = UploadedDocument(document_data)

    try:
        document_id: int = await storage.add_document(
            db_context=db_context,
            doc=doc_for_storage,
            # No separate enriched metadata here, it's already merged
        )
        logger.info(f"Stored document record for {source_id}, got DB ID: {document_id}")
    except Exception as db_err:
        logger.error(
            f"Database error storing document record for {source_id}: {db_err}",
            exc_info=True,
        )
        # Check for unique constraint violation (source_id already exists)
        # This check might be dialect-specific or require inspecting the exception details
        if "UNIQUE constraint failed" in str(
            db_err
        ) or "duplicate key value violates unique constraint" in str(db_err):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Document with source_type '{source_type}' and source_id '{source_id}' already exists.",
            ) from db_err
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error storing document.",
        ) from db_err

    # --- 4. Enqueue Background Task for Embedding ---
    task_payload = {
        "document_id": document_id,
        "content_parts": content_parts,  # Parsed dictionary or None
        "file_ref": file_ref,  # Path to persistent file or None
        "mime_type": detected_mime_type,  # Detected MIME type or None
        "original_filename": original_filename,  # Original filename or None
        "url_to_scrape": url,  # Pass the URL if provided
    }
    task_id = f"index-doc-{document_id}-{uuid.uuid4()}"  # More robust unique task ID
    task_enqueued = False
    try:
        await storage.enqueue_task(
            db_context=db_context,
            task_id=task_id,
            task_type="process_uploaded_document",  # Matches the handler registration
            payload=task_payload,
        )
        task_enqueued = True
        logger.info(f"Enqueued task '{task_id}' to process document ID {document_id}")
    except Exception as task_err:
        logger.error(
            f"Failed to enqueue indexing task for document ID {document_id}: {task_err}",
            exc_info=True,
        )
        # Document record exists, but indexing won't happen automatically.
        # Return success but indicate task failure? Or return an error?
        # Let's return success for the upload but log the task error clearly.
        # The response model will indicate task_enqueued=False.

    # --- 4. Return Response ---
    return DocumentUploadResponse(
        message="Document received and accepted for processing.",
        document_id=document_id,
        task_enqueued=task_enqueued,
    )
