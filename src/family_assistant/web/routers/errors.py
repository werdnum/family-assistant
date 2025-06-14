"""Web UI for viewing error logs."""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

router = APIRouter(prefix="/errors", tags=["errors"])


@router.get("/", response_class=HTMLResponse)
async def error_list(
    request: Request,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    level: str | None = None,
    logger: str | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> HTMLResponse:
    """List recent errors with filtering."""
    templates = request.app.state.templates
    cutoff_date = datetime.now() - timedelta(days=days)

    # Get errors with pagination
    limit = 50
    offset = (page - 1) * limit

    errors = await db_context.error_logs.get_all(
        level=level,
        logger_name=logger,
        since=cutoff_date,
        limit=limit,
        offset=offset,
    )

    # Get total count for pagination
    total_count = await db_context.error_logs.count(
        level=level,
        logger_name=logger,
        since=cutoff_date,
    )

    total_pages = (total_count + limit - 1) // limit

    return templates.TemplateResponse(
        "errors.html",
        {
            "request": request,
            "errors": errors,
            "page": page,
            "total_pages": total_pages,
            "total_count": total_count,
            "level": level,
            "logger": logger,
            "days": days,
            "now_utc": datetime.now(timezone.utc),
        },
    )


@router.get("/{error_id}", response_class=HTMLResponse)
async def error_detail(
    request: Request,
    error_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
) -> HTMLResponse:
    """Show detailed error with full traceback."""
    templates = request.app.state.templates

    error = await db_context.error_logs.get_by_id(error_id)
    if not error:
        raise HTTPException(404, "Error log not found")

    return templates.TemplateResponse(
        "error_detail.html",
        {"request": request, "error": error, "now_utc": datetime.now(timezone.utc)},
    )
