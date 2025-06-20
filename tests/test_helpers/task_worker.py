"""
Helper context manager for managing TaskWorker lifecycle in tests.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from family_assistant.task_worker import TaskWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def managed_task_worker(
    task_worker: TaskWorker,
    new_task_event: asyncio.Event,
    worker_name: str = "TestWorker",
) -> AsyncGenerator[asyncio.Task, None]:
    """
    Context manager that ensures TaskWorker is properly started and stopped.

    This manager handles:
    - Starting the worker task
    - Proper shutdown even if the test fails
    - Cancellation if shutdown times out
    - Logging of all steps for debugging

    Args:
        task_worker: The TaskWorker instance to manage
        new_task_event: The event used to signal new tasks
        worker_name: Name for the worker task (for debugging)

    Yields:
        The asyncio.Task running the worker

    Example:
        async with managed_task_worker(worker, event, "MyWorker") as worker_task:
            # Run your test
            await process_some_event()
            event.set()
            await wait_for_tasks_to_complete()
    """
    worker_task = asyncio.create_task(task_worker.run(new_task_event), name=worker_name)
    await asyncio.sleep(0.1)  # Let worker start

    logger.info(f"Started task worker '{worker_name}'")

    try:
        yield worker_task
    finally:
        logger.info(f"Shutting down task worker '{worker_name}'...")

        # Signal shutdown
        if hasattr(task_worker, "shutdown_event") and task_worker.shutdown_event:
            task_worker.shutdown_event.set()

        # Wake up the worker if it's waiting
        new_task_event.set()

        # Try graceful shutdown first
        try:
            await asyncio.wait_for(worker_task, timeout=2.0)
            logger.info(f"Task worker '{worker_name}' shut down gracefully")
        except asyncio.TimeoutError:
            logger.warning(
                f"Task worker '{worker_name}' didn't shut down in time, cancelling..."
            )
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                logger.info(f"Task worker '{worker_name}' cancelled successfully")
        except Exception as e:
            logger.error(f"Error during '{worker_name}' shutdown: {e}")
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_error:
                logger.error(f"Error during '{worker_name}' cleanup: {cleanup_error}")
