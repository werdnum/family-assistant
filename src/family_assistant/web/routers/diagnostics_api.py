"""API endpoints for diagnostic export.

This module provides endpoints for exporting diagnostic data useful for debugging,
including error logs, LLM request/response records, and message history.
"""

import platform
import sys
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from family_assistant.llm.request_buffer import get_request_buffer
from family_assistant.storage.context import DatabaseContext
from family_assistant.web.dependencies import get_db

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
    # ast-grep-ignore: no-dict-any - Serialized LLM messages from external APIs
    messages: list[dict[str, Any]]
    # ast-grep-ignore: no-dict-any - Serialized tool definitions from external APIs
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | None = None
    # ast-grep-ignore: no-dict-any - Serialized LLM response from external APIs
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
    minutes: Annotated[int, Query(ge=1, le=120)] = 30,
    max_errors: Annotated[int, Query(ge=1, le=100)] = 50,
    max_llm_requests: Annotated[int, Query(ge=1, le=100)] = 20,
    max_messages: Annotated[int, Query(ge=1, le=500)] = 100,
    conversation_id: str | None = None,
    format: ExportFormat = "json",  # noqa: A002 - shadows builtin but matches API convention
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
