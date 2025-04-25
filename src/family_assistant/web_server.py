import logging
import os
import re
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Optional
from fastapi import Response  # Added Response
from datetime import datetime, timezone
import json
import pathlib # Import pathlib for finding template/static dirs
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import storage functions using absolute package path
from family_assistant import storage
from family_assistant.storage import (
    get_all_notes,
    get_note_by_title,
    add_or_update_note,
    delete_note,
    store_incoming_email,
    get_grouped_message_history,
    get_all_tasks,
)

logger = logging.getLogger(__name__)

# Directory to save raw webhook request bodies for debugging/replay
MAILBOX_RAW_DIR = "/mnt/data/mailbox/raw_requests" # TODO: Consider making this configurable via env var

app = FastAPI(title="Family Assistant Web Interface") # Updated title slightly

# --- Determine base path for templates and static files ---
# This assumes web_server.py is at src/family_assistant/web_server.py
# We want the paths relative to the 'family_assistant' package directory
try:
    # Get the directory containing the current file (web_server.py)
    current_file_dir = pathlib.Path(__file__).parent.resolve()
    # Go up one level to the package root (src/family_assistant/)
    package_root_dir = current_file_dir
    # Define template and static directories relative to the package root
    templates_dir = package_root_dir / "templates"
    static_dir = package_root_dir / "static"

    if not templates_dir.is_dir():
        logger.warning(f"Templates directory not found at expected location: {templates_dir}")
        # Fallback or raise error? For now, log warning.
    if not static_dir.is_dir():
        logger.warning(f"Static directory not found at expected location: {static_dir}")
        # Fallback or raise error?

    # Configure templates using the calculated path
    templates = Jinja2Templates(directory=templates_dir)

    # Mount static files using the calculated path
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"Templates directory set to: {templates_dir}")
    logger.info(f"Static files directory set to: {static_dir}")

except NameError:
    # __file__ might not be defined in some execution contexts (e.g., interactive)
    logger.error("Could not determine package path using __file__. Static/template files might not load.")
    # Provide fallback paths relative to CWD, although this might not work reliably
    templates = Jinja2Templates(directory="src/family_assistant/templates")
    app.mount("/static", StaticFiles(directory="src/family_assistant/static"), name="static")

# --- Helper for DB Session (if needed, but storage functions are standalone) ---
# Example if storage functions required a session object
# async def get_db() -> AsyncSession:
#     async with storage.SessionLocal() as session:
#         yield session

# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serves the main page listing all notes."""
    notes = await get_all_notes()
    return templates.TemplateResponse(
        "index.html", {"request": request, "notes": notes}
    )


@app.get("/notes/add", response_class=HTMLResponse)
async def add_note_form(request: Request):
    """Serves the form to add a new note."""
    return templates.TemplateResponse(
        "edit_note.html", {"request": request, "note": None, "is_new": True}
    )


@app.get("/notes/edit/{title}", response_class=HTMLResponse)
async def edit_note_form(request: Request, title: str):
    """Serves the form to edit an existing note."""
    note = await get_note_by_title(title)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return templates.TemplateResponse(
        "edit_note.html", {"request": request, "note": note, "is_new": False}
    )


@app.post("/notes/save")
async def save_note(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    original_title: Optional[str] = Form(None),
):
    """Handles saving a new or updated note."""
    try:
        if original_title and original_title != title:
            # Title changed - need to delete old and add new (or implement rename)
            # Simple approach: delete old, add new
            await delete_note(original_title)
            await add_or_update_note(title, content)
            logger.info(
                f"Renamed note '{original_title}' to '{title}' and updated content."
            )
        else:
            # New note or updating existing without title change
            await add_or_update_note(title, content)
            logger.info(f"Saved note: {title}")
        return RedirectResponse(url="/", status_code=303)  # Redirect back to list
    except Exception as e:
        logger.error(f"Error saving note '{title}': {e}", exc_info=True)
        # You might want to return an error page instead
        raise HTTPException(status_code=500, detail=f"Failed to save note: {e}")


@app.post("/notes/delete/{title}")
async def delete_note_post(request: Request, title: str):
    """Handles deleting a note."""
    deleted = await delete_note(title)
    if not deleted:
        raise HTTPException(status_code=404, detail="Note not found for deletion")
    return RedirectResponse(url="/", status_code=303)  # Redirect back to list


@app.post("/webhook/mail")
async def handle_mail_webhook(request: Request):
    """
    Receives incoming email via webhook (expects multipart/form-data).
    Logs the received form data for now.
    """
    logger.info("Received POST request on /webhook/mail")
    try:
        # --- Save raw request body for debugging/replay ---
        raw_body = await request.body()
        try:
            os.makedirs(MAILBOX_RAW_DIR, exist_ok=True)
            # Use timestamp for filename, as parsing form data might consume the body stream
            # depending on the framework version/internals. Reading body first is safer.
            now = datetime.now(timezone.utc)
            timestamp_str = now.strftime("%Y%m%d_%H%M%S_%f")
            # Sanitize content-type for filename part if available
            content_type = request.headers.get("content-type", "unknown_content_type")
            safe_content_type = (
                re.sub(r'[<>:"/\\|?*]', "_", content_type).split(";")[0].strip()
            )  # Get main type
            filename = f"{timestamp_str}_{safe_content_type}.raw"
            filepath = os.path.join(MAILBOX_RAW_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(raw_body)
            logger.info(
                f"Saved raw webhook request body ({len(raw_body)} bytes) to: {filepath}"
            )
        except Exception as e:
            # Log error but don't fail the request processing
            logger.error(f"Failed to save raw webhook request body: {e}", exc_info=True)
        # --- End raw request saving ---

        # Mailgun sends data as multipart/form-data
        form_data = await request.form()
        await store_incoming_email(
            dict(form_data)
        )  # Pass the parsed form data to storage function
        # TODO: Add logic here to parse/store email content or trigger LLM processing
        # -----------------------------------------

        return Response(status_code=200, content="Email received.")
    except Exception as e:
        logger.error(f"Error processing mail webhook: {e}", exc_info=True)
        # Return 500, Mailgun might retry
        raise HTTPException(status_code=500, detail="Failed to process incoming email")


@app.get("/history", response_class=HTMLResponse)
async def view_message_history(request: Request):
    """Serves the page displaying message history."""
    try:
        history_by_chat = await get_grouped_message_history()
        # Optional: Sort chats by ID if needed (DB query already sorts)
        # history_by_chat = dict(sorted(history_by_chat.items()))
        return templates.TemplateResponse(
            "message_history.html",
            {"request": request, "history_by_chat": history_by_chat},
        )
    except Exception as e:
        logger.error(f"Error fetching message history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch message history")


@app.get("/tasks", response_class=HTMLResponse)
async def view_tasks(request: Request):
    """Serves the page displaying scheduled tasks."""
    try:
        tasks = await get_all_tasks(limit=200)  # Fetch tasks, add limit if needed
        return templates.TemplateResponse(
            "tasks.html",
            {
                "request": request,
                "tasks": tasks,
                # Add json filter to Jinja environment if not default
                # Pass 'tojson' filter if needed explicitly, or handle in template
                # jinja_env.filters['tojson'] = json.dumps # Example
            },
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tasks")


@app.get("/health", status_code=200)
async def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}


# --- Uvicorn Runner (for standalone testing) ---
if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Uvicorn server for testing...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
