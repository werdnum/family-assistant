"""
Utility functions for testing.
"""

import asyncio
import inspect
import logging
import os
import random
import shutil
import socket
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar, cast

import httpx
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.functions import (
    count as sql_count,
)

from family_assistant.llm.messages import UserMessage
from family_assistant.storage.database import Database
from family_assistant.storage.tasks import tasks_table

T = TypeVar("T")

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATUSES = {"done", "failed"}


async def _tasks_are_complete(
    engine: AsyncEngine,
    start_time: datetime,
    task_ids: set[str] | None,
    task_types: set[str] | None,
    allow_failures: bool,
) -> bool:
    db = Database(engine=engine)
    failure_condition = sa.or_(
        tasks_table.c.status == "failed",
        tasks_table.c.error.is_not(None),
    )
    failed_query = select(sql_count(tasks_table.c.id)).where(failure_condition)
    if task_ids:
        failed_query = failed_query.where(tasks_table.c.task_id.in_(task_ids))
    if task_types:
        failed_query = failed_query.where(tasks_table.c.task_type.in_(task_types))

    failed_result = await db.execute(failed_query)
    failed_count = failed_result.scalar_one_or_none()

    if failed_count and failed_count > 0 and not allow_failures:
        failed_task_details_query = select(
            tasks_table.c.task_id,
            tasks_table.c.error,
        ).where(failure_condition)
        if task_ids:
            failed_task_details_query = failed_task_details_query.where(
                tasks_table.c.task_id.in_(task_ids)
            )
        if task_types:
            failed_task_details_query = failed_task_details_query.where(
                tasks_table.c.task_type.in_(task_types)
            )

        failed_tasks_rows = await db.fetch_all(failed_task_details_query)
        error_messages_list = [
            f"  - ID: {row['task_id']}, Error: {row['error'] if row['error'] is not None else 'N/A'}"
            for row in failed_tasks_rows
        ]
        if error_messages_list:
            raise RuntimeError("Task(s) failed:\n" + "\n".join(error_messages_list))
        raise RuntimeError(
            f"{failed_count} task(s) failed, but could not retrieve specific error details."
        )

    current_time = datetime.now(UTC)
    time_with_fudge = current_time + timedelta(seconds=30)
    query = select(sql_count(tasks_table.c.id)).where(
        sa.and_(
            tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES),
            sa.or_(
                tasks_table.c.recurrence_rule.is_(None),
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
    if task_ids:
        query = query.where(tasks_table.c.task_id.in_(task_ids))
    if task_types:
        query = query.where(tasks_table.c.task_type.in_(task_types))

    result = await db.execute(query)
    pending_count = result.scalar_one_or_none()

    if pending_count == 0:
        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        logger.info(f"All relevant tasks completed after {elapsed:.2f}s.")
        return True
    if pending_count is None:
        if task_ids:
            logger.info(
                f"Task count query returned None for specific task IDs {task_ids}. Assuming completion."
            )
        else:
            logger.warning(
                "Task count query returned None when checking all tasks. Assuming completion or empty table."
            )
        return True

    logger.debug(f"Waiting for {pending_count} tasks to complete...")
    return False


async def _get_pending_task_details(
    engine: AsyncEngine,
    task_ids: set[str] | None,
    task_types: set[str] | None,
) -> str:
    db = Database(engine=engine)
    cols_to_select = [
        sa.column("task_id"),
        sa.column("task_type"),
        sa.column("status"),
        sa.column("scheduled_at"),
        sa.column("retry_count"),
        sa.column("recurrence_rule"),
    ]
    current_time = datetime.now(UTC)
    time_with_fudge = current_time + timedelta(seconds=30)
    pending_query = (
        select(*cols_to_select)
        .select_from(tasks_table)
        .where(
            sa.and_(
                tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES),
                sa.or_(
                    tasks_table.c.recurrence_rule.is_(None),
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
        pending_query = pending_query.where(tasks_table.c.task_type.in_(task_types))

    pending_results = await db.fetch_all(pending_query)
    if not pending_results:
        return "No pending tasks found matching criteria."

    details_list = [
        f"  - ID: {row['task_id']}, Type: {row['task_type']}, Status: {row['status']}, "
        f"Scheduled: {row['scheduled_at']}, Retries: {row['retry_count']}, "
        f"Recurring: {'Yes' if row.get('recurrence_rule') else 'No'}"
        for row in pending_results
    ]
    return "Pending tasks:\n" + "\n".join(details_list)


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
            if await _tasks_are_complete(
                engine, start_time, task_ids, task_types, allow_failures
            ):
                return

        except Exception as e:
            logger.exception(f"Error polling task status: {e}")
            raise  # Re-raise database errors

        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling helper interval
        await asyncio.sleep(poll_interval_seconds)

    # If the loop finishes without returning, timeout occurred
    elapsed = (datetime.now(UTC) - start_time).total_seconds()

    # --- Fetch details of pending tasks before raising timeout ---
    pending_tasks_details = "Could not fetch pending task details."
    try:
        pending_tasks_details = await _get_pending_task_details(
            engine, task_ids, task_types
        )
    except Exception as fetch_err:
        logger.exception(
            f"Failed to fetch pending task details on timeout: {fetch_err}"
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
            result = cast("T", await _evaluate_condition(condition))
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


async def _evaluate_condition(condition: Callable[[], object]) -> object:
    maybe_awaitable = condition()
    if inspect.isawaitable(maybe_awaitable):
        return await maybe_awaitable
    return maybe_awaitable


def find_free_port() -> int:
    """Find a free port, using worker-specific ranges when running under pytest-xdist."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")

    if worker_id and worker_id.startswith("gw"):
        worker_num = int(worker_id[2:])
        ports_per_worker = 512
        base_port = 40000 + (worker_num * ports_per_worker)
        max_port = base_port + ports_per_worker - 1

        if max_port > 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

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


def require_executable(command_name: str) -> str:
    """Resolve a test dependency executable or fail with a clear message."""
    executable = shutil.which(command_name)
    if executable is not None:
        return executable

    for executable_dir in (
        Path(sys.executable).parent,
        Path(sys.executable).resolve().parent,
    ):
        venv_executable = executable_dir / command_name
        if venv_executable.exists():
            return str(venv_executable)

    raise RuntimeError(f"Required test executable not found: {command_name}")


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


async def seed_known_conversation(
    engine: AsyncEngine,
    conversation_id: str,
    *,
    interface_type: str = "telegram",
    user_id: str = "known-user",
    text: str = "Hello",
) -> None:
    """Persist a user message so ``conversation_id`` is a known message target.

    ``send_message_to_user`` only delivers to conversations an authorized user
    has already talked to the assistant in, which is how a real conversation
    comes to exist. Tests that send to a conversation they did not otherwise
    drive traffic through need this to make the target legitimate.
    """
    db = Database(engine)
    await db.message_history.add_message(
        UserMessage(content=text),
        interface_type=interface_type,
        conversation_id=conversation_id,
        timestamp=datetime.now(UTC),
        user_id=user_id,
    )


__all__ = [
    "find_free_port",
    "require_executable",
    "seed_known_conversation",
    "wait_for_condition",
    "wait_for_server",
    "wait_for_tasks_to_complete",
]
