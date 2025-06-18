"""
Time API for Starlark scripts.

This module provides time manipulation capabilities for Starlark scripts,
following patterns from starlark-go but adapted for starlark-pyo3's constraints.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Duration constants (in seconds)
NANOSECOND = 0.000000001
MICROSECOND = 0.000001
MILLISECOND = 0.001
SECOND = 1
MINUTE = 60
HOUR = 3600
DAY = 86400
WEEK = 604800


def _datetime_to_dict(dt: datetime) -> dict[str, Any]:
    """Convert a datetime object to a dictionary representation."""
    return {
        "year": dt.year,
        "month": dt.month,
        "day": dt.day,
        "hour": dt.hour,
        "minute": dt.minute,
        "second": dt.second,
        "nanosecond": dt.microsecond * 1000,
        "unix": int(dt.timestamp()),
        "unix_nano": int(dt.timestamp() * 1_000_000_000),
        "timezone": str(dt.tzinfo) if dt.tzinfo else "UTC",
    }


def _dict_to_datetime(time_dict: dict[str, Any]) -> datetime:
    """Convert a time dictionary back to a datetime object."""
    tz_str = time_dict.get("timezone", "UTC")
    if tz_str == "UTC":
        tz = timezone.utc
    else:
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            logger.warning(f"Invalid timezone '{tz_str}', using UTC")
            tz = timezone.utc

    return datetime(
        year=time_dict.get("year", 1970),
        month=time_dict.get("month", 1),
        day=time_dict.get("day", 1),
        hour=time_dict.get("hour", 0),
        minute=time_dict.get("minute", 0),
        second=time_dict.get("second", 0),
        microsecond=time_dict.get("nanosecond", 0) // 1000,
        tzinfo=tz,
    )


# Time Creation Functions


def time_now() -> dict[str, Any]:
    """Get the current time in the local timezone."""
    return _datetime_to_dict(datetime.now())


def time_now_utc() -> dict[str, Any]:
    """Get the current time in UTC."""
    return _datetime_to_dict(datetime.now(timezone.utc))


def time_create(
    year: int = 1970,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    nanosecond: int = 0,
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    """
    Create a time object with the specified components.

    Args:
        year: The year (default: 1970)
        month: The month (1-12, default: 1)
        day: The day (1-31, default: 1)
        hour: The hour (0-23, default: 0)
        minute: The minute (0-59, default: 0)
        second: The second (0-59, default: 0)
        nanosecond: The nanosecond (0-999999999, default: 0)
        timezone_name: The timezone name (default: "UTC")

    Returns:
        A time dictionary
    """
    if timezone_name == "UTC":
        tz = timezone.utc
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            logger.warning(f"Invalid timezone '{timezone_name}', using UTC")
            tz = timezone.utc

    dt = datetime(
        year=year,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        second=second,
        microsecond=nanosecond // 1000,
        tzinfo=tz,
    )
    return _datetime_to_dict(dt)


def time_from_timestamp(seconds: float, nanoseconds: int = 0) -> dict[str, Any]:
    """
    Create a time object from a Unix timestamp.

    Args:
        seconds: Unix timestamp in seconds
        nanoseconds: Additional nanoseconds (optional)

    Returns:
        A time dictionary in UTC
    """
    total_seconds = seconds + (nanoseconds / 1_000_000_000)
    dt = datetime.fromtimestamp(total_seconds, tz=timezone.utc)
    return _datetime_to_dict(dt)


def time_parse(
    time_string: str, format_string: str = "", timezone_name: str = ""
) -> dict[str, Any]:
    """
    Parse a time string into a time object.

    Args:
        time_string: The time string to parse
        format_string: The format string (strftime format). If empty, tries common formats
        timezone_name: The timezone to use. If empty, uses UTC for naive times

    Returns:
        A time dictionary
    """
    # Common formats to try if no format specified
    formats_to_try = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
    ]

    # Handle RFC3339/ISO8601 with timezone
    if not format_string and ("+" in time_string or time_string.endswith("Z")):
        try:
            dt = datetime.fromisoformat(time_string.replace("Z", "+00:00"))
            return _datetime_to_dict(dt)
        except Exception:
            pass

    if format_string:
        formats_to_try = [format_string]

    dt = None
    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(time_string, fmt)
            break
        except ValueError:
            continue

    if dt is None:
        raise ValueError(f"Could not parse time string: {time_string}")

    # Apply timezone if specified
    if timezone_name:
        if timezone_name == "UTC":
            tz = timezone.utc
        else:
            try:
                tz = ZoneInfo(timezone_name)
            except Exception:
                logger.warning(f"Invalid timezone '{timezone_name}', using UTC")
                tz = timezone.utc
        dt = dt.replace(tzinfo=tz)
    elif dt.tzinfo is None:
        # Default to UTC for naive datetimes
        dt = dt.replace(tzinfo=timezone.utc)

    return _datetime_to_dict(dt)


# Time Manipulation Functions


def time_in_location(time_dict: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    """
    Convert a time to a different timezone.

    Args:
        time_dict: The time dictionary
        timezone_name: The target timezone name

    Returns:
        A new time dictionary in the specified timezone
    """
    dt = _dict_to_datetime(time_dict)

    if timezone_name == "UTC":
        tz = timezone.utc
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception:
            logger.warning(f"Invalid timezone '{timezone_name}', using UTC")
            tz = timezone.utc

    dt_converted = dt.astimezone(tz)
    return _datetime_to_dict(dt_converted)


def time_format(time_dict: dict[str, Any], format_string: str) -> str:
    """
    Format a time according to a format string.

    Args:
        time_dict: The time dictionary
        format_string: The format string (strftime format) or special constants:
                      "RFC3339" - ISO format with timezone
                      "ISO8601" - Same as RFC3339

    Returns:
        The formatted time string
    """
    dt = _dict_to_datetime(time_dict)

    if format_string in ("RFC3339", "ISO8601"):
        return dt.isoformat()

    return dt.strftime(format_string)


def time_add(time_dict: dict[str, Any], seconds: float) -> dict[str, Any]:
    """
    Add seconds to a time.

    Args:
        time_dict: The time dictionary
        seconds: Number of seconds to add (can be negative)

    Returns:
        A new time dictionary
    """
    dt = _dict_to_datetime(time_dict)
    dt_new = dt + timedelta(seconds=seconds)
    return _datetime_to_dict(dt_new)


def time_add_duration(
    time_dict: dict[str, Any], amount: float, unit: str
) -> dict[str, Any]:
    """
    Add a duration with a specific unit to a time.

    Args:
        time_dict: The time dictionary
        amount: The amount to add (can be negative)
        unit: The unit ("seconds", "minutes", "hours", "days", "weeks")

    Returns:
        A new time dictionary
    """
    unit_map = {
        "seconds": 1,
        "second": 1,
        "minutes": MINUTE,
        "minute": MINUTE,
        "hours": HOUR,
        "hour": HOUR,
        "days": DAY,
        "day": DAY,
        "weeks": WEEK,
        "week": WEEK,
    }

    multiplier = unit_map.get(unit.lower(), 1)
    return time_add(time_dict, amount * multiplier)


# Time Component Functions


def time_year(time_dict: dict[str, Any]) -> int:
    """Get the year from a time dictionary."""
    return time_dict.get("year", 1970)


def time_month(time_dict: dict[str, Any]) -> int:
    """Get the month from a time dictionary."""
    return time_dict.get("month", 1)


def time_day(time_dict: dict[str, Any]) -> int:
    """Get the day from a time dictionary."""
    return time_dict.get("day", 1)


def time_hour(time_dict: dict[str, Any]) -> int:
    """Get the hour from a time dictionary."""
    return time_dict.get("hour", 0)


def time_minute(time_dict: dict[str, Any]) -> int:
    """Get the minute from a time dictionary."""
    return time_dict.get("minute", 0)


def time_second(time_dict: dict[str, Any]) -> int:
    """Get the second from a time dictionary."""
    return time_dict.get("second", 0)


def time_weekday(time_dict: dict[str, Any]) -> int:
    """
    Get the weekday from a time dictionary.

    Returns:
        0 for Monday, 6 for Sunday
    """
    dt = _dict_to_datetime(time_dict)
    return dt.weekday()


# Time Comparison Functions


def time_before(t1: dict[str, Any], t2: dict[str, Any]) -> bool:
    """Check if t1 is before t2."""
    return int(t1["unix_nano"]) < int(t2["unix_nano"])


def time_after(t1: dict[str, Any], t2: dict[str, Any]) -> bool:
    """Check if t1 is after t2."""
    return int(t1["unix_nano"]) > int(t2["unix_nano"])


def time_equal(t1: dict[str, Any], t2: dict[str, Any]) -> bool:
    """Check if t1 equals t2."""
    return int(t1["unix_nano"]) == int(t2["unix_nano"])


def time_diff(t1: dict[str, Any], t2: dict[str, Any]) -> float:
    """
    Calculate the difference between two times in seconds.

    Returns:
        t1 - t2 in seconds
    """
    return (int(t1["unix_nano"]) - int(t2["unix_nano"])) / 1_000_000_000


# Duration Functions


def duration_parse(duration_string: str) -> float:
    """
    Parse a duration string into seconds.

    Supports formats like:
    - "1h30m" -> 5400
    - "2d12h" -> 216000
    - "30s" -> 30
    - "1w2d" -> 777600

    Args:
        duration_string: The duration string to parse

    Returns:
        Duration in seconds
    """
    import re

    total_seconds = 0.0

    # Pattern to match number + unit pairs
    pattern = r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)"
    matches = re.findall(pattern, duration_string)

    unit_map = {
        "ns": NANOSECOND,
        "nanosecond": NANOSECOND,
        "nanoseconds": NANOSECOND,
        "us": MICROSECOND,
        "microsecond": MICROSECOND,
        "microseconds": MICROSECOND,
        "ms": MILLISECOND,
        "millisecond": MILLISECOND,
        "milliseconds": MILLISECOND,
        "s": SECOND,
        "sec": SECOND,
        "second": SECOND,
        "seconds": SECOND,
        "m": MINUTE,
        "min": MINUTE,
        "minute": MINUTE,
        "minutes": MINUTE,
        "h": HOUR,
        "hr": HOUR,
        "hour": HOUR,
        "hours": HOUR,
        "d": DAY,
        "day": DAY,
        "days": DAY,
        "w": WEEK,
        "week": WEEK,
        "weeks": WEEK,
    }

    for amount_str, unit in matches:
        amount = float(amount_str)
        unit_lower = unit.lower()
        if unit_lower in unit_map:
            total_seconds += amount * unit_map[unit_lower]
        else:
            raise ValueError(f"Unknown duration unit: {unit}")

    if total_seconds == 0 and duration_string.strip():
        raise ValueError(f"Could not parse duration: {duration_string}")

    return total_seconds


def duration_human(seconds: float) -> str:
    """
    Convert seconds to a human-readable duration string.

    Args:
        seconds: Duration in seconds

    Returns:
        Human-readable string like "1h30m" or "2d12h"
    """
    if seconds < 0:
        return "-" + duration_human(-seconds)

    if seconds == 0:
        return "0s"

    parts = []

    # Calculate each unit
    weeks = int(seconds // WEEK)
    seconds %= WEEK

    days = int(seconds // DAY)
    seconds %= DAY

    hours = int(seconds // HOUR)
    seconds %= HOUR

    minutes = int(seconds // MINUTE)
    seconds %= MINUTE

    # Build the string
    if weeks > 0:
        parts.append(f"{weeks}w")
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or not parts:
        if seconds == int(seconds):
            parts.append(f"{int(seconds)}s")
        else:
            # Format with up to 3 decimal places, removing trailing zeros
            formatted = f"{seconds:.3f}".rstrip("0").rstrip(".")
            parts.append(f"{formatted}s")

    return "".join(parts)


# Timezone Functions


def timezone_is_valid(timezone_name: str) -> bool:
    """
    Check if a timezone name is valid.

    Args:
        timezone_name: The timezone name to check

    Returns:
        True if the timezone is valid
    """
    if timezone_name == "UTC":
        return True

    try:
        ZoneInfo(timezone_name)
        return True
    except Exception:
        return False


def timezone_offset(timezone_name: str, time_dict: dict[str, Any] | None = None) -> int:
    """
    Get the offset in seconds for a timezone at a specific time.

    Args:
        timezone_name: The timezone name
        time_dict: The time to check (uses current time if None)

    Returns:
        Offset from UTC in seconds
    """
    if time_dict is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = _dict_to_datetime(time_dict)

    if timezone_name == "UTC":
        tz = timezone.utc
    else:
        try:
            tz = ZoneInfo(timezone_name)
        except Exception as e:
            raise ValueError(f"Invalid timezone: {timezone_name}") from e

    # Get the offset
    dt_in_tz = dt.astimezone(tz)
    offset = dt_in_tz.utcoffset()

    if offset is None:
        return 0

    return int(offset.total_seconds())


# Utility Functions


def is_between(
    start_hour: int, end_hour: int, time_dict: dict[str, Any] | None = None
) -> bool:
    """
    Check if the current time (or specified time) is between two hours.

    Args:
        start_hour: Start hour (0-23)
        end_hour: End hour (0-23)
        time_dict: Time to check (uses current time if None)

    Returns:
        True if the time is within the range
    """
    if time_dict is None:
        time_dict = time_now()

    hour = time_hour(time_dict)

    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    else:
        # Handle overnight ranges (e.g., 22:00 to 02:00)
        return hour >= start_hour or hour < end_hour


def is_weekend(time_dict: dict[str, Any] | None = None) -> bool:
    """
    Check if a time falls on a weekend.

    Args:
        time_dict: Time to check (uses current time if None)

    Returns:
        True if Saturday or Sunday
    """
    if time_dict is None:
        time_dict = time_now()

    weekday = time_weekday(time_dict)
    return weekday >= 5  # 5=Saturday, 6=Sunday
