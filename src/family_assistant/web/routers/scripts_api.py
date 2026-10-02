"""Stored script editing through the authenticated web UI."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select

from family_assistant.processing import ProcessingService
from family_assistant.security.definition_records import (
    definition_content_hash,
    script_definition_content,
)
from family_assistant.security.taint import TurnTaintState
from family_assistant.storage.database import Database, DatabaseTransaction
from family_assistant.storage.repositories.scripts import ScriptModel, ScriptRow
from family_assistant.storage.scripts import scripts_table
from family_assistant.tools.stored_scripts import validate_stored_script
from family_assistant.web.dependencies import get_db, get_processing_service

scripts_api_router = APIRouter()


class SaveScriptRequest(ScriptModel):
    """A complete script definition, saved as the user's own edit."""

    name: str = Field(min_length=1)
    script_code: str = Field(min_length=1)
    expected_content_hash: str | None = None


@scripts_api_router.get("/")
async def list_scripts(db: Annotated[Database, Depends(get_db)]) -> list[ScriptRow]:
    """List stored script definitions for editing."""
    return await db.scripts.list_all()


@scripts_api_router.post("/")
async def save_script(
    request: SaveScriptRequest,
    db: Annotated[Database, Depends(get_db)],
    processing_service: Annotated[ProcessingService, Depends(get_processing_service)],
) -> ScriptRow:
    """Validate and save the complete definition as a direct human edit."""
    error = await validate_stored_script(
        request.script_code,
        request.parameters_schema,
        tools_provider=processing_service.tools_provider,
        keychute_config=processing_service.app_config.keychute_config,
        include_attachment_api=True,
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    async def body(txn: DatabaseTransaction) -> ScriptRow:
        row = await txn.fetch_one(
            select(scripts_table)
            .where(scripts_table.c.name == request.name)
            .with_for_update()
        )
        existing = (
            await txn.scripts.get_by_name(request.name) if row is not None else None
        )
        if existing is not None:
            if request.expected_content_hash is None:
                raise HTTPException(
                    status_code=409, detail="A script with this name already exists."
                )
            content = script_definition_content(
                name=existing.name,
                description=existing.description,
                script_code=existing.script_code,
                parameters_schema=existing.parameters_schema,
            )
            if definition_content_hash(content) != request.expected_content_hash:
                raise HTTPException(
                    status_code=409, detail="Script changed. Reload before saving."
                )
        elif request.expected_content_hash is not None:
            raise HTTPException(status_code=404, detail="Script not found")
        return await txn.scripts.save(
            request.name,
            request.description,
            request.script_code,
            request.parameters_schema,
            definition_taint_state=TurnTaintState.empty(),
            definition_human_direct=True,
        )

    return await db.atomic(body)
