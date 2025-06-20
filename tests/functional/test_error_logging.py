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
    # Clear all existing error logs first to ensure test isolation
    async with DatabaseContext(engine=test_db_engine) as db_context:
        from sqlalchemy import delete

        # Clear all error logs
        await db_context.execute_with_retry(delete(error_logs_table))

    # Create a test logger with our handler
    test_logger = logging.getLogger("test_error_logger")
    test_logger.setLevel(logging.DEBUG)
    # Prevent propagation to avoid duplicate handling by parent loggers
    test_logger.propagate = False

    # Debug: Check existing handlers
    if test_logger.handlers:
        print(
            f"\nWARNING: test_error_logger already has {len(test_logger.handlers)} handlers before test!"
        )
        test_logger.handlers.clear()

    # Add our SQLAlchemy handler
    # Create a session factory that uses our test engine directly
    def test_db_context_factory() -> DatabaseContext:
        return DatabaseContext(engine=test_db_engine)

    handler = SQLAlchemyErrorHandler(test_db_context_factory, min_level=logging.ERROR)
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

        # Flush the handler to ensure all logs are written
        handler.flush()

        # Give async logging handler time to complete writes
        import asyncio

        await asyncio.sleep(0.5)

        # Check the database
        async with DatabaseContext(engine=test_db_engine) as db_context:
            # Get error logs from our test logger only
            query = (
                select(error_logs_table)
                .where(error_logs_table.c.logger_name == "test_error_logger")
                .order_by(error_logs_table.c.timestamp)
            )
            error_logs = await db_context.fetch_all(query)

            # Should have 3 error logs (2 ERROR, 1 CRITICAL)
            if len(error_logs) != 3:
                # Debug: print all error logs found
                print(f"\nExpected 3 error logs, but found {len(error_logs)}:")
                for i, log in enumerate(error_logs):
                    print(f"\n{i + 1}. Logger: {log['logger_name']}")
                    print(f"   Level: {log['level']}")
                    print(f"   Message: {log['message']}")
                    print(f"   Timestamp: {log['timestamp']}")
                    print(f"   Module: {log['module']}")
                    print(f"   Function: {log['function_name']}")

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
        # Clean up the handler and reset logger state
        handler.close()  # Properly close the handler to stop worker thread
        test_logger.removeHandler(handler)
        # Remove any other handlers that might have been added
        test_logger.handlers.clear()
        # Reset propagate to default
        test_logger.propagate = True


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

        # Verify remaining errors (only count the ones we created)
        query = (
            select(error_logs_table)
            .where(
                error_logs_table.c.logger_name.in_([
                    "old.error",
                    "recent.error",
                    "very.recent.error",
                ])
            )
            .order_by(error_logs_table.c.timestamp)
        )
        remaining_errors = await db_context.fetch_all(query)

        assert len(remaining_errors) == 2
        assert all(e["logger_name"] != "old.error" for e in remaining_errors)
        assert any(e["logger_name"] == "recent.error" for e in remaining_errors)
        assert any(e["logger_name"] == "very.recent.error" for e in remaining_errors)
