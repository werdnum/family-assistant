"""SQLAlchemy database logging handler."""

import logging
import traceback
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import insert


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

    def emit(self, record: logging.LogRecord) -> None:
        """Write log record to database."""
        if record.levelno < self.min_level:
            return

        # Circuit breaker
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            return

        # We need to handle async operations in a sync context
        # This is a limitation of Python's logging system
        # In production, consider using a queue for async logging
        import asyncio

        try:
            # Get or create event loop
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context, create a task
                loop.create_task(self._async_emit(record))
            except RuntimeError:
                # No running loop, we need to run it synchronously
                # This is not ideal but necessary for compatibility
                asyncio.run(self._async_emit(record))
        except Exception:
            self.consecutive_failures += 1
            self.handleError(record)  # Fallback to stderr

    async def _async_emit(self, record: logging.LogRecord) -> None:
        """Async helper to write log record to database."""
        from family_assistant.storage.error_logs import error_logs_table

        try:
            async with self.session_factory() as db_context:
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


def setup_error_logging(session_factory: Callable) -> None:
    """Add database error handler to root logger."""
    handler = SQLAlchemyErrorHandler(session_factory)
    handler.setLevel(logging.ERROR)

    # Add formatter to match existing format
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    logging.getLogger().addHandler(handler)
