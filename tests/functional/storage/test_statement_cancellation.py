"""Abandoned database work must actually stop on the server.

These two mechanisms are what keep a cancelled request from leaving a query
running: cancelling the awaiting task tears the statement down immediately, and
``statement_timeout`` bounds anything that somehow outlives its caller.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from family_assistant.storage import init_db
from family_assistant.storage.base import (
    POSTGRES_STATEMENT_TIMEOUT_MS,
    create_engine_with_sqlite_optimizations,
)
from family_assistant.storage.database import Database, DatabaseTransaction
from tests.helpers import wait_for_condition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

_MARKER = "test_statement_cancellation_marker"


async def _active_marked_statements(db: Database) -> int:
    rows = await db.fetch_all(
        sa.text(
            "SELECT count(*) AS c FROM pg_stat_activity "
            "WHERE query LIKE :marker AND state = 'active' "
            "AND pid <> pg_backend_pid()"
        ).bindparams(marker=f"%{_MARKER}%")
    )
    return int(rows[0]["c"])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancelling_the_caller_stops_the_running_statement(
    db_engine: AsyncEngine,
) -> None:
    """Cancelling the task awaiting a query cancels it on the server too.

    This is what makes the disconnect middleware worth having: without it the
    statement outlives the request that asked for it, holding a pooled
    connection and database capacity for a client that is already gone.
    """
    db = Database(db_engine)
    # A second engine, so the observation does not queue behind the statement
    # it is observing.
    observer_engine = create_engine_with_sqlite_optimizations(
        db_engine.url.render_as_string(hide_password=False)
    )
    observer = Database(observer_engine)

    async def long_running() -> None:
        await db.execute(sa.text(f"SELECT pg_sleep(120) /* {_MARKER} */"))

    async def statement_is_running() -> bool:
        return await _active_marked_statements(observer) > 0

    async def statement_has_stopped() -> bool:
        return await _active_marked_statements(observer) == 0

    task = asyncio.create_task(long_running())
    try:
        await wait_for_condition(
            statement_is_running,
            timeout=15.0,
            description="statement to reach the server",
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await wait_for_condition(
            statement_has_stopped,
            timeout=15.0,
            description="statement to stop running on the server",
        )
    finally:
        if not task.done():
            task.cancel()
        await observer_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_connections_carry_a_statement_timeout(
    db_engine: AsyncEngine,
) -> None:
    """Every pooled connection gets the configured ceiling.

    The backstop for work that outlives its caller by some route the
    disconnect middleware does not cover.
    """
    db = Database(db_engine)

    rows = await db.fetch_all(sa.text("SHOW statement_timeout"))

    assert POSTGRES_STATEMENT_TIMEOUT_MS > 0
    timeout_ms = await db.fetch_all(
        sa.text(
            "SELECT setting::bigint AS ms FROM pg_settings "
            "WHERE name = 'statement_timeout'"
        )
    )
    assert int(timeout_ms[0]["ms"]) == POSTGRES_STATEMENT_TIMEOUT_MS, (
        f"statement_timeout reported as {rows[0]['statement_timeout']}"
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migrations_still_run_under_a_ceiling_that_aborts_queries(
    db_engine: AsyncEngine,
) -> None:
    """Startup migrations keep working on an engine whose ceiling is tight.

    ``init_db`` hands Alembic a connection from the application's own pool, so
    migrations would otherwise inherit ``POSTGRES_STATEMENT_TIMEOUT_MS`` -- and
    a long index build or backfill is the one place that ceiling is wrong.

    A guard against gross regressions rather than a proof: the schema offers no
    migration statement slow enough to outrun the ceiling on demand, so this
    cannot distinguish "exempt" from "fast enough". The exemption itself is one
    ``SET`` in ``_run_alembic_command``.
    """
    url = db_engine.url.render_as_string(hide_password=False)
    # Well above connection setup: below roughly 25ms the abort lands outside
    # SQLAlchemy's cursor wrapper and surfaces as a raw asyncpg error.
    tight_engine = create_engine_with_sqlite_optimizations(
        url, statement_timeout_ms=100
    )
    try:
        # Establish that this ceiling really does abort ordinary work, rather
        # than assuming it.
        with pytest.raises(DBAPIError) as exc_info:
            await Database(tight_engine).fetch_all(sa.text("SELECT pg_sleep(0.5)"))
        assert getattr(exc_info.value.orig, "pgcode", None) == "57014"

        await init_db(tight_engine)
    finally:
        await tight_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_statement_timeout_aborts_a_runaway_statement(
    db_engine: AsyncEngine,
) -> None:
    """A statement past the ceiling is aborted rather than left running."""
    db = Database(db_engine)

    async def sleep_past_a_tightened_ceiling(txn: DatabaseTransaction) -> None:
        # Tighten the ceiling for this transaction rather than waiting out the
        # production-sized one; the mechanism under test is identical.
        await txn.execute(sa.text("SET LOCAL statement_timeout = 250"))
        await txn.execute(sa.text(f"SELECT pg_sleep(30) /* {_MARKER} */"))

    with pytest.raises(DBAPIError) as exc_info:
        await db.atomic(sleep_past_a_tightened_ceiling)

    # 57014 = query_canceled. Notably not in the retryable allowlist, so
    # ``atomic()`` surfaces it instead of replaying the runaway three times.
    assert getattr(exc_info.value.orig, "pgcode", None) == "57014"
