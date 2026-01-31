"""
Utility functions for testing.
"""

import asyncio
import inspect
import logging
import os
import random
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import httpx
import sqlalchemy as sa  # Import sqlalchemy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.functions import (
    count as sql_count,
)  # Alias to avoid confusion with len()

# Use absolute imports if DatabaseContext is defined elsewhere,
# otherwise adjust as needed. Assuming it's accessible.
# Adjust the import path based on your project structure if needed
from family_assistant.storage.context import get_db_context
from family_assistant.storage.tasks import tasks_table

T = TypeVar("T")

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATUSES = {"done", "failed"}


async def wait_for_tasks_to_complete(
    engine: AsyncEngine,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
    task_ids: set[str] | None = None,
    task_types: set[str] | None = None,
    allow_failures: bool = False,
) -> None:
    """
    Waits until all specified tasks (or all tasks if none specified)
    in the database reach a terminal state ('done' or 'failed').

    Args:
        engine: The SQLAlchemy AsyncEngine to use for database connections.
        timeout_seconds: Maximum time to wait in seconds.
        poll_interval_seconds: How often to check the task statuses.
        task_ids: An optional set of specific task IDs to wait for. If None,
                  waits for *all* tasks currently in the table that are not
                  in a terminal state to complete.
        task_types: An optional set of task types to wait for. If specified,
                    only tasks with these types will be considered.
        allow_failures: If True, allows tasks to fail without raising RuntimeError.
                       If False (default), fails immediately if any tasks enter the
                       'failed' state or have encountered an error.

    Raises:
        asyncio.TimeoutError: If the timeout is reached before all relevant
                              tasks reach a terminal state.
        RuntimeError: If any task enters the 'failed' state or has a recorded error
                     (only when allow_failures=False).
        Exception: If a database error occurs during polling.
    """
    start_time = datetime.now(UTC)
    end_time = start_time + timedelta(seconds=timeout_seconds)

    filters = []
    if task_ids:
        filters.append(f"IDs: {task_ids}")
    if task_types:
        filters.append(f"Types: {task_types}")

    filter_msg = f" ({', '.join(filters)})" if filters else " (All non-terminal tasks)"
    logger.info(
        f"Waiting up to {timeout_seconds}s for tasks to complete...{filter_msg}"
    )

    while datetime.now(UTC) < end_time:
        try:
            # Use the provided engine to get a context
            async with get_db_context(engine=engine) as db:
                # First check for tasks that have failed or have a recorded error
                failure_condition = sa.or_(
                    tasks_table.c.status == "failed",
                    tasks_table.c.error.is_not(None),
                )
                failed_query = select(sql_count(tasks_table.c.id)).where(
                    failure_condition
                )
                # Filter by specific task IDs if provided
                if task_ids:
                    failed_query = failed_query.where(
                        tasks_table.c.task_id.in_(task_ids)
                    )
                # Filter by task types if provided
                if task_types:
                    failed_query = failed_query.where(
                        tasks_table.c.task_type.in_(task_types)
                    )

                failed_result = await db.execute_with_retry(failed_query)
                failed_count = failed_result.scalar_one_or_none()

                if failed_count and failed_count > 0 and not allow_failures:
                    # Get details of failed tasks including their last error
                    failed_task_details_query = select(
                        tasks_table.c.task_id,
                        tasks_table.c.error,  # Corrected column name
                    ).where(failure_condition)
                    if task_ids:
                        failed_task_details_query = failed_task_details_query.where(
                            tasks_table.c.task_id.in_(task_ids)
                        )
                    if task_types:
                        failed_task_details_query = failed_task_details_query.where(
                            tasks_table.c.task_type.in_(task_types)
                        )

                    failed_tasks_rows = await db.execute_with_retry(
                        failed_task_details_query
                    )

                    error_messages_list = []
                    for row in failed_tasks_rows:
                        task_id_val = row[0]  # task_id
                        # Assuming the second column is last_error.
                        # Access by index as per established pattern in this file.
                        task_error_val = row[1]
                        error_messages_list.append(
                            f"  - ID: {task_id_val}, Error: {task_error_val if task_error_val is not None else 'N/A'}"
                        )

                    if error_messages_list:
                        raise RuntimeError(
                            "Task(s) failed:\n" + "\n".join(error_messages_list)
                        )
                    else:
                        # Fallback if details couldn't be fetched but failed_count > 0
                        raise RuntimeError(
                            f"{failed_count} task(s) failed, but could not retrieve specific error details."
                        )

                # Build the query to count non-terminal tasks
                # For recurring tasks, only consider those that should have already executed
                # For non-recurring tasks, include all of them (to catch spawned tasks)
                current_time = datetime.now(UTC)
                time_with_fudge = current_time + timedelta(seconds=30)
                query = select(
                    sql_count(tasks_table.c.id)
                ).where(  # Pass column to count
                    sa.and_(
                        tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES),
                        sa.or_(
                            # Include all non-recurring tasks
                            tasks_table.c.recurrence_rule.is_(None),
                            # For recurring tasks, only include those scheduled to run soon
                            sa.and_(
                                tasks_table.c.recurrence_rule.is_not(None),
                                sa.or_(
                                    tasks_table.c.scheduled_at <= time_with_fudge,
                                    tasks_table.c.scheduled_at.is_(None),
                                ),
                            ),
                        ),
                    )
                )
                # Filter by specific task IDs if provided
                if task_ids:
                    query = query.where(tasks_table.c.task_id.in_(task_ids))
                # Filter by task types if provided
                if task_types:
                    query = query.where(tasks_table.c.task_type.in_(task_types))

                result = await db.execute_with_retry(query)
                pending_count = result.scalar_one_or_none()

                if pending_count == 0:
                    elapsed = (datetime.now(UTC) - start_time).total_seconds()
                    logger.info(f"All relevant tasks completed after {elapsed:.2f}s.")
                    return  # Success!
                elif pending_count is None:
                    # This might happen if the table is empty or due to an issue
                    # If task_ids were specified, this means none of them are pending (or exist)
                    if task_ids:
                        logger.info(
                            f"Task count query returned None for specific task IDs {task_ids}. Assuming completion."
                        )
                        return  # Assume completed if specific tasks were requested and count is None
                    else:
                        logger.warning(
                            "Task count query returned None when checking all tasks. Assuming completion or empty table."
                        )
                        return  # Assume completion if checking all and count is None
                else:
                    logger.debug(f"Waiting for {pending_count} tasks to complete...")

        except Exception as e:
            logger.error(f"Error polling task status: {e}", exc_info=True)
            raise  # Re-raise database errors

        await asyncio.sleep(poll_interval_seconds)

    # If the loop finishes without returning, timeout occurred
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    # --- Fetch details of pending tasks before raising timeout ---
    pending_tasks_details = "Could not fetch pending task details."
    try:
        async with get_db_context(engine=engine) as db:
            # Define columns explicitly to avoid issues with imported table object state
            cols_to_select = [
                sa.column("task_id"),
                sa.column("task_type"),
                sa.column("status"),
                sa.column("scheduled_at"),
                sa.column("retry_count"),
                sa.column("recurrence_rule"),
            ]
            # Show pending tasks, but for recurring tasks only show those that should have already executed
            current_time = datetime.now(UTC)
            time_with_fudge = current_time + timedelta(seconds=30)
            pending_query = (
                select(*cols_to_select)
                .select_from(tasks_table)
                .where(
                    sa.and_(
                        tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES),
                        sa.or_(
                            # Include all non-recurring tasks
                            tasks_table.c.recurrence_rule.is_(None),
                            # For recurring tasks, only include those scheduled to run soon
                            sa.and_(
                                tasks_table.c.recurrence_rule.is_not(None),
                                sa.or_(
                                    tasks_table.c.scheduled_at <= time_with_fudge,
                                    tasks_table.c.scheduled_at.is_(None),
                                ),
                            ),
                        ),
                    )
                )
            )
            if task_ids:
                pending_query = pending_query.where(tasks_table.c.task_id.in_(task_ids))
            if task_types:
                pending_query = pending_query.where(
                    tasks_table.c.task_type.in_(task_types)
                )

            pending_results = await db.fetch_all(pending_query)
            if pending_results:
                details_list = [
                    f"  - ID: {row['task_id']}, Type: {row['task_type']}, Status: {row['status']}, "
                    f"Scheduled: {row['scheduled_at']}, Retries: {row['retry_count']}, "
                    f"Recurring: {'Yes' if row.get('recurrence_rule') else 'No'}"
                    for row in pending_results
                ]
                pending_tasks_details = "Pending tasks:\n" + "\n".join(details_list)
            else:
                pending_tasks_details = "No pending tasks found matching criteria."
    except Exception as fetch_err:
        logger.error(
            f"Failed to fetch pending task details on timeout: {fetch_err}",
            exc_info=True,
        )
        pending_tasks_details = f"Error fetching pending task details: {fetch_err}"
    # --- End fetching details ---

    raise TimeoutError(
        f"Timeout ({timeout_seconds}s) waiting for tasks to complete. Elapsed: {elapsed:.2f}s\n{pending_tasks_details}"
    )


async def wait_for_condition(  # noqa: UP047 - Use TypeVar for pylint compatibility
    condition: Callable[[], T | Awaitable[T]],
    timeout: float = 30.0,
    interval: float = 0.1,
    description: str = "condition",
) -> T:
    """Wait for a condition to be truthy, with retries.

    Args:
        condition: Callable that returns a value. Can be async. Retries until truthy.
        timeout: Maximum time to wait in seconds.
        interval: Time between retries in seconds.
        description: Description for error message if timeout is reached.

    Returns:
        The truthy result from the condition.

    Raises:
        TimeoutError: If condition doesn't become truthy within timeout.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last_result = None

    while asyncio.get_running_loop().time() < deadline:
        try:
            maybe_awaitable = condition()
            if inspect.isawaitable(maybe_awaitable):
                result = await maybe_awaitable
            else:
                result = maybe_awaitable

            if result:
                return result  # type: ignore
            last_result = result
        except Exception as e:
            logger.warning(f"Condition check raised exception: {e}")
            last_result = e

        # ast-grep-ignore: no-asyncio-sleep-in-tests - This IS the wait_for_condition implementation
        await asyncio.sleep(interval)

    raise TimeoutError(
        f"Timed out waiting for {description} after {timeout}s. Last result: {last_result}"
    )


def find_free_port() -> int:
    """Find a free port, using worker-specific ranges when running under pytest-xdist."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")

    if worker_id and worker_id.startswith("gw"):
        worker_num = int(worker_id[2:])
        base_port = 40000 + (worker_num * 2000)
        max_port = base_port + 1999

        for _ in range(100):
            port = random.randint(base_port, max_port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"Could not find free port in range {base_port}-{max_port}")
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


async def wait_for_server(
    url: str, timeout: float = 30.0, check_interval: float = 0.5
) -> None:
    """
    Wait for a server to be ready by attempting to connect to it.

    Args:
        url: The URL to check
        timeout: Maximum time to wait in seconds
        check_interval: Time between checks in seconds

    Raises:
        RuntimeError: If the server doesn't start within the timeout
    """
    start_time = asyncio.get_event_loop().time()
    last_error = None

    while asyncio.get_event_loop().time() - start_time < timeout:
        try:
            async with (
                httpx.AsyncClient() as client,
                client.stream("GET", url, timeout=1.0) as response,
            ):
                if response.status_code == 200:
                    logger.info(
                        f"Server is ready on {url} (status: {response.status_code})"
                    )
                    return
                elif response.status_code:
                    logger.warning(
                        f"Server responded with status {response.status_code} on {url}"
                    )
                    return
        except httpx.ConnectError as e:
            last_error = e
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling retry
            await asyncio.sleep(check_interval)
        except httpx.ReadTimeout:
            logger.info(f"Server is ready on {url} (SSE stream established)")
            return
        except Exception as e:
            logger.warning(f"Unexpected error checking {url}: {type(e).__name__}: {e}")
            last_error = e
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling retry
            await asyncio.sleep(check_interval)

    raise RuntimeError(
        f"Server did not start on {url} within {timeout} seconds. Last error: {last_error}"
    )


__all__ = [
    "wait_for_tasks_to_complete",
    "wait_for_condition",
    "find_free_port",
    "wait_for_server",
]
