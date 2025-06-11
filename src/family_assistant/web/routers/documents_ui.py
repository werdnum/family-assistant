import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.vector import DocumentRecord
from family_assistant.web.auth import AUTH_ENABLED, User, get_current_user_optional
from family_assistant.web.dependencies import get_db

if TYPE_CHECKING:
    from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_class=HTMLResponse, name="ui_list_documents")
async def list_documents(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    limit: int = 20,
    offset: int = 0,
    current_user: Annotated[User | None, Depends(get_current_user_optional)] = None,
) -> HTMLResponse:
    """Lists all documents with pagination."""
    templates: Jinja2Templates = request.app.state.templates

    try:
        # Count total documents
        count_query = select(func.count().label("count")).select_from(DocumentRecord)  # pylint: disable=not-callable
        total_count_result = await db_context.fetch_one(count_query)
        total_count = total_count_result["count"] if total_count_result else 0

        # Get documents with pagination
        documents_query = (
            select(DocumentRecord)
            .order_by(DocumentRecord.added_at.desc())
            .limit(limit)
            .offset(offset)
        )

        # Get documents as dict-like objects
        documents_rows = await db_context.fetch_all(documents_query)

        # Convert to list of dicts for template
        documents = []
        for row in documents_rows:
            doc_dict = dict(row)
            documents.append(doc_dict)

        # Calculate pagination info
        has_next = offset + limit < total_count
        has_prev = offset > 0
        next_offset = offset + limit if has_next else None
        prev_offset = max(0, offset - limit) if has_prev else None

        # Calculate page numbers
        current_page = (offset // limit) + 1
        total_pages = (total_count + limit - 1) // limit  # Ceiling division

    except Exception as e:
        logger.error(f"Error fetching documents: {e}", exc_info=True)
        documents = []
        total_count = 0
        has_next = False
        has_prev = False
        next_offset = None
        prev_offset = None
        current_page = 1
        total_pages = 1
        error = f"Error loading documents: {str(e)}"
    else:
        error = None

    return templates.TemplateResponse(
        "documents_list.html.j2",
        {
            "request": request,
            "documents": documents,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "has_next": has_next,
            "has_prev": has_prev,
            "next_offset": next_offset,
            "prev_offset": prev_offset,
            "current_page": current_page,
            "total_pages": total_pages,
            "error": error,
            "current_user": current_user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.get("/upload", response_class=HTMLResponse)
async def get_document_upload_form(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)] = None,
) -> HTMLResponse:
    """Serves the HTML form for uploading documents."""
    if AUTH_ENABLED and not current_user:
        # Redirect to login or show an error if auth is on and no user
        # For simplicity, let's assume templates handle "not logged in" state
        # or a middleware redirects. Here, we just pass it to the template.
        pass

    templates: Jinja2Templates = request.app.state.templates
    now_utc = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        "document_upload.html.j2",
        {
            "request": request,
            "current_user": current_user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "now_utc": now_utc,
        },
    )


@router.post("/upload", response_class=HTMLResponse)
async def handle_document_upload(  # noqa: PLR0913
    request: Request,
    source_type: Annotated[str, Form(...)],
    source_id: Annotated[str, Form(...)],
    source_uri: Annotated[str, Form(...)],
    title: Annotated[str, Form(...)],
    created_at: Annotated[str | None, Form()] = None,
    metadata: Annotated[str | None, Form()] = None,
    content_parts: Annotated[str | None, Form()] = None,
    uploaded_file: Annotated[UploadFile | None, File()] = None,
    current_user: Annotated[User | None, Depends(get_current_user_optional)] = None,
) -> HTMLResponse:
    """Handles document submission from the UI form and calls the internal API."""
    if AUTH_ENABLED and not current_user:
        # As above, handle auth if necessary
        pass

    templates: Jinja2Templates = request.app.state.templates
    api_url = f"{request.app.state.server_url}/api/documents/upload"

    form_data = {
        "source_type": source_type,
        "source_id": source_id,
        "source_uri": source_uri,
        "title": title,
    }
    if created_at:
        form_data["created_at"] = created_at
    if metadata:
        form_data["metadata"] = metadata
    if content_parts:
        form_data["content_parts"] = content_parts

    files_payload = {}
    if uploaded_file and uploaded_file.filename:
        files_payload["uploaded_file"] = (
            uploaded_file.filename,
            uploaded_file.file,
            uploaded_file.content_type,
        )

    message = None
    error = None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Need to pass auth if the API endpoint requires it.
            # Assuming the /api/documents/upload is publicly accessible or handles auth internally.
            # If it needs a token from the current_user, that needs to be added to headers.
            response = await client.post(api_url, data=form_data, files=files_payload)

            if response.status_code == 202:  # HTTP_202_ACCEPTED
                response_data = response.json()
                message = response_data.get(
                    "message", "Document submitted successfully."
                )
                if not response_data.get("task_enqueued", True):
                    message += " (Warning: Background processing task was not enqueued)"
            else:
                try:
                    error_detail = response.json().get("detail", response.text)
                except Exception:
                    error_detail = response.text
                error = f"API Error ({response.status_code}): {error_detail}"
                logger.error(
                    f"Error calling internal document upload API: {response.status_code} - {error_detail}"
                )

    except httpx.RequestError as e:
        logger.error(f"HTTPX RequestError during internal API call: {e}", exc_info=True)
        error = f"Could not connect to the document processing service: {e}"
    except Exception as e:
        logger.error(
            f"Unexpected error during document upload form submission: {e}",
            exc_info=True,
        )
        error = f"An unexpected error occurred: {e}"
    finally:
        if uploaded_file:
            await uploaded_file.close()

    now_utc = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        "document_upload.html.j2",
        {
            "request": request,
            "message": message,
            "error": error,
            "form_data": form_data,  # To repopulate form on error
            "current_user": current_user,
            "AUTH_ENABLED": AUTH_ENABLED,
            "now_utc": now_utc,
        },
    )
