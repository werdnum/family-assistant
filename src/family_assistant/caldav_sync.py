"""Narrow caldav's dual-mode return types for code that drives a synchronous client.

caldav 3 shares its domain objects (``Calendar``, ``Event``, ...) between ``DAVClient``
and ``AsyncDAVClient``, so their methods are annotated ``T | Coroutine[Any, Any, T]``.
A coroutine is only returned when the object is bound to an ``AsyncDAVClient``, which
this codebase never uses.
"""

from collections.abc import Coroutine
from typing import Any


def sync_result[T](value: T | Coroutine[Any, Any, T]) -> T:
    if isinstance(value, Coroutine):
        value.close()
        raise TypeError(
            "caldav returned a coroutine; the object is bound to an async client"
        )
    return value
