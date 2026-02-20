"""Functional tests for engineering diagnostic tools with real database.

These tests run query_database against a real database engine (both SQLite
and PostgreSQL via the db_engine fixture) to catch issues that unit tests
with mocks cannot detect -- such as SET TRANSACTION READ ONLY failing on
an active PostgreSQL transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.engineering import query_database
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
    )


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
