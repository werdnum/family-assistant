"""Abandoned database work must actually stop on the server.

These two mechanisms are what keep a cancelled request from leaving a query
running: cancelling the awaiting task tears the statement down immediately, and
``statement_timeout`` bounds anything that somehow outlives its caller.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from family_assistant.llm.messages import UserMessage
from family_assistant.request_side_effects import begin_tracking, state_changed
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
# The revision before head: the test rewinds to it so a migration is actually
# pending while the tight ceiling is in force. Both this and the head asserted
# below move with each new migration.
_PREVIOUS_REVISION = "631e7ea62ec4"
_HEAD_REVISION = "d7065490c04e"
# Columns the head revision adds, by table: the test drops them to make the
# migration genuinely pending, then asserts they came back.
_HEAD_REVISION_COLUMNS_BY_TABLE = {
    "delegation_runs": ("model_selection_json",),
}

_alembic_version_table = sa.Table(
    "alembic_version",
    sa.MetaData(),
    sa.Column("version_num", sa.String(32), primary_key=True),
)


async def _active_marked_statements(db: Database) -> int:
    rows = await db.fetch_all(
        sa.text(
            "SELECT count(*) AS c FROM pg_stat_activity "
            "WHERE query LIKE :marker AND state = 'active' "
            "AND pid <> pg_backend_pid()"
        ).bindparams(marker=f"%{_MARKER}%")
    )
    return int(rows[0]["c"])


@pytest.mark.asyncio
async def test_repository_writes_report_a_state_change(
    db_engine: AsyncEngine,
) -> None:
    """A real write through the repositories trips the side-effect tracker.

    This is the link the disconnect middleware relies on to tell a request that
    has only read from one it would be destructive to abandon.
    """
    db = Database(db_engine)
    begin_tracking()

    await db.message_history.add_message(
        UserMessage(content="a write"),
        interface_type="web",
        conversation_id="side_effect_conv",
        timestamp=datetime(2026, 4, 1, tzinfo=UTC),
        turn_id="side-effect-turn",
        processing_profile_id="default",
        user_id="side_effect_user",
    )

    assert state_changed() is True


@pytest.mark.asyncio
async def test_reads_do_not_report_a_state_change(
    db_engine: AsyncEngine,
) -> None:
    """A read-only request stays cancellable."""
    db = Database(db_engine)
    begin_tracking()

    await db.message_history.get_conversation_summaries(
        limit=10, offset=0, owner_user_ids={"nobody"}
    )

    assert state_changed() is False


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
    """Startup migrations commit on an engine whose ceiling is tight.

    ``init_db`` hands Alembic a connection from the application's own pool, so
    migrations would otherwise inherit ``POSTGRES_STATEMENT_TIMEOUT_MS`` -- and
    a long index build or backfill is the one place that ceiling is wrong.
    """
    url = db_engine.url.render_as_string(hide_password=False)
    db = Database(db_engine)

    async def regress_to_previous_revision(txn: DatabaseTransaction) -> None:
        for table, columns in _HEAD_REVISION_COLUMNS_BY_TABLE.items():
            for column in columns:
                await txn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        await txn.execute(
            sa.update(_alembic_version_table).values(version_num=_PREVIOUS_REVISION)
        )

    await db.atomic(regress_to_previous_revision)

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

    verification_engine = create_engine_with_sqlite_optimizations(url)
    try:
        async with verification_engine.connect() as conn:
            columns_by_table = {
                table: await conn.run_sync(
                    lambda sync_conn, table=table: {
                        column["name"]
                        for column in sa.inspect(sync_conn).get_columns(table)
                    }
                )
                for table in _HEAD_REVISION_COLUMNS_BY_TABLE
            }
            revision = await conn.scalar(
                sa.select(_alembic_version_table.c.version_num)
            )
        for table, expected in _HEAD_REVISION_COLUMNS_BY_TABLE.items():
            assert set(expected) <= columns_by_table[table], table
        assert revision == _HEAD_REVISION
    finally:
        await verification_engine.dispose()


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
