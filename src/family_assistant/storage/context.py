"""
Database context manager for storage operations.

This module provides a context manager and utilities for database operations,
enabling dependency injection for testing and centralizing retry logic.
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional, TypeVar, Generic, Callable, Union, cast

from sqlalchemy import Result, TextClause, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction
from sqlalchemy.sql import Select, Insert, Update, Delete
from sqlalchemy.exc import DBAPIError, ProgrammingError

# Use absolute package path
from family_assistant.storage.base import get_engine

logger = logging.getLogger(__name__)

# Type variable for query result type
T = TypeVar("T")


class DatabaseContext:
    """
    Context manager for database operations with retry logic.

    This class provides a centralized way to handle database connections,
    transactions, and retry logic for database operations.
    """

    def __init__(
        self,
        engine: Optional[AsyncEngine] = None,
        max_retries: int = 3,
        base_delay: float = 0.5,
    ):
        """
        Initialize the database context.

        Args:
            engine: Optional SQLAlchemy AsyncEngine. If not provided, the default engine from
                   storage.base will be used. This enables dependency injection for testing.
            max_retries: Maximum number of retries for database operations.
            base_delay: Base delay in seconds for exponential backoff.
        """
        self.engine = engine or get_engine()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.conn: Optional[AsyncConnection] = None
        self._transaction_cm: Optional[AsyncTransaction] = None

    async def __aenter__(self) -> "DatabaseContext":
        """Enter the async context manager, starting a transaction."""
        if self._transaction_cm is not None:
            # This shouldn't happen if used correctly with 'async with'
            raise RuntimeError("DatabaseContext is not reentrant")

        # Get the transaction context manager from engine.begin()
        self._transaction_cm = self.engine.begin()
        # Enter the transaction context manager to get the connection
        self.conn = await self._transaction_cm.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the async context manager, committing or rolling back the transaction."""
        if self._transaction_cm is None:
            # This shouldn't happen if __aenter__ succeeded
            return

        try:
            # Exit the underlying transaction context manager, which handles commit/rollback
            await self._transaction_cm.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            # Clean up references
            self.conn = None
            self._transaction_cm = None

    # Removed begin, commit, rollback methods

    async def execute_with_retry(
        self,
        query: Union[Select, Insert, Update, Delete, TextClause],
        params: Optional[Dict[str, Any]] = None,
    ) -> Result:
        """
        Execute a query with retry logic for transient database errors.

        Args:
            query: The SQLAlchemy query to execute.
            params: Optional parameters for the query.

        Returns:
            The SQLAlchemy Result object.

        Raises:
            RuntimeError: If there is no active database connection or if
                         all retry attempts fail.
        """
        if self.conn is None:
            raise RuntimeError("No active database connection")

        for attempt in range(self.max_retries):
            try:
                if params:
                    return await self.conn.execute(query, params)
                else:
                    return await self.conn.execute(query)
            except DBAPIError as e:
                # Check if the error is a ProgrammingError (syntax error, undefined object, etc.)
                # These should not be retried.
                if isinstance(e.orig, ProgrammingError) or isinstance(
                    e, ProgrammingError
                ):  # Check original and wrapper
                    logger.error(
                        f"Non-retryable ProgrammingError encountered: {e}",
                        exc_info=True,
                    )
                    raise  # Re-raise immediately, do not retry

                # Log other DBAPI errors and proceed with retry logic
                logger.warning(
                    f"Retryable DBAPIError (attempt {attempt + 1}/{self.max_retries}): {e}."
                )
                if attempt == self.max_retries - 1:
                    logger.error(
                        f"Max retries exceeded for retryable error. Raising error."
                    )
                    raise

                # Calculate backoff with jitter for retry
                delay = self.base_delay * (2**attempt) + random.uniform(
                    0, self.base_delay
                )
                logger.info(f"Retrying in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"Non-retryable error: {e}", exc_info=True)
                raise

        # This should ideally not be reached if retry logic works
        raise RuntimeError("Database operation failed after multiple retries")

    async def fetch_all(
        self, query: Union[Select, TextClause], params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute a query and fetch all results as dictionaries.

        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.

        Returns:
            A list of dictionaries representing the rows.
        """
        result = await self.execute_with_retry(query, params)
        rows = result.fetchall()
        return [row._mapping for row in rows]

    async def fetch_one(
        self, query: Union[Select, TextClause], params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Execute a query and fetch one result as a dictionary.

        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.

        Returns:
            A dictionary representing the row, or None if no results.
        """
        result = await self.execute_with_retry(query, params)
        row = result.fetchone()
        return row._mapping if row else None

    def on_commit(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """
        Register a callback to be called on transaction commit.

        Args:
            callback: A callable to be executed on commit.

        Returns:
            The original callback for chaining.
        """
        if self._transaction_cm is None:
            raise RuntimeError("No active transaction context manager")

        # Register the callback with the transaction context manager
        event.listen(self.conn.sync_connection, "commit", callback)
        return callback


# Convenience function to create a database context
# This function is now less useful as DatabaseContext manages its own transaction
# via __aenter__/__aexit__. Callers should instantiate DatabaseContext directly.
# Keeping it for now but marking as potentially deprecated or for removal.
async def get_db_context(
    engine: Optional[AsyncEngine] = None, max_retries: int = 3, base_delay: float = 0.5
) -> DatabaseContext:
    """
    Create and enter a database context.

    This function creates a DatabaseContext and enters its async context manager,
    returning the active context. This is intended to be used with an
    async with statement.

    Args:
        engine: Optional SQLAlchemy AsyncEngine for dependency injection.
        max_retries: Maximum number of retries for database operations.
        base_delay: Base delay in seconds for exponential backoff.

    Returns:
        An active DatabaseContext.

    Example:
        ```python
        async with get_db_context() as db:
            result = await db.fetch_all(...)
        ```
    """
    return DatabaseContext(engine, max_retries, base_delay)
