"""Repository for error logs storage operations."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.sql import functions as func

from family_assistant.storage.error_logs import error_logs_table
from family_assistant.storage.repositories.base import BaseRepository


class ErrorLogsRepository(BaseRepository):
    """Repository for managing error logs in the database."""

    async def get_logs(
        self,
        *,
        level: str | None = None,
        logger_name: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get error logs with filtering and pagination.

        Args:
            level: Filter by log level (e.g., 'ERROR', 'WARNING')
            logger_name: Filter by logger name (partial match)
            since: Only return logs after this timestamp
            limit: Maximum number of logs to return
            offset: Number of logs to skip for pagination

        Returns:
            List of error log dictionaries
        """
        query = select(error_logs_table)

        if level:
            query = query.where(error_logs_table.c.level == level)
        if logger_name:
            query = query.where(error_logs_table.c.logger_name.contains(logger_name))
        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        query = query.order_by(error_logs_table.c.timestamp.desc())
        query = query.offset(offset).limit(limit)

        rows = await self._db.fetch_all(query)
        return [dict(row) for row in rows]

    async def get_by_id(self, error_id: int) -> dict[str, Any] | None:
        """Get a specific error log by ID.

        Args:
            error_id: The error log ID

        Returns:
            Error log data or None if not found
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
        """Count error logs matching criteria.

        Args:
            level: Filter by log level
            logger_name: Filter by logger name (partial match)
            since: Only count logs after this timestamp

        Returns:
            Count of matching error logs
        """
        query = select(func.count(error_logs_table.c.id).label("count"))

        if level:
            query = query.where(error_logs_table.c.level == level)
        if logger_name:
            query = query.where(error_logs_table.c.logger_name.contains(logger_name))
        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        row = await self._db.fetch_one(query)
        return row["count"] if row else 0

    async def cleanup_old(self, retention_days: int = 30) -> int:
        """Delete error logs older than the retention period.

        Args:
            retention_days: Number of days to keep error logs

        Returns:
            Number of deleted logs
        """
        cutoff_date = datetime.now() - timedelta(days=retention_days)

        stmt = delete(error_logs_table).where(
            error_logs_table.c.timestamp < cutoff_date
        )

        result = await self._db.execute_with_retry(stmt)
        deleted_count = result.rowcount  # type: ignore[attr-defined]

        if deleted_count > 0:
            self._logger.info(
                f"Cleaned up {deleted_count} error logs older than {retention_days} days"
            )

        return deleted_count

    async def get_by_module(
        self,
        module: str,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get error logs from a specific module.

        Args:
            module: Module name to filter by
            limit: Maximum number of logs to return
            since: Only return logs after this timestamp

        Returns:
            List of error logs from the module
        """
        query = select(error_logs_table).where(error_logs_table.c.module == module)

        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        query = query.order_by(error_logs_table.c.timestamp.desc()).limit(limit)

        rows = await self._db.fetch_all(query)
        return [dict(row) for row in rows]

    async def get_by_exception_type(
        self,
        exception_type: str,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Get error logs by exception type.

        Args:
            exception_type: Exception type to filter by
            limit: Maximum number of logs to return
            since: Only return logs after this timestamp

        Returns:
            List of error logs with the specified exception type
        """
        query = select(error_logs_table).where(
            error_logs_table.c.exception_type == exception_type
        )

        if since:
            query = query.where(error_logs_table.c.timestamp >= since)

        query = query.order_by(error_logs_table.c.timestamp.desc()).limit(limit)

        rows = await self._db.fetch_all(query)
        return [dict(row) for row in rows]

    async def get_summary(self, since: datetime | None = None) -> dict[str, Any]:
        """Get a summary of error logs.

        Args:
            since: Only consider logs after this timestamp

        Returns:
            Dictionary with summary statistics
        """
        # Base query with optional time filter
        base_query = select(error_logs_table)
        if since:
            base_query = base_query.where(error_logs_table.c.timestamp >= since)

        # Count by level
        level_counts = {}
        for level in ["ERROR", "WARNING", "CRITICAL"]:
            query = select(func.count(error_logs_table.c.id).label("count")).where(
                error_logs_table.c.level == level
            )
            if since:
                query = query.where(error_logs_table.c.timestamp >= since)
            row = await self._db.fetch_one(query)
            level_counts[level] = row["count"] if row else 0

        # Get top exception types
        exception_query = (
            select(
                error_logs_table.c.exception_type,
                func.count(error_logs_table.c.id).label("count"),
            )
            .where(error_logs_table.c.exception_type.isnot(None))
            .group_by(error_logs_table.c.exception_type)
            .order_by(func.count(error_logs_table.c.id).desc())
            .limit(5)
        )
        if since:
            exception_query = exception_query.where(
                error_logs_table.c.timestamp >= since
            )

        exception_rows = await self._db.fetch_all(exception_query)
        top_exceptions = [
            {"type": row["exception_type"], "count": row["count"]}
            for row in exception_rows
        ]

        # Get top modules with errors
        module_query = (
            select(
                error_logs_table.c.module,
                func.count(error_logs_table.c.id).label("count"),
            )
            .where(error_logs_table.c.module.isnot(None))
            .group_by(error_logs_table.c.module)
            .order_by(func.count(error_logs_table.c.id).desc())
            .limit(5)
        )
        if since:
            module_query = module_query.where(error_logs_table.c.timestamp >= since)

        module_rows = await self._db.fetch_all(module_query)
        top_modules = [
            {"module": row["module"], "count": row["count"]} for row in module_rows
        ]

        # Total count
        total_query = select(func.count(error_logs_table.c.id).label("count"))
        if since:
            total_query = total_query.where(error_logs_table.c.timestamp >= since)
        total_row = await self._db.fetch_one(total_query)
        total_count = total_row["count"] if total_row else 0

        return {
            "total_count": total_count,
            "level_counts": level_counts,
            "top_exceptions": top_exceptions,
            "top_modules": top_modules,
            "since": since.isoformat() if since else None,
        }
