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
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from sqlalchemy import TextClause, event
from sqlalchemy.engine import CursorResult  # CursorResult added
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql import Delete, Insert, Select, Update

# PostgreSQL SQLSTATE codes - the authoritative way to identify error types
# See: https://www.postgresql.org/docs/current/errcodes-appendix.html
PGCODE_IN_FAILED_SQL_TRANSACTION = "25P02"  # Transaction is aborted
PGCODE_CHARACTER_NOT_IN_REPERTOIRE = "22021"  # Invalid byte sequence for encoding
# Retryable codes (for reference, not currently used for auto-retry)
PGCODE_SERIALIZATION_FAILURE = "40001"  # Concurrent transaction conflict
PGCODE_DEADLOCK_DETECTED = "40P01"  # Two processes blocked each other

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from family_assistant.storage.repositories import (
        AutomationsRepository,
        EmailRepository,
        ErrorLogsRepository,
        EventsRepository,
        MessageHistoryRepository,
        NotesRepository,
        PushSubscriptionRepository,
        ScheduleAutomationsRepository,
        TasksRepository,
        VectorRepository,
        WorkerTasksRepository,
    )
    from family_assistant.storage.repositories.a2a_tasks import A2ATasksRepository
    from family_assistant.web.message_notifier import MessageNotifier


# Use absolute package path

logger = logging.getLogger(__name__)

# Type variable for query result type
T = TypeVar("T")


def sanitize_text_for_postgres(text: str | None) -> str | None:
    """
    Sanitize text content for storage in PostgreSQL TEXT columns.

    PostgreSQL TEXT columns don't allow null bytes (\\x00) which can appear in:
    - Browser console output from Playwright
    - Binary data accidentally treated as text
    - External API responses with embedded null bytes

    This function:
    1. Removes null bytes (PostgreSQL doesn't allow them in TEXT)
    2. Handles invalid UTF-8 surrogate characters by replacing them
    3. Preserves valid control characters (tabs, newlines, ANSI escapes)

    Args:
        text: The text to sanitize, or None

    Returns:
        Sanitized text safe for PostgreSQL, or None if input was None
    """
    if text is None:
        return None

    # Remove null bytes - PostgreSQL doesn't allow them in TEXT columns
    text = text.replace("\x00", "")

    # Handle potential surrogate characters or other encoding issues
    # by round-tripping through UTF-8 with error replacement
    # This catches lone surrogates (U+D800-U+DFFF) and replaces them
    try:
        text = text.encode("utf-8", errors="surrogatepass").decode(
            "utf-8", errors="replace"
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        # If encoding fails completely, replace all problematic chars
        text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")

    return text


def _is_non_retryable_postgres_error(
    exc: BaseException | None,
) -> tuple[bool, Literal["transaction_aborted", "encoding_error", ""]]:
    """
    Check if an exception is a non-retryable PostgreSQL error using SQLSTATE codes.

    Some PostgreSQL errors should not be retried because:
    - They're data errors (bad encoding, constraint violations)
    - The transaction is already aborted

    Uses pgcode (SQLSTATE) which is the authoritative way to identify PostgreSQL
    error types, rather than isinstance checks which can fail with SQLAlchemy's
    exception wrapping.

    Returns:
        Tuple of (is_non_retryable, error_type_description)
    """
    if exc is None:
        return False, ""

    # asyncpg exceptions have a 'pgcode' attribute with the SQLSTATE code
    # This is the gold standard for identifying PostgreSQL error types
    pgcode = getattr(exc, "pgcode", None)
    if pgcode is None:
        return False, ""

    # Check for specific non-retryable SQLSTATE codes
    if pgcode == PGCODE_IN_FAILED_SQL_TRANSACTION:
        return True, "transaction_aborted"
    if pgcode == PGCODE_CHARACTER_NOT_IN_REPERTOIRE:
        return True, "encoding_error"

    return False, ""


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
        message_notifier: "MessageNotifier | None" = None,
    ) -> None:
        """
        Initialize the database context.

        Args:
            engine: Optional SQLAlchemy AsyncEngine. If not provided, the default engine from
                   storage.base will be used. This enables dependency injection for testing.
            max_retries: Maximum number of retries for database operations.
            base_delay: Base delay in seconds for exponential backoff.
            message_notifier: Optional MessageNotifier instance for live message updates.
        """
        if engine is None:
            raise ValueError("DatabaseContext requires an engine to be provided")
        self.engine = engine
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.message_notifier = message_notifier
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
        self._schedule_automations = None
        self._automations = None
        self._push_subscriptions = None
        self._worker_tasks = None
        self._a2a_tasks = None

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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
                # or IntegrityError (constraint violations). These should not be retried.
                if isinstance(e.orig, ProgrammingError | IntegrityError) or isinstance(
                    e, ProgrammingError | IntegrityError
                ):  # Check original and wrapper
                    is_prog_error = isinstance(e.orig, ProgrammingError) or isinstance(
                        e, ProgrammingError
                    )
                    is_integ_error = isinstance(e.orig, IntegrityError) or isinstance(
                        e, IntegrityError
                    )

                    if is_prog_error:
                        error_type = "ProgrammingError"
                    elif is_integ_error:
                        error_type = "IntegrityError"
                    else:
                        error_type = "UnknownError"  # Should not happen given the outer condition

                    logger.error(
                        f"Non-retryable {error_type} encountered: {e}",
                        exc_info=True,
                    )
                    raise  # Re-raise immediately, do not retry

                # Check for PostgreSQL-specific non-retryable errors
                # (transaction aborted, encoding errors, etc.)
                is_non_retryable, error_type = _is_non_retryable_postgres_error(e.orig)
                if is_non_retryable:
                    if error_type == "transaction_aborted":
                        logger.error(
                            "PostgreSQL transaction is aborted. Cannot retry within same transaction.",
                            exc_info=True,
                        )
                    elif error_type == "encoding_error":
                        logger.error(
                            f"Non-retryable encoding error (invalid characters in data): {e}",
                            exc_info=True,
                        )
                    else:
                        logger.error(
                            f"Non-retryable PostgreSQL error ({error_type}): {e}",
                            exc_info=True,
                        )
                    raise  # Re-raise immediately, retrying won't help

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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        query: Select | TextClause,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        params: dict[str, Any] | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        self,
        query: Select | TextClause,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
        params: dict[str, Any] | None = None,
        # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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
        def event_listener_wrapper(*args: object, **kwargs: object) -> None:  # noqa: ARG001
            callback()

        # Register the wrapper with the transaction context manager
        event.listen(self.conn.sync_connection, "commit", event_listener_wrapper)
        return callback

    @property
    def notes(self) -> "NotesRepository":
        """Get the notes repository instance."""
        if self._notes is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                NotesRepository,
            )

            self._notes = NotesRepository(self)
        return self._notes

    @property
    def tasks(self) -> "TasksRepository":
        """Get the tasks repository instance."""
        if self._tasks is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                TasksRepository,
            )

            self._tasks = TasksRepository(self)
        return self._tasks

    @property
    def message_history(self) -> "MessageHistoryRepository":
        """Get the message history repository instance."""
        if self._message_history is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                MessageHistoryRepository,
            )

            self._message_history = MessageHistoryRepository(self)
        return self._message_history

    @property
    def email(self) -> "EmailRepository":
        """Get the email repository instance."""
        if self._email is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                EmailRepository,
            )

            self._email = EmailRepository(self)
        return self._email

    @property
    def error_logs(self) -> "ErrorLogsRepository":
        """Get the error logs repository instance."""
        if self._error_logs is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                ErrorLogsRepository,
            )

            self._error_logs = ErrorLogsRepository(self)
        return self._error_logs

    @property
    def events(self) -> "EventsRepository":
        """Get the events repository instance."""
        if self._events is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                EventsRepository,
            )

            self._events = EventsRepository(self)
        return self._events

    @property
    def vector(self) -> "VectorRepository":
        """Get the vector repository instance."""
        if self._vector is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                VectorRepository,
            )

            self._vector = VectorRepository(self)
        return self._vector

    @property
    def schedule_automations(self) -> "ScheduleAutomationsRepository":
        """Get the schedule automations repository instance."""
        if self._schedule_automations is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                ScheduleAutomationsRepository,
            )

            self._schedule_automations = ScheduleAutomationsRepository(self)
        return self._schedule_automations

    @property
    def automations(self) -> "AutomationsRepository":
        """Get the unified automations repository instance."""
        if self._automations is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                AutomationsRepository,
            )

            self._automations = AutomationsRepository(self)
        return self._automations

    @property
    def push_subscriptions(self) -> "PushSubscriptionRepository":
        """Get the push subscriptions repository instance."""
        if self._push_subscriptions is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                PushSubscriptionRepository,
            )

            self._push_subscriptions = PushSubscriptionRepository(self)
        return self._push_subscriptions

    @property
    def worker_tasks(self) -> "WorkerTasksRepository":
        """Get the worker tasks repository instance."""
        if self._worker_tasks is None:
            from family_assistant.storage.repositories import (  # noqa: PLC0415
                WorkerTasksRepository,
            )

            self._worker_tasks = WorkerTasksRepository(self)
        return self._worker_tasks

    @property
    def a2a_tasks(self) -> "A2ATasksRepository":
        """Get the A2A tasks repository instance."""
        if self._a2a_tasks is None:
            from family_assistant.storage.repositories.a2a_tasks import (  # noqa: PLC0415
                A2ATasksRepository,
            )

            self._a2a_tasks = A2ATasksRepository(self)
        return self._a2a_tasks

    async def init_vector_db(self) -> None:
        """Initialize vector database components."""
        await self.vector.init_db()


# Convenience function to create a database context
# This function is now less useful as DatabaseContext manages its own transaction
# via __aenter__/__aexit__. Callers should instantiate DatabaseContext directly.
# Keeping it for now but marking as potentially deprecated or for removal.
def get_db_context(
    engine: AsyncEngine,
    max_retries: int = 3,
    base_delay: float = 0.5,
    message_notifier: "MessageNotifier | None" = None,
) -> DatabaseContext:
    """
    Creates an instance of DatabaseContext.

    This function instantiates and returns a DatabaseContext object,
    which is an asynchronous context manager.

    Args:
        engine: Required SQLAlchemy AsyncEngine for dependency injection.
        max_retries: Maximum number of retries for database operations.
        base_delay: Base delay in seconds for exponential backoff.
        message_notifier: Optional MessageNotifier instance for live message updates.

    Returns:
        A DatabaseContext instance.

    Example:
        ```python
        db_context_instance = get_db_context()
        async with db_context_instance as db:
            result = await db.fetch_all(...)
        ```
    """
    return DatabaseContext(engine, max_retries, base_delay, message_notifier)
