"""Functional tests for the report_technical_problem tool.

Runs the tool against a real database engine (SQLite and PostgreSQL via the
db_engine fixture) and verifies that the reported problem is persisted to the
error_logs table so it surfaces in the errors/diagnostics endpoints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.problem_reporting import (
    REPORTED_PROBLEM_LOGGER_NAME,
    report_technical_problem_tool,
)
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _make_exec_context(db: DatabaseContext) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="telegram",
        conversation_id="conv-123",
        user_name="Reporter",
        turn_id="turn-9",
        db_context=db,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_report_technical_problem_persists_error_log(
    db_engine: AsyncEngine,
) -> None:
    """A reported problem is stored as an ERROR-level error_logs row."""
    async with DatabaseContext(engine=db_engine) as db:
        result = await report_technical_problem_tool(
            _make_exec_context(db),
            description="search_documents returned a 500 error",
            details="Happened when searching for 'budget'.",
        )

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["recorded"] is True
        assert data["level"] == "ERROR"

        logs = await db.error_logs.get_all(logger_name=REPORTED_PROBLEM_LOGGER_NAME)
        assert len(logs) == 1
        row = logs[0]
        assert row["level"] == "ERROR"
        assert row["message"] == "search_documents returned a 500 error"
        extra = row["extra_data"]
        assert isinstance(extra, dict)
        assert extra["severity"] == "error"
        assert extra["details"] == "Happened when searching for 'budget'."
        assert extra["conversation_id"] == "conv-123"
        assert extra["reported_by"] == "Reporter"


@pytest.mark.asyncio
async def test_report_technical_problem_maps_warning_severity(
    db_engine: AsyncEngine,
) -> None:
    """A 'warning' severity is stored as a WARNING-level row."""
    async with DatabaseContext(engine=db_engine) as db:
        result = await report_technical_problem_tool(
            _make_exec_context(db),
            description="Calendar sync looked slow",
            severity="warning",
        )

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["level"] == "WARNING"

        logs = await db.error_logs.get_all(
            level="WARNING", logger_name=REPORTED_PROBLEM_LOGGER_NAME
        )
        assert len(logs) == 1
        assert logs[0]["message"] == "Calendar sync looked slow"


@pytest.mark.asyncio
async def test_report_technical_problem_unknown_severity_defaults_to_error(
    db_engine: AsyncEngine,
) -> None:
    """An unrecognized severity falls back to ERROR rather than failing."""
    async with DatabaseContext(engine=db_engine) as db:
        result = await report_technical_problem_tool(
            _make_exec_context(db),
            description="Something odd",
            severity="catastrophic",
        )

        data = result.get_data()
        assert isinstance(data, dict)
        assert data["level"] == "ERROR"
        assert data["severity"] == "error"
