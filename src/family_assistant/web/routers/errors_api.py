"""API endpoints for error logs."""

import logging
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

errors_api_router = APIRouter()

# Logger for frontend JavaScript errors
frontend_logger = logging.getLogger("frontend.javascript")


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


class FrontendErrorReport(BaseModel):
    """Request model for frontend error reports."""

    message: str
    stack: str | None = None
    url: str
    user_agent: str | None = None
    component_name: str | None = None
    error_type: str | None = (
        None  # uncaught, promise_rejection, component_error, manual
    )
    extra_data: dict | None = None


class FrontendErrorReportResponse(BaseModel):
    """Response model for frontend error report."""

    status: str


@errors_api_router.post("/")
async def report_frontend_error(
    error_report: FrontendErrorReport,
) -> FrontendErrorReportResponse:
    """Report a frontend JavaScript error.

    This endpoint receives error reports from the web client and logs them
    using Python's logging system. The SQLAlchemyErrorHandler automatically
    stores ERROR-level logs in the database.

    Note: This endpoint is intentionally unauthenticated to allow error
    capture before user login or when auth state is broken. The /api/* paths
    are in PUBLIC_PATHS (auth.py). Rate limiting via batching and deduplication
    is implemented in the frontend errorClient.ts.
    """
    extra_data = {
        "url": error_report.url,
        "user_agent": error_report.user_agent,
        "component_name": error_report.component_name,
        "error_type": error_report.error_type,
        "stack": error_report.stack,
        "details": error_report.extra_data,
    }

    frontend_logger.error(
        error_report.message,
        extra={"extra_data": extra_data},
    )

    return FrontendErrorReportResponse(status="reported")


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
