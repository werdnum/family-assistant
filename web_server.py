import logging
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Dict, Optional

# Import storage functions - adjust path if needed
import storage

logger = logging.getLogger(__name__)

app = FastAPI(title="Family Assistant Notes Editor")

# Configure templates
templates = Jinja2Templates(directory="templates")

# Mount static files (optional, if you have CSS/JS)
# app.mount("/static", StaticFiles(directory="static"), name="static")

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
    return templates.TemplateResponse("index.html", {"request": request, "notes": notes})

@app.get("/notes/add", response_class=HTMLResponse)
async def add_note_form(request: Request):
    """Serves the form to add a new note."""
    return templates.TemplateResponse("edit_note.html", {"request": request, "note": None, "is_new": True})

@app.get("/notes/edit/{title}", response_class=HTMLResponse)
async def edit_note_form(request: Request, title: str):
    """Serves the form to edit an existing note."""
    note = await storage.get_note_by_title(title) # Need to add this function to storage.py
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return templates.TemplateResponse("edit_note.html", {"request": request, "note": note, "is_new": False})

@app.post("/notes/save")
async def save_note(request: Request, title: str = Form(...), content: str = Form(...), original_title: Optional[str] = Form(None)):
    """Handles saving a new or updated note."""
    try:
        if original_title and original_title != title:
            # Title changed - need to delete old and add new (or implement rename)
            # Simple approach: delete old, add new
            await storage.delete_note(original_title)
            await storage.add_or_update_note(title, content)
            logger.info(f"Renamed note '{original_title}' to '{title}' and updated content.")
        else:
            # New note or updating existing without title change
            await storage.add_or_update_note(title, content)
            logger.info(f"Saved note: {title}")
        return RedirectResponse(url="/", status_code=303) # Redirect back to list
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
    return RedirectResponse(url="/", status_code=303) # Redirect back to list

# --- Uvicorn Runner (for standalone testing) ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server for testing...")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
