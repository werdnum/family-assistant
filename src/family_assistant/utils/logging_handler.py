"""SQLAlchemy database logging handler."""

import asyncio
import logging
import queue
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class SQLAlchemyErrorHandler(logging.Handler):
    """Handler that writes ERROR and above to database."""

    def __init__(
        self,
        session_factory: Callable,
        min_level: int = logging.ERROR,
    ) -> None:
        super().__init__()
        self.session_factory = session_factory
        self.min_level = min_level
        self.consecutive_failures = 0
        self.circuit_breaker_threshold = 5

        # Use a thread-safe queue to handle cross-thread/loop communication
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._shutdown = False
        self._worker_thread = None
        self._worker_loop = None
        self._worker_engine: AsyncEngine | None = None

        # Start the worker thread
        self._start_worker()

    def _start_worker(self) -> None:
        """Start the background worker thread for database operations."""
        self._worker_thread = threading.Thread(target=self._worker_run, daemon=True)
        self._worker_thread.start()

    def _worker_run(self) -> None:
        """Run the async event loop in the worker thread."""
        # Create a new event loop for this thread
        self._worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._worker_loop)

        try:
            # Create a new engine specifically for this thread/loop
            import os

            from family_assistant.storage.base import (
                create_engine_with_sqlite_optimizations,
            )

            database_url = os.getenv(
                "DATABASE_URL", "sqlite+aiosqlite:///family_assistant.db"
            )
            self._worker_engine = create_engine_with_sqlite_optimizations(database_url)

            self._worker_loop.run_until_complete(self._worker_async())
        finally:
            # Clean up the engine
            if self._worker_engine:
                self._worker_loop.run_until_complete(self._worker_engine.dispose())
            self._worker_loop.close()

    async def _worker_async(self) -> None:
        """Process log records from the queue."""
        while not self._shutdown:
            try:
                # Check for records with timeout
                record = await asyncio.get_event_loop().run_in_executor(
                    None, self._queue.get, True, 0.1
                )
                await self._async_emit(record)
            except queue.Empty:
                continue
            except Exception as e:
                # Log worker errors to stderr
                import sys

                print(f"Error in logging worker: {e}", file=sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        """Write log record to database."""
        if record.levelno < self.min_level:
            return

        # Circuit breaker
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            return

        try:
            # Put the record in the queue for the worker thread to process
            self._queue.put_nowait(record)
        except queue.Full:
            self.consecutive_failures += 1
            self.handleError(record)  # Fallback to stderr

    async def _async_emit(self, record: logging.LogRecord) -> None:
        """Async helper to write log record to database."""
        from family_assistant.storage.error_logs import error_logs_table

        try:
            # First try to use the session_factory
            db_context_instance = self.session_factory()

            # Check if this is a DatabaseContext instance with an engine already set
            if hasattr(db_context_instance, "engine") and db_context_instance.engine:
                # Use the provided context as-is
                async with db_context_instance as db_context:
                    error_log = self._create_error_log_dict(record)
                    stmt = insert(error_logs_table).values(**error_log)
                    await db_context.execute_with_retry(stmt)
                    self.consecutive_failures = 0  # Reset on success
            else:
                # Fall back to using the worker engine
                from family_assistant.storage.context import DatabaseContext

                if not self._worker_engine:
                    raise RuntimeError("Worker engine not initialized")

                async with DatabaseContext(engine=self._worker_engine) as db_context:
                    error_log = self._create_error_log_dict(record)
                    stmt = insert(error_logs_table).values(**error_log)
                    await db_context.execute_with_retry(stmt)
                    self.consecutive_failures = 0  # Reset on success
        except Exception:
            self.consecutive_failures += 1
            raise

    def _create_error_log_dict(self, record: logging.LogRecord) -> dict:
        """Create error log dictionary from LogRecord."""
        exc_info = record.exc_info
        exception_type = None
        exception_message = None
        tb_text = None

        if exc_info:
            exception_type = exc_info[0].__name__ if exc_info[0] else None
            exception_message = str(exc_info[1]) if exc_info[1] else None
            tb_text = "".join(traceback.format_exception(*exc_info))

        return {
            "timestamp": datetime.fromtimestamp(record.created),
            "logger_name": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "exception_type": exception_type,
            "exception_message": exception_message,
            "traceback": tb_text,
            "module": record.module,
            "function_name": record.funcName,
        }

    def flush(self) -> None:
        """Flush any pending log records."""
        # Wait for queue to be empty
        while not self._queue.empty():
            import time

            time.sleep(0.1)

    def close(self) -> None:
        """Clean up the handler and stop the worker thread."""
        # Flush any pending records first
        self.flush()

        # Signal shutdown
        self._shutdown = True

        # Wait for worker thread to finish (with timeout)
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        # Call parent close
        super().close()


def setup_error_logging(session_factory: Callable) -> SQLAlchemyErrorHandler:
    """Add database error handler to root logger."""
    handler = SQLAlchemyErrorHandler(session_factory)
    handler.setLevel(logging.ERROR)

    # Add formatter to match existing format
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    logging.getLogger().addHandler(handler)
    return handler
