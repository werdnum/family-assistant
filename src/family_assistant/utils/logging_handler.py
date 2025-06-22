"""SQLAlchemy database logging handler."""

import asyncio
import logging
import traceback
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine


class SQLAlchemyErrorHandler(logging.Handler):
    """Async handler that writes ERROR and above to database."""

    def __init__(
        self,
        engine: AsyncEngine,
        min_level: int = logging.ERROR,
    ) -> None:
        super().__init__()
        self.engine = engine
        self.min_level = min_level
        self.consecutive_failures = 0
        self.circuit_breaker_threshold = 5
        self._pending_tasks: set[asyncio.Task] = set()

    def emit(self, record: logging.LogRecord) -> None:
        """Write log record to database asynchronously."""
        if record.levelno < self.min_level:
            return

        # Circuit breaker
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            return

        try:
            # Get current event loop
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running - can't log to database
            # This might happen during shutdown or in sync contexts
            return

        # Create task and track it
        task = loop.create_task(self._async_emit(record))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _async_emit(self, record: logging.LogRecord) -> None:
        """Write log record to database."""
        from family_assistant.storage.context import DatabaseContext
        from family_assistant.storage.error_logs import error_logs_table

        try:
            async with DatabaseContext(engine=self.engine) as db_context:
                error_log = self._create_error_log_dict(record)
                stmt = insert(error_logs_table).values(**error_log)
                await db_context.execute_with_retry(stmt)
                self.consecutive_failures = 0  # Reset on success
        except Exception as e:
            self.consecutive_failures += 1
            # Log to stderr as fallback
            import sys

            print(f"Failed to log error to database: {e}", file=sys.stderr)

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

    async def wait_for_pending_logs(self, timeout: float = 5.0) -> None:
        """Wait for all pending log writes to complete."""
        if not self._pending_tasks:
            return

        # Wait for all tasks with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending_tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Cancel remaining tasks
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()

    def close(self) -> None:
        """Close handler and cancel pending tasks."""
        # Cancel all pending tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        super().close()


def setup_error_logging(engine: AsyncEngine) -> SQLAlchemyErrorHandler:
    """Add database error handler to root logger."""
    handler = SQLAlchemyErrorHandler(engine)
    handler.setLevel(logging.ERROR)

    # Add formatter to match existing format
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    logging.getLogger().addHandler(handler)
    return handler
