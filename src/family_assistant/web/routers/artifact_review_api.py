"""Authenticated UI review of stored artifact provenance."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from family_assistant.memory.invariants import MemoryWriteError
from family_assistant.storage.database import Database
from family_assistant.storage.repositories.artifact_review import (
    ArtifactChangedError,
    ArtifactKind,
    ArtifactReview,
)
from family_assistant.web.dependencies import get_current_user, get_db

artifact_review_api_router = APIRouter()


class ConfirmArtifactRequest(BaseModel):
    """The hash of the complete content shown to the approving user."""

    content_hash: str


@artifact_review_api_router.get("/")
async def list_artifacts(
    db: Annotated[Database, Depends(get_db)],
) -> list[ArtifactReview]:
    """List notes, stored scripts, and event and schedule definitions for review."""
    return await db.artifact_review.list_all()


@artifact_review_api_router.post("/{kind}/{artifact_id}/confirm")
async def confirm_artifact(
    kind: ArtifactKind,
    artifact_id: int,
    request: ConfirmArtifactRequest,
    db: Annotated[Database, Depends(get_db)],
    current_user: Annotated[dict, Depends(get_current_user)],
) -> ArtifactReview:
    """Record a user's approval of exactly the content they reviewed."""
    try:
        artifact = await db.artifact_review.confirm(
            kind,
            artifact_id,
            content_hash=request.content_hash,
            user_id=str(current_user["user_identifier"]),
        )
    except ArtifactChangedError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except MemoryWriteError as err:
        raise HTTPException(status_code=422, detail=err.message) from err
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact
