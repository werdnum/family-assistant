"""
Utility functions for testing.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Set
import sqlalchemy as sa  # Import sqlalchemy
from sqlalchemy import select
from sqlalchemy.sql.functions import (
    count as sql_count,
)  # Alias to avoid confusion with len()
from sqlalchemy.ext.asyncio import AsyncEngine

# Use absolute imports if DatabaseContext is defined elsewhere,
# otherwise adjust as needed. Assuming it's accessible.
# Adjust the import path based on your project structure if needed
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.tasks import tasks_table

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATUSES = {"done", "failed"}


async def wait_for_tasks_to_complete(
    engine: AsyncEngine,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
    task_ids: Optional[Set[str]] = None,
):
    """
    Waits until all specified tasks (or all tasks if none specified)
    in the database reach a terminal state ('done' or 'failed').
    Fails immediately if any tasks enter the 'failed' state.

    Args:
        engine: The SQLAlchemy AsyncEngine to use for database connections.
        timeout_seconds: Maximum time to wait in seconds.
        poll_interval_seconds: How often to check the task statuses.
        task_ids: An optional set of specific task IDs to wait for. If None,
                  waits for *all* tasks currently in the table that are not
                  in a terminal state to complete.

    Raises:
        asyncio.TimeoutError: If the timeout is reached before all relevant
                              tasks reach a terminal state.
        RuntimeError: If any task enters the 'failed' state.
        Exception: If a database error occurs during polling.
    """
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(seconds=timeout_seconds)

    logger.info(
        f"Waiting up to {timeout_seconds}s for tasks to complete..."
        f"{' (Specific IDs: ' + str(task_ids) + ')' if task_ids else ' (All non-terminal tasks)'}"
    )

    while datetime.now(timezone.utc) < end_time:
        try:
            # Use the provided engine to get a context
            async with await get_db_context(engine=engine) as db:
                # First check for failed tasks
                failed_query = select(
                    sql_count(tasks_table.c.id)
                ).where(  # Pass column to count
                    tasks_table.c.status == "failed"
                )
                # Filter by specific task IDs if provided
                if task_ids:
                    failed_query = failed_query.where(
                        tasks_table.c.task_id.in_(task_ids)
                    )

                failed_result = await db.execute_with_retry(failed_query)
                failed_count = failed_result.scalar_one_or_none()

                if failed_count and failed_count > 0:
                    # Get details of failed tasks including their last error
                    failed_task_details_query = select(
                        tasks_table.c.task_id,
                        tasks_table.c.last_error  # Assuming this column exists
                    ).where(tasks_table.c.status == "failed")

                        tasks_table.c.status == "failed"
                    )
                    if task_ids:
                        failed_task_details_query = failed_task_details_query.where(
                            tasks_table.c.task_id.in_(task_ids)
                        )

                    failed_tasks_rows = await db.execute_with_retry(failed_task_details_query)

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
                        raise RuntimeError(f"Task(s) failed:\n" + "\n".join(error_messages_list))
                    else:
                        # Fallback if details couldn't be fetched but failed_count > 0
                        raise RuntimeError(f"{failed_count} task(s) failed, but could not retrieve specific error details.")

                # Build the query to count non-terminal tasks
                query = select(

                    sql_count(tasks_table.c.id)
                ).where(  # Pass column to count
                    tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES)
                )
                # Filter by specific task IDs if provided
                if task_ids:
                    query = query.where(tasks_table.c.task_id.in_(task_ids))

                result = await db.execute_with_retry(query)
                pending_count = result.scalar_one_or_none()

                if pending_count == 0:
                    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
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
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    # --- Fetch details of pending tasks before raising timeout ---
    pending_tasks_details = "Could not fetch pending task details."
    try:
        async with await get_db_context(engine=engine) as db:
            # Define columns explicitly to avoid issues with imported table object state
            cols_to_select = [
                sa.column("task_id"),
                sa.column("task_type"),
                sa.column("status"),
                sa.column("scheduled_at"),
                sa.column("retry_count"),
                # sa.column("last_error"),
            ]
            pending_query = (
                select(*cols_to_select)
                .select_from(tasks_table)
                .where(tasks_table.c.status.notin_(TERMINAL_TASK_STATUSES))
            )
            if task_ids:
                pending_query = pending_query.where(tasks_table.c.task_id.in_(task_ids))

            pending_results = await db.fetch_all(pending_query)
            if pending_results:
                details_list = [
                    f"  - ID: {row['task_id']}, Type: {row['task_type']}, Status: {row['status']}, Scheduled: {row['scheduled_at']}, Retries: {row['retry_count']}"
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

    raise asyncio.TimeoutError(
        f"Timeout ({timeout_seconds}s) waiting for tasks to complete. Elapsed: {elapsed:.2f}s\n{pending_tasks_details}"
    )


__all__ = ["wait_for_tasks_to_complete"]
