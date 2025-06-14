"""Repository for managing error logs storage."""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.sql import functions as func

from family_assistant.storage.error_logs import error_logs_table

from .base import BaseRepository

logger = logging.getLogger(__name__)


class ErrorLogsRepository(BaseRepository):
    """Repository for managing error logs."""

    async def get_all(
        self,
        *,
        level: str | None = None,
        logger_name: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Retrieve error logs with optional filtering.

        Args:
            level: Filter by log level (e.g., 'ERROR', 'WARNING')
            logger_name: Filter by logger name
            since: Only return logs after this timestamp
            limit: Maximum number of logs to return
            offset: Number of logs to skip

        Returns:
            List of error log dictionaries
        """
        query = select(error_logs_table).order_by(error_logs_table.c.timestamp.desc())

        # Add filters
        if level:
            query = query.where(error_logs_table.c.level == level.upper())
        if logger_name:
            query = query.where(error_logs_table.c.logger_name == logger_name)
        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        # Add pagination
        query = query.limit(limit).offset(offset)

        rows = await self._db.fetch_all(query)
        return [dict(row) for row in rows]

    async def get_by_id(self, error_id: int) -> dict[str, Any] | None:
        """
        Get a specific error log by ID.

        Args:
            error_id: The error log ID

        Returns:
            Error log dictionary or None if not found
        """
        query = select(error_logs_table).where(error_logs_table.c.id == error_id)
        row = await self._db.fetch_one(query)
        return dict(row) if row else None

    async def count(
        self,
        *,
        level: str | None = None,
        logger_name: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """
        Count error logs with optional filtering.

        Args:
            level: Filter by log level
            logger_name: Filter by logger name
            since: Only count logs after this timestamp

        Returns:
            Number of matching error logs
        """
        query = select(func.count(error_logs_table.c.id).label("count"))

        # Add filters
        if level:
            query = query.where(error_logs_table.c.level == level.upper())
        if logger_name:
            query = query.where(error_logs_table.c.logger_name == logger_name)
        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        row = await self._db.fetch_one(query)
        return row["count"] if row else 0

    async def add(
        self,
        *,
        logger_name: str,
        level: str,
        message: str,
        exception_type: str | None = None,
        exception_message: str | None = None,
        traceback: str | None = None,
        module: str | None = None,
        function_name: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> int:
        """
        Add a new error log.

        Args:
            logger_name: Name of the logger
            level: Log level (e.g., 'ERROR', 'WARNING')
            message: Log message
            exception_type: Type of exception if applicable
            exception_message: Exception message if applicable
            traceback: Stack trace if applicable
            module: Module where error occurred
            function_name: Function where error occurred
            extra_data: Additional metadata

        Returns:
            ID of the created error log
        """
        stmt = error_logs_table.insert().values(
            logger_name=logger_name,
            level=level.upper(),
            message=message,
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=traceback,
            module=module,
            function_name=function_name,
            extra_data=extra_data,
            timestamp=func.now(),
        )

        result = await self._db.execute_with_retry(stmt)
        return result.lastrowid  # type: ignore[attr-defined]

    async def delete_old(self, older_than: datetime) -> int:
        """
        Delete error logs older than the specified timestamp.

        Args:
            older_than: Delete logs before this timestamp

        Returns:
            Number of deleted logs
        """
        stmt = error_logs_table.delete().where(
            error_logs_table.c.timestamp < older_than
        )
        result = await self._db.execute_with_retry(stmt)
        return result.rowcount  # type: ignore[attr-defined]
