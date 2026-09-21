"""Unit tests for database engine instrumentation."""

import pytest
from sqlalchemy import text

from family_assistant.storage.base import create_engine_with_sqlite_optimizations
from family_assistant.storage.instrumentation import (
    EngineInstrumentation,
    get_instrumentation,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


class FakeClock:
    """A monotonic clock the test advances explicitly, so no test waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def install(self, instrumentation: EngineInstrumentation) -> "FakeClock":
        instrumentation.monotonic = self
        return self


@pytest.mark.asyncio
async def test_engine_is_not_instrumented_by_default() -> None:
    """Production engines carry no instrumentation listeners."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL)
    try:
        assert get_instrumentation(engine) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_committed_transaction_leaves_no_violations() -> None:
    """A short transaction that commits returns its connection and passes."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL, instrument=True)
    try:
        instrumentation = get_instrumentation(engine)
        assert instrumentation is not None

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            assert instrumentation.open_transactions == 1

        assert instrumentation.open_transactions == 0
        assert instrumentation.checked_out_connections == 0
        assert instrumentation.violations() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_exceeding_the_bound_is_reported() -> None:
    """A transaction held past the bound is recorded with its opening stack."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL, instrument=True)
    try:
        instrumentation = get_instrumentation(engine)
        assert instrumentation is not None
        clock = FakeClock().install(instrumentation)

        # ast-grep-ignore: no-raw-transaction-management - test fixture setup, outside the application transaction model
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
            clock.now = 60.0

        violations = instrumentation.violations()
        assert len(violations) == 1
        assert "60.00s" in violations[0]
        assert "test_transaction_exceeding_the_bound_is_reported" in violations[0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_still_open_is_reported_with_its_origin() -> None:
    """A parked transaction is measured at inspection time, not only at commit."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL, instrument=True)
    conn = None
    try:
        instrumentation = get_instrumentation(engine)
        assert instrumentation is not None
        clock = FakeClock().install(instrumentation)

        conn = await engine.connect()
        await conn.begin()
        await conn.execute(text("SELECT 1"))
        clock.now = 90.0

        violations = instrumentation.violations()
        assert "transaction held open for 90.00s" in violations[0]
        assert (
            "test_transaction_still_open_is_reported_with_its_origin" in violations[0]
        )
        assert "1 transaction(s) still open" in violations

        # Measured, not recorded: inspecting twice must not count it twice.
        assert instrumentation.violations() == violations
    finally:
        if conn is not None:
            await conn.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_unreturned_connection_is_reported_as_a_leak() -> None:
    """A connection never returned to the pool is reported at teardown."""
    engine = create_engine_with_sqlite_optimizations(TEST_DATABASE_URL, instrument=True)
    conn = None
    try:
        instrumentation = get_instrumentation(engine)
        assert instrumentation is not None

        conn = await engine.connect()
        await conn.execute(text("SELECT 1"))

        assert instrumentation.checked_out_connections == 1
        # SQLAlchemy autobegins a transaction on the first statement, so the
        # still-open transaction is reported alongside the leak.
        assert instrumentation.violations() == [
            "1 transaction(s) still open",
            "1 connection(s) still checked out of the pool",
        ]
    finally:
        if conn is not None:
            await conn.close()
        await engine.dispose()
