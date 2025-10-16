"""Shared utilities for datetime normalization across database backends."""

from datetime import datetime, timezone

from dateutil.parser import parse as parse_datetime


def normalize_datetime(value: datetime | str | None) -> datetime | None:
    """
    Normalize datetime from database to timezone-aware UTC datetime.

    Handles both SQLite (returns ISO string) and PostgreSQL (returns datetime object).

    Args:
        value: datetime object, ISO string, or None

    Returns:
        Timezone-aware datetime in UTC or None
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        # If already a datetime, ensure it's timezone-aware
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    # If it's a string, parse it as ISO format
    if isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    return None
