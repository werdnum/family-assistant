"""API endpoints for error logs."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db, get_diagnostics_reader
from family_assistant.web.frontend_telemetry import (
    FrontendTelemetryRecord,
    get_frontend_telemetry_buffer,
)

errors_api_router = APIRouter()

# Logger for frontend errors. ERROR-level reports land here (and, via
# SQLAlchemyErrorHandler, in the error_logs table read by the engineer profile).
frontend_logger = logging.getLogger("frontend.javascript")

# Logger for non-error frontend telemetry (breadcrumbs). Kept below the
# error_logs handler threshold so telemetry never pollutes the error log; the
# in-memory ring buffer is the queryable lane (GET /api/errors/telemetry).
telemetry_logger = logging.getLogger("frontend.telemetry")

# Maps a report severity to the stdlib log level used for the telemetry lane.
# Everything here is below ERROR, so none of it is persisted to error_logs.
_TELEMETRY_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
}


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
    # Severity axis, orthogonal to error_type. Absent or "error" routes to the
    # error log (as today); "info"/"warning"/"debug" route to the telemetry ring
    # buffer instead so breadcrumbs never pollute error_logs. The web frontend
    # never sets this, so its reports (incl. component_error boundaries) stay in
    # the error log; only clients that opt in (iOS breadcrumbs) get the new lane.
    severity: str | None = None
    extra_data: dict | None = None


class FrontendErrorReportResponse(BaseModel):
    """Response model for frontend error report."""

    status: str


class FrontendTelemetryResponse(BaseModel):
    """Response for a page of buffered frontend telemetry (breadcrumbs)."""

    records: list[dict]
    count: int


@errors_api_router.post("/")
async def report_frontend_error(
    error_report: FrontendErrorReport,
) -> FrontendErrorReportResponse:
    """Report a frontend JavaScript error.

    This endpoint receives error reports from the web client and logs them
    using Python's logging system. The SQLAlchemyErrorHandler automatically
    stores ERROR-level logs in the database.

    A non-error ``severity`` ("info"/"warning"/"debug") routes the report to the
    in-memory telemetry ring buffer instead of the error log, so high-frequency
    breadcrumbs do not drown genuine errors. Absent or "error" severity behaves
    as before (logged at ERROR, persisted to error_logs).

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

    severity = (error_report.severity or "error").strip().lower()
    telemetry_level = _TELEMETRY_LEVELS.get(severity)
    if telemetry_level is None:
        # Error lane: unknown severities fall back here too, so a malformed value
        # is never silently dropped from the error log.
        frontend_logger.error(
            error_report.message,
            extra={"extra_data": extra_data},
        )
        return FrontendErrorReportResponse(status="reported")

    # Telemetry lane: ring buffer (the queryable surface) plus a stdout line
    # below the error_logs threshold.
    get_frontend_telemetry_buffer().add(
        FrontendTelemetryRecord(
            timestamp=datetime.now(UTC),
            severity=severity,
            message=error_report.message,
            component_name=error_report.component_name,
            error_type=error_report.error_type,
            url=error_report.url,
            user_agent=error_report.user_agent,
            extra_data=error_report.extra_data,
        )
    )
    telemetry_logger.log(
        telemetry_level,
        error_report.message,
        extra={"extra_data": extra_data},
    )
    return FrontendErrorReportResponse(status="recorded")


@errors_api_router.get("/")
async def get_errors(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    _: Annotated[dict, Depends(get_diagnostics_reader)],
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    level: str | None = None,
    logger: str | None = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> ErrorLogsListResponse:
    """Get paginated list of error logs."""
    cutoff_date = datetime.now(UTC) - timedelta(days=days)
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


@errors_api_router.get("/telemetry")
async def get_frontend_telemetry(
    _: Annotated[dict, Depends(get_diagnostics_reader)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    since_minutes: Annotated[int | None, Query(ge=1)] = None,
    component: str | None = None,
) -> FrontendTelemetryResponse:
    """Get recent non-error frontend telemetry (breadcrumbs) from the ring buffer.

    This is the queryable lane for iOS sync breadcrumbs, kept separate from the
    error log so it does not drown genuine errors. Registered before
    ``/{error_id}`` so this static path is matched first.
    """
    records = get_frontend_telemetry_buffer().get_recent(
        limit=limit,
        since_minutes=since_minutes,
        component=component,
    )
    return FrontendTelemetryResponse(
        records=[r.to_dict() for r in records],
        count=len(records),
    )


@errors_api_router.get("/{error_id}")
async def get_error_by_id(
    error_id: int,
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    _: Annotated[dict, Depends(get_diagnostics_reader)],
) -> ErrorLogResponse:
    """Get a specific error log by ID."""
    error = await db_context.error_logs.get_by_id(error_id)
    if not error:
        raise HTTPException(404, "Error log not found")

    return ErrorLogResponse(**error)
