"""Clock utilities for managing time, allowing for mockable time in tests."""

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """A protocol for objects that can provide the current time."""

    def now(self) -> datetime:
        """Return the current datetime, timezone-aware (UTC)."""
        ...


class SystemClock:
    """A clock that provides the real system time."""

    def now(self) -> datetime:
        """Return the current system datetime in UTC."""
        return datetime.now(timezone.utc)


class MockClock:
    """
    A clock that allows setting and advancing time, for use in tests.
    All times are handled as UTC.
    """

    def __init__(self, initial_time: datetime | None = None) -> None:
        if initial_time and initial_time.tzinfo is None:
            raise ValueError("MockClock initial_time must be timezone-aware.")
        self._current_time: datetime = initial_time or datetime.now(timezone.utc)

    def now(self) -> datetime:
        """Return the current mock datetime in UTC."""
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        """
        Set the current mock time to a specific datetime.
        The new_time must be timezone-aware.
        """
        if new_time.tzinfo is None:
            raise ValueError("MockClock.set_time requires a timezone-aware datetime.")
        self._current_time = new_time

    def advance(self, duration: timedelta) -> None:
        """Advance the current mock time by a given timedelta."""
        self._current_time += duration
