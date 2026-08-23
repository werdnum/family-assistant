"""API endpoints for error logs."""

import json
import logging
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage.database import Database
from family_assistant.web.auth import api_authentication_enabled, extract_api_credential
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
    """Request model for frontend error reports.

    Field bounds bound the blast radius of the deliberately unauthenticated
    intake: a report is rejected with 422 before any logging/persistence when
    a field is oversized.
    """

    message: str = Field(max_length=8_000)
    stack: str | None = Field(default=None, max_length=32_000)
    url: str = Field(max_length=2_000)
    user_agent: str | None = Field(default=None, max_length=512)
    component_name: str | None = Field(default=None, max_length=256)
    error_type: str | None = (
        None  # uncaught, promise_rejection, component_error, manual
    )
    # Severity axis, orthogonal to error_type. Absent or "error" routes to the
    # error log (as today); "info"/"warning"/"debug" route to the telemetry ring
    # buffer instead so breadcrumbs never pollute error_logs. The web frontend
    # never sets this, so its reports (incl. component_error boundaries) stay in
    # the error log; only clients that opt in (iOS breadcrumbs) get the new lane.
    severity: str | None = Field(default=None, max_length=16)
    extra_data: dict | None = None

    @field_validator("extra_data")
    @classmethod
    def _bound_extra_data(cls, value: dict | None) -> dict | None:
        if value is not None and len(json.dumps(value, default=str)) > 16_000:
            raise ValueError("extra_data serialized size exceeds 16000 characters")
        return value


class FrontendErrorReportResponse(BaseModel):
    """Response model for frontend error report."""

    status: str


class FrontendTelemetryResponse(BaseModel):
    """Response for a page of buffered frontend telemetry (breadcrumbs)."""

    records: list[dict]
    count: int


# --- Server-side abuse controls for the unauthenticated intake ---
#
# The intake must stay reachable without a user session (error capture before
# login or with broken auth), so its protection is bounded impact instead of
# access control: a per-client rate limit, hard payload bounds (see
# FrontendErrorReport), and unauthenticated reports never persist — they are
# clamped into the in-memory telemetry ring, which is fixed-size and dropped
# on restart. Authenticated reporters keep the full severity behaviour.
ERROR_INTAKE_RATE_LIMIT = 60  # reports per window per authenticated user or address
# A higher pre-authentication ceiling bounds invalid-token verification and body
# buffering without collapsing all authenticated users behind one proxy into the
# same reporting bucket. The normal per-user/address limit still applies below.
ERROR_INTAKE_ADDRESS_ADMISSION_RATE_LIMIT = 600
ERROR_INTAKE_WINDOW_SECONDS = 60.0
# Cap on tracked addresses: an attacker rotating source addresses (e.g. across
# an IPv6 allocation) must not be able to grow the map without bound. When the
# cap is hit, expired entries are swept; if that frees nothing, new client keys
# are refused until an entry expires, degrading intake availability under
# attack rather than allowing memory exhaustion.
RATE_LIMIT_MAX_ADDRESSES = 4_096


class ErrorIntakeRateLimiter:
    """Application-scoped sliding-window limiter for public error intake."""

    def __init__(self, max_hits: int = ERROR_INTAKE_RATE_LIMIT) -> None:
        self._max_hits = max_hits
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def allow(self, client_key: str) -> bool:
        """Return whether this client may submit another report."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(client_key)
            if hits is not None:
                while hits and now - hits[0] > ERROR_INTAKE_WINDOW_SECONDS:
                    hits.popleft()
                if not hits:
                    del self._hits[client_key]
            if len(self._hits) >= RATE_LIMIT_MAX_ADDRESSES:
                self._sweep(now)
            hits = self._hits.get(client_key)
            if hits is None:
                if len(self._hits) >= RATE_LIMIT_MAX_ADDRESSES:
                    return False
                hits = deque()
                self._hits[client_key] = hits
            if len(hits) >= self._max_hits:
                return False
            hits.append(now)
            return True

    def tracked_clients(self) -> int:
        """Return the number of currently tracked client keys."""
        with self._lock:
            return len(self._hits)

    def expire_clients(self, count: int) -> None:
        """Expire client keys for deterministic bounded-map tests."""
        with self._lock:
            for client_key in list(self._hits)[:count]:
                self._hits[client_key].clear()

    def _sweep(self, now: float) -> None:
        for client_key in list(self._hits):
            hits = self._hits[client_key]
            while hits and now - hits[0] > ERROR_INTAKE_WINDOW_SECONDS:
                hits.popleft()
            if not hits:
                del self._hits[client_key]


def _session_bound_token_id(request: Request) -> int | None:
    """Return the API-token id bound to the session user, when present.

    None means there is no session credential to revalidate (no session
    middleware, or a plain OIDC session without a token binding).
    """
    try:
        if not request.session.get("user"):
            return None
    except AssertionError:
        # Session middleware not installed; no session credential exists.
        return None
    return request.session.get("api_token_id")


async def _token_bound_session_is_valid(engine: AsyncEngine, token_id: int) -> bool:
    from family_assistant.storage import (  # noqa: PLC0415 - deferred to avoid circular import at module level
        api_tokens as api_tokens_storage,
    )
    from family_assistant.storage.database import (  # noqa: PLC0415 - deferred to avoid circular import at module level
        Database,
    )

    return await api_tokens_storage.is_token_valid(Database(engine), token_id)


async def _reporter_authentication(request: Request) -> tuple[bool, str | None]:
    """Return authentication state and a stable identity for rate limiting.

    Sessions bound to an API token are revalidated against the token row, so a
    revoked or expired credential cannot keep writing persistent reports.
    Deployments without either OIDC or JWT authentication are treated as trusted
    (LAN/dev model). JWT-edge deployments without OIDC are still public here.
    """
    auth_service = getattr(request.app.state, "auth_service", None)
    if not auth_service:
        return False, None
    if not api_authentication_enabled(auth_service):
        return True, None

    try:
        session_user = request.session.get("user")
    except AssertionError:
        session_user = None
    session_identifier = None
    if session_user:
        session_identifier = (
            str(session_user.get("sub") or session_user.get("user_identifier") or "")
            or None
        )

    session_bound_token_id = _session_bound_token_id(request)
    if session_bound_token_id is not None:
        engine = getattr(auth_service, "database_engine", None)
        if not engine:
            return True, session_identifier
        valid = await _token_bound_session_is_valid(engine, session_bound_token_id)
        return valid, session_identifier if valid else None

    if session_user:
        return True, session_identifier

    credential = extract_api_credential(request)
    if not credential:
        return False, None

    api_user = await auth_service.get_user_from_api_token(
        f"Bearer {credential}", request
    )
    if api_user is None:
        return False, None
    identifier = (
        str(api_user.get("sub") or api_user.get("user_identifier") or "") or None
    )
    return True, identifier


@errors_api_router.post("/")
async def report_frontend_error(
    request: Request,
) -> FrontendErrorReportResponse:
    """Report a frontend JavaScript error.

    This endpoint receives error reports from the web client and logs them
    using Python's logging system. The SQLAlchemyErrorHandler automatically
    stores ERROR-level logs in the database.

    A non-error ``severity`` ("info"/"warning"/"debug") routes the report to the
    in-memory telemetry ring buffer instead of the error log, so high-frequency
    breadcrumbs do not drown genuine errors. Absent or "error" severity behaves
    as before (logged at ERROR, persisted to error_logs).

    This endpoint is intentionally reachable without a user session so error
    capture works before login or with broken auth. Because it is therefore
    exposed unauthenticated (including past the edge under
    docs/design/jwt-edge-auth.md), abuse is bounded server-side rather than by
    access control: per-client rate limiting, a hard body-size cap enforced
    while the body is still streaming (before it is ever fully buffered), field
    limits on the report model, and unauthenticated reports are clamped into
    the telemetry ring buffer — never persisted to error_logs and lost on
    process restart — regardless of claimed severity.
    """
    reporter_authenticated, reporter_identifier = await _reporter_authentication(
        request
    )
    client_address = request.client.host if request.client else "unknown"
    rate_limit_key = (
        f"user:{reporter_identifier}"
        if reporter_authenticated and reporter_identifier
        else f"address:{client_address}"
    )
    limiter = getattr(request.app.state, "error_intake_rate_limiter", None)
    if not isinstance(limiter, ErrorIntakeRateLimiter):
        raise HTTPException(
            status_code=500, detail="Error intake limiter is unavailable."
        )
    if not limiter.allow(rate_limit_key):
        raise HTTPException(status_code=429, detail="Too many error reports.")

    try:
        error_report = FrontendErrorReport.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    extra_data = {
        "url": error_report.url,
        "user_agent": error_report.user_agent,
        "component_name": error_report.component_name,
        "error_type": error_report.error_type,
        "stack": error_report.stack,
        "details": error_report.extra_data,
        "reporter_authenticated": reporter_authenticated,
    }

    severity = (error_report.severity or "error").strip().lower()
    telemetry_level = _TELEMETRY_LEVELS.get(severity)
    if not reporter_authenticated:
        # Clamp unauthenticated reports into the bounded ring buffer; a
        # claimed "error" severity must not buy persistence.
        telemetry_level = logging.INFO

    if telemetry_level is None:
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
            severity=severity if reporter_authenticated else "info",
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
    db_context: Annotated[Database, Depends(get_db)],
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
    db_context: Annotated[Database, Depends(get_db)],
    _: Annotated[dict, Depends(get_diagnostics_reader)],
) -> ErrorLogResponse:
    """Get a specific error log by ID."""
    error = await db_context.error_logs.get_by_id(error_id)
    if not error:
        raise HTTPException(404, "Error log not found")

    return ErrorLogResponse(**error)
