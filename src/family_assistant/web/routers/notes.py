import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from family_assistant.storage import (
    add_or_update_note,
    delete_note,
    get_all_notes,
    get_note_by_title,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.auth import AUTH_ENABLED
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
notes_router = APIRouter()


@notes_router.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> HTMLResponse:
    """Serves the main page listing all notes."""
    templates = request.app.state.templates
    server_url = request.app.state.server_url

    notes = await get_all_notes(db_context)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "notes": notes,
            "user": request.session.get("user"),
            "auth_enabled": AUTH_ENABLED,
            "server_url": server_url,
        },
    )


@notes_router.get("/notes/add", response_class=HTMLResponse)
async def add_note_form(request: Request) -> HTMLResponse:
    """Serves the form to add a new note."""
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "edit_note.html",
        {
            "request": request,
            "note": None,
            "is_new": True,
            "user": request.session.get("user"),
            "auth_enabled": AUTH_ENABLED,
        },
    )


@notes_router.get("/notes/edit/{title}", response_class=HTMLResponse)
async def edit_note_form(
    request: Request,
    title: str,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> HTMLResponse:
    """Serves the form to edit an existing note."""
    templates = request.app.state.templates
    note = await get_note_by_title(db_context, title)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return templates.TemplateResponse(
        "edit_note.html",
        {
            "request": request,
            "note": note,
            "is_new": False,
            "user": request.session.get("user"),
            "auth_enabled": AUTH_ENABLED,
        },
    )


@notes_router.post("/notes/save")
async def save_note(
    request: Request,
    title: Annotated[str, Form()],
    content: Annotated[str, Form()],
    original_title: Annotated[str | None, Form()] = None,
    db_context: Annotated[DatabaseContext, Depends(get_db)] = None,
) -> RedirectResponse:
    """Handles saving a new or updated note."""
    try:
        if original_title and original_title != title:
            await delete_note(db_context, original_title)
            await add_or_update_note(db_context, title, content)
            logger.info(
                f"Renamed note '{original_title}' to '{title}' and updated content."
            )
        else:
            await add_or_update_note(db_context, title, content)
            logger.info(f"Saved note: {title}")
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"Error saving note '{title}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save note: {e}") from e


@notes_router.post("/notes/delete/{title}")
async def delete_note_post(
    request: Request,
    title: str,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> RedirectResponse:
    """Handles deleting a note."""
    deleted = await delete_note(db_context, title)
    if not deleted:
        raise HTTPException(status_code=404, detail="Note not found for deletion")
    return RedirectResponse(url="/", status_code=303)
