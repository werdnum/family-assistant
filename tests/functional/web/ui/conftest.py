"""Shared fixtures and helpers for web UI tests."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def wait_for_condition(  # noqa: UP047 - Use TypeVar for pylint compatibility
    condition: Callable[[], Awaitable[T]],
    timeout: float = 2.0,
    interval: float = 0.1,
    description: str = "condition",
) -> T:
    """Wait for a condition to be truthy, with retries.

    This is useful for SQLite tests where transaction visibility may be delayed
    due to WAL mode or connection pool timing. Rather than forcing WAL checkpoints,
    this provides a more general retry mechanism.

    Args:
        condition: Async callable that returns a value. Retries until truthy.
        timeout: Maximum time to wait in seconds.
        interval: Time between retries in seconds.
        description: Description for error message if timeout is reached.

    Returns:
        The truthy result from the condition.

    Raises:
        TimeoutError: If condition doesn't become truthy within timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last_result = None

    while asyncio.get_event_loop().time() < deadline:
        result = await condition()
        if result:
            return result
        last_result = result
        # ast-grep-ignore: no-asyncio-sleep-in-tests - This IS the wait_for_condition implementation
        await asyncio.sleep(interval)

    raise TimeoutError(
        f"Timed out waiting for {description} after {timeout}s. Last result: {last_result}"
    )
