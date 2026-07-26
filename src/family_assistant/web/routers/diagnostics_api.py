"""API endpoints for diagnostic export.

This module provides endpoints for exporting diagnostic data useful for debugging,
including error logs, LLM request/response records, and message history.
"""

import platform
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from family_assistant.llm.request_buffer import get_request_buffer
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.types import ToolDefinition
from family_assistant.web.dependencies import get_db, get_diagnostics_reader

diagnostics_api_router = APIRouter()

# Export format type - using Literal for better FastAPI compatibility
ExportFormat = Literal["json", "markdown"]


class SystemInfo(BaseModel):
    """System information for diagnostic context."""

    python_version: str
    platform: str
    database_type: str


class ErrorLogExport(BaseModel):
    """Error log entry for export."""

    timestamp: str
    level: str
    logger: str
    message: str
    exception_type: str | None = None
    traceback: str | None = None


class LLMRequestExport(BaseModel):
    """LLM request/response record for export."""

    timestamp: str
    request_id: str
    model_id: str
    duration_ms: float
    # ast-grep-ignore: no-dict-any - LLM messages have provider-specific structure (OpenAI/Anthropic/Google formats differ)
    messages: list[dict[str, Any]]
    tools: list[ToolDefinition] | None = None
    tool_choice: str | None = None
    # ast-grep-ignore: no-dict-any - LLM response structure varies by provider (OpenAI/Anthropic/Google formats differ)
    response: dict[str, Any] | None = None
    error: str | None = None


class MessageHistoryExport(BaseModel):
    """Message history entry for export."""

    timestamp: str
    role: str
    content: str | None = None
    conversation_id: str
    interface_type: str


class ExportSummary(BaseModel):
    """Summary of exported data counts."""

    error_count: int
    llm_request_count: int
    message_count: int


class DiagnosticsExportResponse(BaseModel):
    """Full diagnostic export response."""

    export_timestamp: str
    time_window_minutes: int
    system_info: SystemInfo
    error_logs: list[ErrorLogExport]
    llm_requests: list[LLMRequestExport]
    message_history: list[MessageHistoryExport]
    summary: ExportSummary


class DiagnosticCount(BaseModel):
    """Count for one diagnostics breakdown value."""

    key: str | None
    count: int


class TaintAuditDiagnostics(BaseModel):
    """Aggregated runtime taint audit decisions for a bounded time window."""

    matched_event_count: int
    included_event_count: int
    truncated: bool
    oldest_included_timestamp: str | None
    newest_included_timestamp: str | None
    by_event_type: list[DiagnosticCount]
    by_mode: list[DiagnosticCount]
    by_max_tier: list[DiagnosticCount]
    by_sink_class: list[DiagnosticCount]
    by_requested_outcome: list[DiagnosticCount]
    by_effective_outcome: list[DiagnosticCount]
    by_tool: list[DiagnosticCount]
    source_type_occurrences: list[DiagnosticCount]
    source_tier_occurrences: list[DiagnosticCount]
    source_label_occurrences: list[DiagnosticCount]


class MessageHistoryTaintGroup(BaseModel):
    """One distinct-row group in the message-history taint inventory."""

    status: Literal["classified", "malformed", "missing", "not_applicable"]
    interface_type: str
    role: str
    processing_profile_id: str | None
    tool_name: str | None
    metadata_version: str | None
    max_tier: str | None
    pre_epoch: bool | None
    oldest_timestamp: datetime
    newest_timestamp: datetime
    count: int


class MessageHistoryTaintDiagnostics(BaseModel):
    """Breakdown of persisted message-history rows by taint state.

    The ``*_epoch_*`` fields are ``None`` unless
    ``taint_policy.history_taint_epoch`` is configured; with an epoch set they
    split the inventory into rows amnestied at read time (pre-epoch) and rows
    trusted as recorded (post-epoch), and count the post-epoch rows whose
    missing metadata indicates a write-path regression.
    """

    total_rows: int
    classified_rows: int
    missing_required_metadata_rows: int
    malformed_metadata_rows: int
    not_applicable_rows: int
    pre_epoch_rows: int | None
    post_epoch_rows: int | None
    post_epoch_missing_required_metadata_rows: int | None
    by_status: list[DiagnosticCount]
    by_metadata_version: list[DiagnosticCount]
    by_max_tier: list[DiagnosticCount]
    by_role: list[DiagnosticCount]
    by_interface_type: list[DiagnosticCount]
    by_processing_profile: list[DiagnosticCount]
    by_tool_name: list[DiagnosticCount]
    groups: list[MessageHistoryTaintGroup]


class TaintDiagnosticsResponse(BaseModel):
    """Runtime taint audit and persisted-history diagnostics."""

    generated_at: str
    window_days: int
    max_events: int
    history_taint_epoch: str | None
    audit: TaintAuditDiagnostics
    message_history: MessageHistoryTaintDiagnostics


def _diagnostic_counts(counter: Counter[str | None]) -> list[DiagnosticCount]:
    """Render deterministic count breakdowns with the largest groups first."""
    return [
        DiagnosticCount(key=key, count=count)
        for key, count in sorted(
            counter.items(),
            key=lambda item: (-item[1], item[0] or ""),
        )
    ]


def _format_markdown_export(data: DiagnosticsExportResponse) -> str:
    """Format the diagnostic export as markdown."""
    lines = [
        "# Diagnostic Export",
        f"**Generated**: {data.export_timestamp} | **Window**: {data.time_window_minutes} min",
        "",
    ]

    # System info
    lines.extend([
        "## System Info",
        f"- Python: {data.system_info.python_version}",
        f"- Platform: {data.system_info.platform}",
        f"- Database: {data.system_info.database_type}",
        "",
    ])

    # Error logs
    lines.append(f"## Error Logs ({data.summary.error_count} entries)")
    if data.error_logs:
        for error in data.error_logs:
            lines.append(f"### [{error.timestamp}] {error.level} {error.logger}")
            lines.append(f"{error.message}")
            if error.exception_type:
                lines.append(f"**Exception**: {error.exception_type}")
            if error.traceback:
                lines.append("```")
                lines.append(error.traceback[:2000])  # Truncate long tracebacks
                if len(error.traceback) > 2000:
                    lines.append("... (truncated)")
                lines.append("```")
            lines.append("")
    else:
        lines.append("_No errors in time window_")
        lines.append("")

    # LLM requests
    lines.append(f"## LLM Requests ({data.summary.llm_request_count} entries)")
    if data.llm_requests:
        for req in data.llm_requests:
            status = "✓" if req.error is None else "✗"
            lines.append(
                f"### [{req.timestamp}] {status} {req.model_id} ({req.duration_ms:.0f}ms)"
            )
            lines.append(f"**Request ID**: {req.request_id}")

            # Summarize messages
            lines.append(f"**Messages**: {len(req.messages)} message(s)")
            for msg in req.messages[:3]:  # Show first 3 messages
                role = msg.get("role", "unknown")
                content = str(msg.get("content", ""))[:100]
                if len(str(msg.get("content", ""))) > 100:
                    content += "..."
                lines.append(f"  - {role}: {content}")
            if len(req.messages) > 3:
                lines.append(f"  - ... and {len(req.messages) - 3} more")

            if req.tools:
                tool_names = [t.get("function", {}).get("name", "?") for t in req.tools]
                lines.append(f"**Tools**: {', '.join(tool_names[:5])}")
                if len(tool_names) > 5:
                    lines.append(f"  ... and {len(tool_names) - 5} more")

            if req.error:
                lines.append(f"**Error**: {req.error}")

            if req.response:
                content = req.response.get("content")
                if content:
                    content_preview = content[:200]
                    if len(content) > 200:
                        content_preview += "..."
                    lines.append(f"**Response**: {content_preview}")
                tool_calls = req.response.get("tool_calls")
                if tool_calls:
                    lines.append(f"**Tool Calls**: {len(tool_calls)}")

            lines.append("")
    else:
        lines.append("_No LLM requests in time window_")
        lines.append("")

    # Message history
    lines.append(f"## Message History ({data.summary.message_count} entries)")
    if data.message_history:
        for msg in data.message_history:
            content_preview = (msg.content or "")[:100]
            if len(msg.content or "") > 100:
                content_preview += "..."
            lines.append(f"- [{msg.timestamp}] **{msg.role}**: {content_preview}")
    else:
        lines.append("_No messages in time window_")

    lines.append("")
    lines.append("---")
    lines.append("Generated with Family Assistant Diagnostics Export")

    return "\n".join(lines)


@diagnostics_api_router.get("/export", response_model=None)
async def export_diagnostics(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    _: Annotated[dict, Depends(get_diagnostics_reader)],
    minutes: Annotated[int, Query(ge=1, le=120)] = 30,
    max_errors: Annotated[int, Query(ge=1, le=100)] = 50,
    max_llm_requests: Annotated[int, Query(ge=1, le=100)] = 20,
    max_messages: Annotated[int, Query(ge=1, le=500)] = 100,
    conversation_id: str | None = None,
    format: ExportFormat = "json",
) -> DiagnosticsExportResponse | PlainTextResponse:
    """Export diagnostic data for debugging.

    Returns a combined export of error logs, LLM requests, and message history
    from the specified time window. Designed for use with curl and jq.

    Examples:
        # Get JSON export (default)
        curl -s http://localhost:8000/api/diagnostics/export | jq .

        # Get just LLM requests
        curl -s http://localhost:8000/api/diagnostics/export | jq '.llm_requests'

        # Get errors from last 5 minutes
        curl -s 'http://localhost:8000/api/diagnostics/export?minutes=5' | jq '.error_logs'

        # Get markdown format
        curl -s 'http://localhost:8000/api/diagnostics/export?format=markdown'
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=minutes)

    # Get system info
    system_info = SystemInfo(
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        database_type=db_context.engine.dialect.name,
    )

    # Get error logs
    error_rows = await db_context.error_logs.get_all(
        since=cutoff,
        limit=max_errors,
    )
    error_logs = [
        ErrorLogExport(
            timestamp=row["timestamp"].isoformat() if row.get("timestamp") else "",
            level=row.get("level", ""),
            logger=row.get("logger_name", ""),
            message=row.get("message", ""),
            exception_type=row.get("exception_type"),
            traceback=row.get("traceback"),
        )
        for row in error_rows
    ]

    # Get LLM requests from ring buffer
    llm_buffer = get_request_buffer()
    llm_records = llm_buffer.get_recent(limit=max_llm_requests, since_minutes=minutes)
    llm_requests = [
        LLMRequestExport(
            timestamp=record.timestamp.isoformat(),
            request_id=record.request_id,
            model_id=record.model_id,
            duration_ms=record.duration_ms,
            messages=record.messages,
            tools=record.tools,
            tool_choice=record.tool_choice,
            response=record.response,
            error=record.error,
        )
        for record in llm_records
    ]

    # Get message history
    message_rows = await db_context.message_history.get_all_grouped(
        conversation_id=conversation_id,
        date_from=cutoff,
        include_internal=True,
    )

    # Flatten and sort messages
    all_messages: list[MessageHistoryExport] = []
    for (interface_type, conv_id), messages in message_rows.items():
        for msg in messages:
            timestamp = msg.get("timestamp")
            if isinstance(timestamp, datetime):
                timestamp_str = timestamp.isoformat()
            elif timestamp:
                timestamp_str = str(timestamp)
            else:
                timestamp_str = ""

            all_messages.append(
                MessageHistoryExport(
                    timestamp=timestamp_str,
                    role=msg.get("role", ""),
                    content=msg.get("content"),
                    conversation_id=conv_id,
                    interface_type=interface_type,
                )
            )

    # Sort by timestamp descending (newest first) and limit
    all_messages.sort(key=lambda m: m.timestamp, reverse=True)
    all_messages = all_messages[:max_messages]

    # Build response
    response = DiagnosticsExportResponse(
        export_timestamp=now.isoformat(),
        time_window_minutes=minutes,
        system_info=system_info,
        error_logs=error_logs,
        llm_requests=llm_requests,
        message_history=all_messages,
        summary=ExportSummary(
            error_count=len(error_logs),
            llm_request_count=len(llm_requests),
            message_count=len(all_messages),
        ),
    )

    if format == "markdown":
        return PlainTextResponse(
            content=_format_markdown_export(response),
            media_type="text/markdown",
        )

    return response


@diagnostics_api_router.get("/taint-audit")
async def get_taint_diagnostics(
    db_context: Annotated[DatabaseContext, Depends(get_db)],
    _: Annotated[dict, Depends(get_diagnostics_reader)],
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    max_events: Annotated[int, Query(ge=1, le=50000)] = 10000,
) -> TaintDiagnosticsResponse:
    """Return aggregate taint audit decisions and message-history taint coverage.

    The audit window is bounded by ``days`` and ``max_events``. Message-history
    counts cover every distinct persisted row so repeated history reads cannot
    inflate the apparent legacy migration scope. No message content, tool
    arguments, conversation identifiers, or source identifiers are returned.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    history_taint_epoch = db_context.history_taint_epoch
    audit_count = await db_context.taint_audit_events.count_since(cutoff)
    audit_events = await db_context.taint_audit_events.list_since(
        cutoff, limit=max_events
    )
    history_rows = await db_context.message_history.get_taint_diagnostics(
        history_taint_epoch=history_taint_epoch
    )

    event_type_counts: Counter[str | None] = Counter()
    mode_counts: Counter[str | None] = Counter()
    max_tier_counts: Counter[str | None] = Counter()
    sink_class_counts: Counter[str | None] = Counter()
    requested_outcome_counts: Counter[str | None] = Counter()
    effective_outcome_counts: Counter[str | None] = Counter()
    tool_counts: Counter[str | None] = Counter()
    source_type_counts: Counter[str | None] = Counter()
    source_tier_counts: Counter[str | None] = Counter()
    source_label_counts: Counter[str | None] = Counter()
    for event in audit_events:
        event_type_counts[event["event_type"]] += 1
        mode_counts[event["mode"]] += 1
        max_tier_counts[event["max_tier"]] += 1
        sink_class_counts[event["sink_class"]] += 1
        requested_outcome_counts[event["requested_outcome"]] += 1
        effective_outcome_counts[event["effective_outcome"]] += 1
        tool_counts[event["tool_name"]] += 1
        for source in event["sources_json"]:
            source_type_counts[source["source_type"]] += 1
            source_tier_counts[source["tier"]] += 1
            for label in source["labels"]:
                source_label_counts[label] += 1

    status_counts: Counter[str | None] = Counter()
    version_counts: Counter[str | None] = Counter()
    history_tier_counts: Counter[str | None] = Counter()
    role_counts: Counter[str | None] = Counter()
    interface_counts: Counter[str | None] = Counter()
    processing_profile_counts: Counter[str | None] = Counter()
    history_tool_counts: Counter[str | None] = Counter()
    total_history_rows = 0
    pre_epoch_rows = 0
    post_epoch_rows = 0
    post_epoch_missing_rows = 0
    for row in history_rows:
        count = row["count"]
        total_history_rows += count
        status_counts[row["status"]] += count
        version_counts[row["metadata_version"]] += count
        history_tier_counts[row["max_tier"]] += count
        role_counts[row["role"]] += count
        interface_counts[row["interface_type"]] += count
        processing_profile_counts[row["processing_profile_id"]] += count
        history_tool_counts[row["tool_name"]] += count
        if row["pre_epoch"]:
            pre_epoch_rows += count
        else:
            post_epoch_rows += count
            if row["status"] == "missing":
                post_epoch_missing_rows += count

    timestamps = [event["created_at"] for event in audit_events]
    return TaintDiagnosticsResponse(
        generated_at=now.isoformat(),
        window_days=days,
        max_events=max_events,
        history_taint_epoch=(
            history_taint_epoch.isoformat() if history_taint_epoch else None
        ),
        audit=TaintAuditDiagnostics(
            matched_event_count=audit_count,
            included_event_count=len(audit_events),
            truncated=audit_count > len(audit_events),
            oldest_included_timestamp=min(timestamps).isoformat()
            if timestamps
            else None,
            newest_included_timestamp=max(timestamps).isoformat()
            if timestamps
            else None,
            by_event_type=_diagnostic_counts(event_type_counts),
            by_mode=_diagnostic_counts(mode_counts),
            by_max_tier=_diagnostic_counts(max_tier_counts),
            by_sink_class=_diagnostic_counts(sink_class_counts),
            by_requested_outcome=_diagnostic_counts(requested_outcome_counts),
            by_effective_outcome=_diagnostic_counts(effective_outcome_counts),
            by_tool=_diagnostic_counts(tool_counts),
            source_type_occurrences=_diagnostic_counts(source_type_counts),
            source_tier_occurrences=_diagnostic_counts(source_tier_counts),
            source_label_occurrences=_diagnostic_counts(source_label_counts),
        ),
        message_history=MessageHistoryTaintDiagnostics(
            total_rows=total_history_rows,
            classified_rows=status_counts["classified"],
            missing_required_metadata_rows=status_counts["missing"],
            malformed_metadata_rows=status_counts["malformed"],
            not_applicable_rows=status_counts["not_applicable"],
            pre_epoch_rows=pre_epoch_rows if history_taint_epoch else None,
            post_epoch_rows=post_epoch_rows if history_taint_epoch else None,
            post_epoch_missing_required_metadata_rows=(
                post_epoch_missing_rows if history_taint_epoch else None
            ),
            by_status=_diagnostic_counts(status_counts),
            by_metadata_version=_diagnostic_counts(version_counts),
            by_max_tier=_diagnostic_counts(history_tier_counts),
            by_role=_diagnostic_counts(role_counts),
            by_interface_type=_diagnostic_counts(interface_counts),
            by_processing_profile=_diagnostic_counts(processing_profile_counts),
            by_tool_name=_diagnostic_counts(history_tool_counts),
            groups=[MessageHistoryTaintGroup(**row) for row in history_rows],
        ),
    )
