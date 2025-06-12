"""Integration test for error logging functionality."""

import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.storage import error_logs_table
from family_assistant.storage.context import DatabaseContext
from family_assistant.storage.error_logs import (
    cleanup_old_error_logs,
    get_error_logs,
)
from family_assistant.utils.logging_handler import SQLAlchemyErrorHandler


@pytest.mark.asyncio
async def test_error_logging_integration(test_db_engine: AsyncEngine) -> None:
    """Test that errors are logged to the database correctly."""
    # Create a test logger with our handler
    test_logger = logging.getLogger("test_error_logger")
    test_logger.setLevel(logging.DEBUG)

    # Add our SQLAlchemy handler
    from family_assistant.storage.context import get_db_context

    handler = SQLAlchemyErrorHandler(get_db_context, min_level=logging.ERROR)
    test_logger.addHandler(handler)

    try:
        # Test 1: Log a simple error
        test_logger.error("Test error message")

        # Test 2: Log an error with exception
        try:
            _ = 1 / 0
        except ZeroDivisionError:
            test_logger.exception("Division by zero occurred")

        # Test 3: Log a critical error
        test_logger.critical("Critical system failure")

        # Test 4: Log a warning (should not be stored as min_level is ERROR)
        test_logger.warning("This is just a warning")

        # Give async logging handler time to write to database
        import asyncio

        await asyncio.sleep(0.5)

        # Check the database
        async with DatabaseContext(engine=test_db_engine) as db_context:
            # Count total error logs
            query = select(error_logs_table).order_by(error_logs_table.c.timestamp)
            error_logs = await db_context.fetch_all(query)

            # Should have 3 error logs (2 ERROR, 1 CRITICAL)
            assert len(error_logs) == 3

            # Check first error (simple error)
            first_error = error_logs[0]
            assert first_error["level"] == "ERROR"
            assert first_error["message"] == "Test error message"
            assert first_error["logger_name"] == "test_error_logger"
            assert first_error["exception_type"] is None
            assert first_error["traceback"] is None

            # Check second error (with exception)
            second_error = error_logs[1]
            assert second_error["level"] == "ERROR"
            assert "Division by zero occurred" in second_error["message"]
            assert second_error["exception_type"] == "ZeroDivisionError"
            assert second_error["exception_message"] == "division by zero"
            assert "ZeroDivisionError: division by zero" in second_error["traceback"]
            assert "1 / 0" in second_error["traceback"]

            # Check third error (critical)
            third_error = error_logs[2]
            assert third_error["level"] == "CRITICAL"
            assert third_error["message"] == "Critical system failure"

            # Test the get_error_logs function
            errors = await get_error_logs(db_context, level="ERROR")
            assert len(errors) == 2  # Only ERROR level, not CRITICAL

            errors = await get_error_logs(db_context, level="CRITICAL")
            assert len(errors) == 1

            # Test filtering by logger name
            errors = await get_error_logs(db_context, logger_name="test_error_logger")
            assert len(errors) == 3

            errors = await get_error_logs(db_context, logger_name="nonexistent")
            assert len(errors) == 0

    finally:
        # Clean up the handler
        test_logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_error_log_cleanup(test_db_engine: AsyncEngine) -> None:
    """Test that old error logs are cleaned up correctly."""
    async with DatabaseContext(engine=test_db_engine) as db_context:
        from sqlalchemy import insert

        # Insert error logs with different ages
        now = datetime.now()

        # Old error (40 days ago)
        stmt1 = insert(error_logs_table).values(
            timestamp=now - timedelta(days=40),
            logger_name="old.error",
            level="ERROR",
            message="Old error message",
        )
        await db_context.execute_with_retry(stmt1)

        # Recent error (10 days ago)
        stmt2 = insert(error_logs_table).values(
            timestamp=now - timedelta(days=10),
            logger_name="recent.error",
            level="ERROR",
            message="Recent error message",
        )
        await db_context.execute_with_retry(stmt2)

        # Very recent error (1 hour ago)
        stmt3 = insert(error_logs_table).values(
            timestamp=now - timedelta(hours=1),
            logger_name="very.recent.error",
            level="ERROR",
            message="Very recent error message",
        )
        await db_context.execute_with_retry(stmt3)

        # Run cleanup with 30 day retention
        deleted_count = await cleanup_old_error_logs(db_context, retention_days=30)

        # Should have deleted 1 error (the 40-day old one)
        assert deleted_count == 1

        # Verify remaining errors
        query = select(error_logs_table).order_by(error_logs_table.c.timestamp)
        remaining_errors = await db_context.fetch_all(query)

        assert len(remaining_errors) == 2
        assert all(e["logger_name"] != "old.error" for e in remaining_errors)
        assert any(e["logger_name"] == "recent.error" for e in remaining_errors)
        assert any(e["logger_name"] == "very.recent.error" for e in remaining_errors)
