import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.repositories.notes import (
    DuplicateNoteError,
    NoteNotFoundError,
)
from family_assistant.web.dependencies import get_db

logger = logging.getLogger(__name__)
notes_api_router = APIRouter()


class NoteModel(BaseModel):
    """Schema for a note."""

    title: str
    content: str
    include_in_prompt: bool = True
    original_title: str | None = None  # For edit operations when title changes


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
    # If original_title is provided, this is an edit operation with potential rename
    if note.original_title and note.original_title != note.title:
        # Use the repository's rename method which preserves primary key
        try:
            await db_context.notes.rename_and_update(
                note.original_title, note.title, note.content, note.include_in_prompt
            )
        except NoteNotFoundError as err:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
        except DuplicateNoteError as err:
            raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
        except IntegrityError as err:
            # Handle race condition where title was taken between check and update
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A note with title '{note.title}' already exists",
            ) from err
    else:
        # Regular create or update (no rename)
        try:
            await db_context.notes.add_or_update(
                note.title, note.content, note.include_in_prompt
            )
        except IntegrityError as err:
            # Handle race condition where title was taken between check and create
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A note with title '{note.title}' already exists",
            ) from err

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
