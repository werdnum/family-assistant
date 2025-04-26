"""
Testing utilities for storage modules.

This module provides utilities for testing storage functions with
an in-memory database.
"""

import asyncio
import logging
from typing import Any, AsyncGenerator, Callable, Optional, TypeVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine, AsyncConnection
from sqlalchemy import event, MetaData

# Use absolute package paths
from family_assistant.storage.base import metadata
from family_assistant.storage.context import DatabaseContext

logger = logging.getLogger(__name__)

T = TypeVar("T")


@pytest.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """
    Create an in-memory SQLite database engine for testing.

    This fixture creates an isolated in-memory SQLite database for testing.
    It yields an AsyncEngine that can be used to create connections and
    execute queries. When the test is complete, the engine is disposed of.

    Yields:
        An AsyncEngine connected to an in-memory SQLite database.
    """
    # Create an in-memory SQLite database with foreign key support
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
        isolation_level="AUTOCOMMIT",  # Important for transaction tests
    )

    # Create all tables in the metadata
    async with engine.begin() as conn:
        # First, enable foreign key support for SQLite
        await conn.execute(
            AsyncConnection.sync_connection_callable(
                lambda sync_conn: sync_conn.execute("PRAGMA foreign_keys=ON")
            )
        )

        # Create all tables
        await conn.run_sync(metadata.create_all)

    logger.info("Created in-memory SQLite database with tables")

    yield engine

    # Dispose of the engine after the test
    await engine.dispose()
    logger.info("Disposed of test engine")


@pytest.fixture
async def test_db_context(
    test_engine: AsyncEngine,
) -> AsyncGenerator[DatabaseContext, None]:
    """
    Create a DatabaseContext with a test engine.

    This fixture creates a DatabaseContext using the test_engine fixture.
    It yields the context for use in tests.

    Args:
        test_engine: The test engine fixture.

    Yields:
        A DatabaseContext connected to the test engine.
    """
    async with DatabaseContext(engine=test_engine) as db:
        yield db


async def run_with_test_db(
    test_func: Callable[[DatabaseContext, Any], T], *args: Any, **kwargs: Any
) -> T:
    """
    Run a test function with a test database.

    This function creates an in-memory SQLite database, initializes it with
    the application's schema, and then runs the provided test function with
    a DatabaseContext connected to the test database.

    Args:
        test_func: An async function that takes a DatabaseContext as its first
                 argument, followed by any additional arguments.
        *args: Positional arguments to pass to the test function.
        **kwargs: Keyword arguments to pass to the test function.

    Returns:
        The return value of the test function.
    """
    # Create an in-memory SQLite database
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", echo=False, future=True
    )

    try:
        # Create schema
        async with engine.begin() as conn:
            # Enable foreign key support for SQLite
            await conn.execute(
                AsyncConnection.sync_connection_callable(
                    lambda sync_conn: sync_conn.execute("PRAGMA foreign_keys=ON")
                )
            )
            await conn.run_sync(metadata.create_all)

        # Create DatabaseContext with the test engine
        async with DatabaseContext(engine=engine) as db:
            # Run the test function
            return await test_func(db, *args, **kwargs)
    finally:
        # Clean up
        await engine.dispose()
