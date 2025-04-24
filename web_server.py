import logging
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Dict, Optional
import email_storage # Import the new module
from fastapi import Response  # Added Response
import json  # Import json for payload rendering

from collections import defaultdict  # Import defaultdict

# Import storage functions - adjust path if needed
import storage
import email_storage
from storage import get_grouped_message_history, get_all_tasks  # Added get_all_tasks

logger = logging.getLogger(__name__)

app = FastAPI(title="Family Assistant Notes Editor")

# Configure templates
templates = Jinja2Templates(directory="templates")

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Helper for DB Session (if needed, but storage functions are standalone) ---
# Example if storage functions required a session object
# async def get_db() -> AsyncSession:
#     async with storage.SessionLocal() as session:
#         yield session

# --- Routes ---


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serves the main page listing all notes."""
    notes = await storage.get_all_notes()
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
    note = await storage.get_note_by_title(
        title
    )  # Need to add this function to storage.py
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
            await storage.delete_note(original_title)
            await storage.add_or_update_note(title, content)
            logger.info(
                f"Renamed note '{original_title}' to '{title}' and updated content."
            )
        else:
            # New note or updating existing without title change
            await storage.add_or_update_note(title, content)
            logger.info(f"Saved note: {title}")
        return RedirectResponse(url="/", status_code=303)  # Redirect back to list
    except Exception as e:
        logger.error(f"Error saving note '{title}': {e}", exc_info=True)
        # You might want to return an error page instead
        raise HTTPException(status_code=500, detail=f"Failed to save note: {e}")


@app.post("/notes/delete/{title}")
async def delete_note_post(request: Request, title: str):
    """Handles deleting a note."""
    deleted = await storage.delete_note(title)
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
        # Mailgun sends data as multipart/form-data
        form_data = await request.form()
        await email_storage.store_incoming_email(dict(form_data)) # Convert MultiDict to dict
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
