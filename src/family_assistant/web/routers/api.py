import contextlib
import json
import logging
import pathlib

# import re # No longer used directly here
# import shutil # No longer used directly here
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

# import filetype # No longer used directly here
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

from family_assistant.indexing.ingestion import process_document_ingestion_request
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
                    raise ValueError(f"Value for content part '{key}' must be a string.")
        elif not uploaded_file and not url:
            raise ValueError("'content_parts' must be provided if no file or URL.")

        if metadata_json:
            doc_metadata = json.loads(metadata_json)
            if not isinstance(doc_metadata, dict):
                raise ValueError("'metadata' must be a valid JSON object string.")

        if created_at_str:
            try:
                created_at_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                if created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    created_date = date.fromisoformat(created_at_str)
                    created_at_dt = datetime.combine(created_date, datetime.min.time(), tzinfo=timezone.utc)
                except ValueError:
                    raise ValueError("Invalid 'created_at' format. Use ISO 8601 datetime or date.") from None
        
        if uploaded_file:
            original_filename = uploaded_file.filename
            client_content_type = uploaded_file.content_type
            uploaded_file_content_bytes = await uploaded_file.read()

    except json.JSONDecodeError as json_err:
        logger.error(f"JSON parsing error for upload {source_id}: {json_err}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {json_err}") from json_err
    except ValueError as val_err:
        logger.error(f"Validation error for upload {source_id}: {val_err}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(val_err)) from val_err
    except Exception as e: # Catch errors during file read
        logger.error(f"Error reading uploaded file for {source_id}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing uploaded file.") from e
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
        status_code = ingestion_result.get("status_code", status.HTTP_500_INTERNAL_SERVER_ERROR)
        # Ensure status_code is a valid HTTP status int
        if not isinstance(status_code, int) or not (100 <= status_code <= 599):
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            
        raise HTTPException(
            status_code=status_code,
            detail=ingestion_result["message"], # Use message from result as detail
        )

    return DocumentUploadResponse(
        message=ingestion_result["message"],
        document_id=ingestion_result["document_id"],
        task_enqueued=ingestion_result["task_enqueued"],
    )
