import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from family_assistant.web.auth import AUTH_ENABLED, User, get_current_user_optional

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/upload", response_class=HTMLResponse)
async def get_document_upload_form(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)] = None,
):
    """Serves the HTML form for uploading documents."""
    if AUTH_ENABLED and not current_user:
        # Redirect to login or show an error if auth is on and no user
        # For simplicity, let's assume templates handle "not logged in" state
        # or a middleware redirects. Here, we just pass it to the template.
        pass

    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        "document_upload.html",
        {
            "request": request,
            "current_user": current_user,
            "AUTH_ENABLED": AUTH_ENABLED,
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
):
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

            if response.status_code == 202: # HTTP_202_ACCEPTED
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

    return templates.TemplateResponse(
        "document_upload.html",
        {
            "request": request,
            "message": message,
            "error": error,
            "form_data": form_data,  # To repopulate form on error
            "current_user": current_user,
            "AUTH_ENABLED": AUTH_ENABLED,
        },
    )
