"""
Database context manager for storage operations.

This module provides a context manager and utilities for database operations,
enabling dependency injection for testing and centralizing retry logic.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import TextClause, event  # Result removed
from sqlalchemy.engine import CursorResult  # CursorResult added
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Delete, Insert, Select, Update

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from family_assistant.storage.repositories import (
        EmailRepository,
        ErrorLogsRepository,
        EventsRepository,
        MessageHistoryRepository,
        NotesRepository,
        TasksRepository,
    )


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
        engine: AsyncEngine | None = None,
        max_retries: int = 3,
        base_delay: float = 0.5,
    ) -> None:
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
        self.conn: AsyncConnection | None = None
        self._transaction_cm: AbstractAsyncContextManager[AsyncConnection] | None = None

        # Repository instances (lazy-loaded)
        self._notes = None
        self._tasks = None
        self._message_history = None
        self._email = None
        self._error_logs = None
        self._events = None
        self._vector = None

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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
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
        query: Select | Insert | Update | Delete | TextClause,
        params: dict[str, Any] | None = None,
    ) -> CursorResult:
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
                        "Max retries exceeded for retryable error. Raising error."
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
        self, query: Select | TextClause, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Execute a query and fetch all results as dictionaries.

        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.

        Returns:
            A list of dictionaries representing the rows.
        """
        result = await self.execute_with_retry(query, params)
        # Convert RowMapping objects to dicts
        return [dict(row_mapping) for row_mapping in result.mappings().all()]

    async def fetch_one(
        self, query: Select | TextClause, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """
        Execute a query and fetch one result as a dictionary.

        Args:
            query: The SQLAlchemy SELECT query to execute.
            params: Optional parameters for the query.

        Returns:
            A dictionary representing the row, or None if no results.
        """
        result = await self.execute_with_retry(query, params)
        row_mapping = result.mappings().one_or_none()
        return dict(row_mapping) if row_mapping else None

    def on_commit(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """
        Register a callback to be called on transaction commit.

        Args:
            callback: A callable to be executed on commit.

        Returns:
            The original callback for chaining.
        """
        if (
            self.conn is None or not self.conn.in_transaction()
        ):  # Check for active transaction
            raise RuntimeError(
                "on_commit called without an active database connection or outside of a transaction context."
            )

        # Wrapper to call the original callback without arguments
        def event_listener_wrapper(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
            callback()

        # Register the wrapper with the transaction context manager
        event.listen(self.conn.sync_connection, "commit", event_listener_wrapper)
        return callback

    @property
    def notes(self) -> "NotesRepository":
        """Get the notes repository instance."""
        if self._notes is None:
            from family_assistant.storage.repositories import NotesRepository

            self._notes = NotesRepository(self)
        return self._notes

    @property
    def tasks(self) -> "TasksRepository":
        """Get the tasks repository instance."""
        if self._tasks is None:
            from family_assistant.storage.repositories import TasksRepository

            self._tasks = TasksRepository(self)
        return self._tasks

    @property
    def message_history(self) -> "MessageHistoryRepository":
        """Get the message history repository instance."""
        if self._message_history is None:
            from family_assistant.storage.repositories import MessageHistoryRepository

            self._message_history = MessageHistoryRepository(self)
        return self._message_history

    @property
    def email(self) -> "EmailRepository":
        """Get the email repository instance."""
        if self._email is None:
            from family_assistant.storage.repositories import EmailRepository

            self._email = EmailRepository(self)
        return self._email

    @property
    def error_logs(self) -> "ErrorLogsRepository":
        """Get the error logs repository instance."""
        if self._error_logs is None:
            from family_assistant.storage.repositories import ErrorLogsRepository

            self._error_logs = ErrorLogsRepository(self)
        return self._error_logs

    @property
    def events(self) -> "EventsRepository":
        """Get the events repository instance."""
        if self._events is None:
            from family_assistant.storage.repositories import EventsRepository

            self._events = EventsRepository(self)
        return self._events

    @property
    def vector(self) -> Any:  # Type hint as Any to avoid circular import
        """Get the vector repository instance."""
        if self._vector is None:
            from family_assistant.storage.repositories import VectorRepository

            self._vector = VectorRepository(self)
        return self._vector

    async def init_vector_db(self) -> None:
        """Initialize vector database components."""
        await self.vector.init_db()


# Convenience function to create a database context
# This function is now less useful as DatabaseContext manages its own transaction
# via __aenter__/__aexit__. Callers should instantiate DatabaseContext directly.
# Keeping it for now but marking as potentially deprecated or for removal.
def get_db_context(
    engine: AsyncEngine | None = None, max_retries: int = 3, base_delay: float = 0.5
) -> DatabaseContext:
    """
    Creates an instance of DatabaseContext.

    This function instantiates and returns a DatabaseContext object,
    which is an asynchronous context manager.

    Args:
        engine: Optional SQLAlchemy AsyncEngine for dependency injection.
        max_retries: Maximum number of retries for database operations.
        base_delay: Base delay in seconds for exponential backoff.

    Returns:
        A DatabaseContext instance.

    Example:
        ```python
        db_context_instance = get_db_context()
        async with db_context_instance as db:
            result = await db.fetch_all(...)
        ```
    """
    return DatabaseContext(engine, max_retries, base_delay)
