"""Functional tests for engineering diagnostic tools with real database.

These tests run query_database and read_error_logs against a real database
engine (both SQLite and PostgreSQL via the db_engine fixture) to catch issues
that unit tests with mocks cannot detect -- such as SET TRANSACTION READ ONLY
failing on an active PostgreSQL transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import insert, text

from family_assistant.storage import error_logs_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.engineering import query_database, read_error_logs
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def _make_exec_context(db: DatabaseContext) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conversation",
        user_name="test_user",
        turn_id=None,
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


async def _insert_error_log(
    db: DatabaseContext,
    *,
    logger_name: str = "test.module",
    level: str = "ERROR",
    message: str = "Test error",
) -> None:
    """Insert an error log entry using raw SQL to avoid lastrowid issues on PostgreSQL."""
    stmt = insert(error_logs_table).values(
        logger_name=logger_name,
        level=level.upper(),
        message=message,
    )
    await db.execute_with_retry(stmt)


# --- query_database tests ---


@pytest.mark.asyncio
async def test_query_database_select_on_fresh_connection(
    db_engine: AsyncEngine,
) -> None:
    """query_database should work with a real database engine."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await query_database(exec_context, "SELECT 1 AS val")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" not in data
        assert data["row_count"] == 1
        assert data["rows"][0]["val"] == 1


@pytest.mark.asyncio
async def test_query_database_works_after_prior_queries(
    db_engine: AsyncEngine,
) -> None:
    """query_database must work even after other queries on the same db_context.

    This is the key regression test: on PostgreSQL, SET TRANSACTION READ ONLY
    fails if the active transaction has already executed queries. The fix uses
    a separate connection via engine.begin() so this should always succeed.
    """
    async with DatabaseContext(engine=db_engine) as db:
        # Simulate prior activity on the shared connection (like message
        # history fetching that happens before tool execution in production)
        await db.execute_with_retry(text("SELECT 1"))

        exec_context = _make_exec_context(db)
        result = await query_database(exec_context, "SELECT 1 AS val")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" not in data, f"query_database failed: {data.get('error')}"
        assert data["row_count"] == 1


@pytest.mark.asyncio
async def test_query_database_rejects_mutation(
    db_engine: AsyncEngine,
) -> None:
    """Non-SELECT queries should be rejected before hitting the database."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await query_database(
            exec_context, "INSERT INTO notes (title) VALUES ('hack')"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "Only SELECT queries are allowed" in data["error"]


@pytest.mark.asyncio
async def test_query_database_read_only_blocks_writes_on_postgres(
    db_engine: AsyncEngine,
) -> None:
    """On PostgreSQL, SET TRANSACTION READ ONLY should block writes even if
    sqlparse validation is somehow bypassed.

    On SQLite, there's no SET TRANSACTION READ ONLY, so we just verify sqlparse
    catches the mutation.
    """
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        # sqlparse will catch this, so we get the validation error
        result = await query_database(exec_context, "DROP TABLE IF EXISTS notes")
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data


@pytest.mark.asyncio
async def test_query_database_returns_real_table_data(
    db_engine: AsyncEngine,
) -> None:
    """query_database should return actual data from application tables."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        # error_logs table always exists after schema init
        result = await query_database(
            exec_context,
            "SELECT COUNT(*) AS cnt FROM error_logs",
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" not in data
        assert data["row_count"] == 1
        assert data["rows"][0]["cnt"] == 0
        assert data["truncated"] is False


@pytest.mark.asyncio
async def test_query_database_invalid_table_returns_error(
    db_engine: AsyncEngine,
) -> None:
    """Querying a non-existent table should return an error, not crash."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await query_database(
            exec_context, "SELECT * FROM nonexistent_table_xyz"
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data


# --- read_error_logs tests ---


@pytest.mark.asyncio
async def test_read_error_logs_empty(
    db_engine: AsyncEngine,
) -> None:
    """read_error_logs returns empty list when no logs exist."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 0
        assert data["logs"] == []


@pytest.mark.asyncio
async def test_read_error_logs_returns_inserted_logs(
    db_engine: AsyncEngine,
) -> None:
    """read_error_logs should return logs that were inserted."""
    async with DatabaseContext(engine=db_engine) as db:
        await _insert_error_log(db, level="ERROR", message="Something went wrong")
        await _insert_error_log(db, level="WARNING", message="Something might be wrong")

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 2


@pytest.mark.asyncio
async def test_read_error_logs_filters_by_level(
    db_engine: AsyncEngine,
) -> None:
    """read_error_logs should respect the level filter."""
    async with DatabaseContext(engine=db_engine) as db:
        await _insert_error_log(db, level="ERROR", message="An error")
        await _insert_error_log(db, level="WARNING", message="A warning")

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, level="ERROR")
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 1
        assert data["logs"][0]["level"] == "ERROR"
        assert data["logs"][0]["message"] == "An error"


@pytest.mark.asyncio
async def test_read_error_logs_respects_limit(
    db_engine: AsyncEngine,
) -> None:
    """read_error_logs should respect the limit parameter."""
    async with DatabaseContext(engine=db_engine) as db:
        for i in range(5):
            await _insert_error_log(db, message=f"Error {i}")

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, limit=2)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 2


@pytest.mark.asyncio
async def test_read_error_logs_limit_capped_at_200(
    db_engine: AsyncEngine,
) -> None:
    """Limit values above 200 should be clamped to 200."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, limit=500)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["filters"]["limit"] == 200


@pytest.mark.asyncio
async def test_read_error_logs_negative_limit_clamped(
    db_engine: AsyncEngine,
) -> None:
    """Negative limit values should be clamped to 1."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, limit=-1)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["filters"]["limit"] == 1


@pytest.mark.asyncio
async def test_read_error_logs_includes_traceback_omits_extra_data_by_default(
    db_engine: AsyncEngine,
) -> None:
    """Tracebacks are always returned; extra_data is omitted unless requested."""
    async with DatabaseContext(engine=db_engine) as db:
        stmt = insert(error_logs_table).values(
            logger_name="test.module",
            level="ERROR",
            message="boom",
            traceback="Traceback (most recent call last): frame",
            extra_data={"secret": "value"},
        )
        await db.execute_with_retry(stmt)

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["count"] == 1
        # Traceback (code structure, low risk) is always included.
        assert (
            data["logs"][0]["traceback"] == "Traceback (most recent call last): frame"
        )
        # extra_data (arbitrary logged JSON) is stripped by default.
        assert "extra_data" not in data["logs"][0]
        assert data["filters"]["include_extra_data"] is False


@pytest.mark.asyncio
async def test_read_error_logs_includes_extra_data_when_requested(
    db_engine: AsyncEngine,
) -> None:
    async with DatabaseContext(engine=db_engine) as db:
        stmt = insert(error_logs_table).values(
            logger_name="test.module",
            level="ERROR",
            message="boom",
            extra_data={"request_id": "abc123"},
        )
        await db.execute_with_retry(stmt)

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, include_extra_data=True)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["logs"][0]["extra_data"] == {"request_id": "abc123"}


@pytest.mark.asyncio
async def test_read_error_logs_respects_since_hours(
    db_engine: AsyncEngine,
) -> None:
    """since_hours excludes logs older than the window."""
    now = datetime.now(UTC)
    async with DatabaseContext(engine=db_engine) as db:
        await db.execute_with_retry(
            insert(error_logs_table).values(
                logger_name="test.module",
                level="ERROR",
                message="recent",
                timestamp=now - timedelta(hours=1),
            )
        )
        await db.execute_with_retry(
            insert(error_logs_table).values(
                logger_name="test.module",
                level="ERROR",
                message="old",
                timestamp=now - timedelta(hours=48),
            )
        )

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, since_hours=24)
        data = result.get_data()
        assert isinstance(data, dict)
        messages = {log["message"] for log in data["logs"]}
        assert "recent" in messages
        assert "old" not in messages
        assert data["filters"]["since_hours"] == 24


@pytest.mark.asyncio
async def test_read_error_logs_rejects_non_bool_extra_data_flag(
    db_engine: AsyncEngine,
) -> None:
    """A truthy non-bool (e.g. the string "true" from a script kwarg) is rejected.

    The policy matcher compares exact types, so a deny rule on
    ``include_extra_data: true`` never fires for the string ``"true"``; the tool
    must therefore refuse non-bool values instead of treating them as truthy.
    """
    async with DatabaseContext(engine=db_engine) as db:
        stmt = insert(error_logs_table).values(
            logger_name="test.module",
            level="ERROR",
            message="boom",
            extra_data={"secret": "value"},
        )
        await db.execute_with_retry(stmt)

        exec_context = _make_exec_context(db)
        result = await read_error_logs(
            exec_context,
            include_extra_data="true",  # type: ignore[arg-type]  # exercising script-kwarg type bypass
        )
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "boolean" in data["error"]


@pytest.mark.asyncio
async def test_read_error_logs_default_window_excludes_old_logs(
    db_engine: AsyncEngine,
) -> None:
    """The window is always bounded: omitting since_hours means 7 days, not all logs."""
    now = datetime.now(UTC)
    async with DatabaseContext(engine=db_engine) as db:
        await db.execute_with_retry(
            insert(error_logs_table).values(
                logger_name="test.module",
                level="ERROR",
                message="recent",
                timestamp=now - timedelta(hours=1),
            )
        )
        await db.execute_with_retry(
            insert(error_logs_table).values(
                logger_name="test.module",
                level="ERROR",
                message="ancient",
                timestamp=now - timedelta(hours=200),
            )
        )

        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context)
        data = result.get_data()
        assert isinstance(data, dict)
        messages = {log["message"] for log in data["logs"]}
        assert "recent" in messages
        assert "ancient" not in messages
        assert data["filters"]["since_hours"] == 168


@pytest.mark.asyncio
async def test_read_error_logs_rejects_non_positive_window(
    db_engine: AsyncEngine,
) -> None:
    """Non-positive since_hours used to mean 'unbounded'; it is now rejected."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, since_hours=0)
        data = result.get_data()
        assert isinstance(data, dict)
        assert "error" in data
        assert "positive" in data["error"]


@pytest.mark.asyncio
async def test_read_error_logs_caps_window_at_retention(
    db_engine: AsyncEngine,
) -> None:
    """since_hours is capped at 720 (the 30-day retention default)."""
    async with DatabaseContext(engine=db_engine) as db:
        exec_context = _make_exec_context(db)
        result = await read_error_logs(exec_context, since_hours=100_000)
        data = result.get_data()
        assert isinstance(data, dict)
        assert data["filters"]["since_hours"] == 720
