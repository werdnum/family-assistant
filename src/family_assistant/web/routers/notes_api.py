import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
notes_api_router = APIRouter()


class NoteModel(BaseModel):
    """Schema for a note."""

    title: str
    content: str
    include_in_prompt: bool = True


@notes_api_router.get("/")
async def list_notes(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> list[NoteModel]:
    """Return all notes."""
    notes = await db_context.notes.get_all()
    return [NoteModel(**note) for note in notes]


@notes_api_router.get("/{title}")
async def get_note(
    title: str, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> NoteModel:
    """Return a note by title."""
    note = await db_context.notes.get_by_title(title)
    if not note:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return NoteModel(**note)


@notes_api_router.post("/", status_code=status.HTTP_201_CREATED)
async def create_or_update_note(
    note: NoteModel, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> dict[str, str]:
    """Create or update a note."""
    await db_context.notes.add_or_update(
        note.title, note.content, note.include_in_prompt
    )
    logger.info("Saved note %s", note.title)
    return {"message": "Note saved"}


@notes_api_router.delete("/{title}")
async def delete_note(
    title: str, db_context: Annotated[DatabaseContext, Depends(get_db)]
) -> dict[str, str]:
    """Delete a note by title."""
    deleted = await db_context.notes.delete(title)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    logger.info("Deleted note %s", title)
    return {"message": "Note deleted"}
