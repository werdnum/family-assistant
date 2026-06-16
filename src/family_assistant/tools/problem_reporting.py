"""Tool for the assistant to report technical problems.

Provides ``report_technical_problem``, a tool available to the LLM in every
processing profile (via ``universal_tools_policy``). It records a log entry in
the ``error_logs`` table so that reported issues surface in the error-logs and
diagnostics-export endpoints used for debugging.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from family_assistant.tools.types import ToolDefinition, ToolResult

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

# Logger name recorded for problems reported through this tool. Distinct so the
# errors/diagnostics endpoints (and read_error_logs) can filter to assistant
# reports specifically.
REPORTED_PROBLEM_LOGGER_NAME = "assistant.reported_problem"

# Map the user-facing severity onto a stored log level. The error_logs table
# stores any level; the diagnostics/errors endpoints surface them all.
_SEVERITY_TO_LEVEL: dict[str, str] = {
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}
_DEFAULT_SEVERITY = "error"


REPORT_TECHNICAL_PROBLEM_TOOLS_DEFINITION: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "report_technical_problem",
            "description": (
                "Report a technical problem, bug, or unexpected behaviour you "
                "encounter while helping the user (for example: a tool returned "
                "an error, data looks wrong or inconsistent, something you were "
                "asked to do is impossible because of a defect, or you hit a "
                "limitation that looks like a bug). This records the problem in "
                "the application's error log so the developers can see it in the "
                "diagnostics. Use it whenever something seems broken, in addition "
                "to telling the user. It does not fix the problem itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": (
                            "A clear, specific description of the problem: what "
                            "went wrong, what you (or the user) were trying to do, "
                            "and any error messages you saw."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "error", "critical"],
                        "description": (
                            "How serious the problem is. Use 'error' for a "
                            "genuine malfunction (default), 'warning' for "
                            "something suspicious or degraded that still works, "
                            "'critical' for a complete failure, and 'info' for a "
                            "minor note worth recording."
                        ),
                        "default": "error",
                    },
                    "details": {
                        "type": "string",
                        "description": (
                            "Optional extra context such as steps to reproduce, "
                            "the tool or feature involved, or a stack trace / "
                            "error payload if you have one."
                        ),
                    },
                },
                "required": ["description"],
            },
        },
    },
]


async def report_technical_problem_tool(
    exec_context: ToolExecutionContext,
    description: str,
    severity: str = _DEFAULT_SEVERITY,
    details: str | None = None,
) -> ToolResult:
    """Record a technical problem reported by the assistant in the error log.

    Args:
        exec_context: The tool execution context.
        description: Description of the problem.
        severity: One of ``info``, ``warning``, ``error``, ``critical``.
        details: Optional extra context (repro steps, error payload, etc.).

    Returns:
        ToolResult with the recorded log level and the new error-log id.
    """
    normalized_severity = (severity or _DEFAULT_SEVERITY).strip().lower()
    level = _SEVERITY_TO_LEVEL.get(normalized_severity)
    if level is None:
        logger.info(
            "report_technical_problem received unknown severity %r; recording as %s",
            severity,
            _DEFAULT_SEVERITY,
        )
        normalized_severity = _DEFAULT_SEVERITY
        level = _SEVERITY_TO_LEVEL[_DEFAULT_SEVERITY]

    extra_data: dict[str, str | None] = {
        "source": "report_technical_problem",
        "severity": normalized_severity,
        "details": details,
        "interface_type": exec_context.interface_type,
        "conversation_id": exec_context.conversation_id,
        "turn_id": exec_context.turn_id,
        "reported_by": exec_context.user_name,
    }

    error_log_id = await exec_context.db_context.error_logs.add(
        logger_name=REPORTED_PROBLEM_LOGGER_NAME,
        level=level,
        message=description,
        exception_message=details,
        module="report_technical_problem",
        extra_data=extra_data,
    )

    logger.info(
        "Assistant reported a technical problem (severity=%s, error_log_id=%s): %s",
        normalized_severity,
        error_log_id,
        description,
    )

    return ToolResult(
        text=(
            f"Recorded the problem in the error log (severity: {normalized_severity}). "
            "The developers can see it in the diagnostics."
        ),
        data={
            "recorded": True,
            "severity": normalized_severity,
            "level": level,
            "error_log_id": error_log_id,
        },
    )
