"""Error log storage models and queries."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    delete,
    select,
)
from sqlalchemy.sql import functions as func

from family_assistant.storage.base import metadata
from family_assistant.storage.database import DatabaseExecutor

# Define the error_logs table
error_logs_table = Table(
    "error_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "timestamp",
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        index=True,
    ),
    Column("logger_name", String(255), nullable=False, index=True),
    Column("level", String(50), nullable=False, index=True),
    Column("message", Text, nullable=False),
    # Error details
    Column("exception_type", String(255)),
    Column("exception_message", Text),
    Column("traceback", Text),
    # Context
    Column("module", String(255), index=True),
    Column("function_name", String(255)),
    # Additional metadata
    Column("extra_data", JSON),
)


async def get_error_logs(
    db_context: DatabaseExecutor,
    *,
    level: str | None = None,
    logger_name: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    # ast-grep-ignore: no-dict-any - database rows have dynamic columns from query
) -> list[dict[str, Any]]:
    """Get error logs with filtering and pagination."""
    query = select(error_logs_table)

    if level:
        query = query.where(error_logs_table.c.level == level)
    if logger_name:
        query = query.where(error_logs_table.c.logger_name.contains(logger_name))
    if since:
        query = query.where(error_logs_table.c.timestamp >= since)

    query = query.order_by(error_logs_table.c.timestamp.desc())
    query = query.offset(offset).limit(limit)

    rows = await db_context.fetch_all(query)
    return [dict(row) for row in rows]


async def get_error_log_by_id(
    db_context: DatabaseExecutor,
    error_id: int,
    # ast-grep-ignore: no-dict-any - database row has dynamic columns from query
) -> dict[str, Any] | None:
    """Get a specific error log by ID."""
    query = select(error_logs_table).where(error_logs_table.c.id == error_id)
    row = await db_context.fetch_one(query)
    return dict(row) if row else None


async def count_error_logs(
    db_context: DatabaseExecutor,
    *,
    level: str | None = None,
    logger_name: str | None = None,
    since: datetime | None = None,
) -> int:
    """Count error logs matching criteria."""
    query = select(func.count(error_logs_table.c.id).label("count"))

    if level:
        query = query.where(error_logs_table.c.level == level)
    if logger_name:
        query = query.where(error_logs_table.c.logger_name.contains(logger_name))
    if since:
        query = query.where(error_logs_table.c.timestamp >= since)

    row = await db_context.fetch_one(query)
    return row["count"] if row else 0


async def cleanup_old_error_logs(
    db_context: DatabaseExecutor, retention_days: int = 30
) -> int:
    """Remove error logs older than retention period. Returns number of deleted rows."""
    cutoff_date = datetime.now(UTC) - timedelta(days=retention_days)

    stmt = delete(error_logs_table).where(error_logs_table.c.timestamp < cutoff_date)
    result = await db_context.execute(stmt)

    return result.rowcount
