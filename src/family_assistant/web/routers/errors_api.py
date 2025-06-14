"""API endpoints for error logs."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

errors_api_router = APIRouter()


class ErrorLogResponse(BaseModel):
    """Response model for error log entries."""

    id: int
    timestamp: datetime
    logger_name: str
    level: str
    message: str
    exception_type: str | None = None
    exception_message: str | None = None
    traceback: str | None = None
    module: str | None = None
    function_name: str | None = None
    extra_data: dict | None = None


class ErrorLogsListResponse(BaseModel):
    """Response for paginated error logs list."""

    errors: list[ErrorLogResponse]
    page: int
    total_pages: int
    total_count: int
    limit: int


@errors_api_router.get("/")
async def get_errors(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    level: str | None = None,
    logger: str | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> ErrorLogsListResponse:
    """Get paginated list of error logs."""
    cutoff_date = datetime.now() - timedelta(days=days)
    offset = (page - 1) * limit

    errors = await db_context.error_logs.get_all(
        level=level,
        logger_name=logger,
        since=cutoff_date,
        limit=limit,
        offset=offset,
    )

    total_count = await db_context.error_logs.count(
        level=level,
        logger_name=logger,
        since=cutoff_date,
    )

    total_pages = (total_count + limit - 1) // limit

    return ErrorLogsListResponse(
        errors=[ErrorLogResponse(**error) for error in errors],
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        limit=limit,
    )


@errors_api_router.get("/{error_id}")
async def get_error_by_id(
    error_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> ErrorLogResponse:
    """Get a specific error log by ID."""
    error = await db_context.error_logs.get_by_id(error_id)
    if not error:
        raise HTTPException(404, "Error log not found")

    return ErrorLogResponse(**error)
