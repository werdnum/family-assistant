"""Tests for the Starlark time API."""

from datetime import datetime, timezone

import pytest

from family_assistant.scripting.apis import time as time_api


class TestTimeCreation:
    """Test time creation functions."""

    def test_time_now(self) -> None:
        """Test getting current time."""
        result = time_api.time_now()

        # Check structure
        assert isinstance(result, dict)
        assert "year" in result
        assert "month" in result
        assert "day" in result
        assert "hour" in result
        assert "minute" in result
        assert "second" in result
        assert "nanosecond" in result
        assert "unix" in result
        assert "unix_nano" in result
        assert "timezone" in result

        # Verify it's roughly current time
        now = datetime.now()
        assert result["year"] == now.year
        assert result["month"] == now.month
        assert result["day"] == now.day

    def test_time_now_utc(self) -> None:
        """Test getting current UTC time."""
        result = time_api.time_now_utc()

        assert result["timezone"] == "UTC"

        # Verify it's roughly current UTC time
        now_utc = datetime.now(timezone.utc)
        assert result["year"] == now_utc.year
        assert result["month"] == now_utc.month
        assert result["day"] == now_utc.day

    def test_time_create(self) -> None:
        """Test creating a specific time."""
        result = time_api.time_create(
            year=2024, month=12, day=25, hour=15, minute=30, second=45
        )

        assert result["year"] == 2024
        assert result["month"] == 12
        assert result["day"] == 25
        assert result["hour"] == 15
        assert result["minute"] == 30
        assert result["second"] == 45
        assert result["timezone"] == "UTC"

    def test_time_create_with_timezone(self) -> None:
        """Test creating time with specific timezone."""
        result = time_api.time_create(
            year=2024, month=6, day=15, hour=14, timezone_name="America/New_York"
        )

        assert result["year"] == 2024
        assert result["month"] == 6
        assert result["day"] == 15
        assert result["hour"] == 14
        assert "America/New_York" in result["timezone"]

    def test_time_from_timestamp(self) -> None:
        """Test creating time from Unix timestamp."""
        # Known timestamp: 2024-01-01 00:00:00 UTC
        timestamp = 1704067200
        result = time_api.time_from_timestamp(timestamp)

        assert result["year"] == 2024
        assert result["month"] == 1
        assert result["day"] == 1
        assert result["hour"] == 0
        assert result["minute"] == 0
        assert result["second"] == 0
        assert result["unix"] == timestamp

    def test_time_from_timestamp_with_nanos(self) -> None:
        """Test creating time from timestamp with nanoseconds."""
        timestamp = 1704067200
        nanos = 500_000_000  # 0.5 seconds
        result = time_api.time_from_timestamp(timestamp, nanos)

        assert result["unix"] == timestamp
        assert result["nanosecond"] == nanos

    def test_time_parse_iso(self) -> None:
        """Test parsing ISO format strings."""
        test_cases = [
            "2024-12-25T15:30:45Z",
            "2024-12-25T15:30:45+00:00",
            "2024-12-25T15:30:45.123Z",
        ]

        for time_str in test_cases:
            result = time_api.time_parse(time_str)
            assert result["year"] == 2024
            assert result["month"] == 12
            assert result["day"] == 25
            assert result["hour"] == 15
            assert result["minute"] == 30
            assert result["second"] == 45

    def test_time_parse_common_formats(self) -> None:
        """Test parsing common date/time formats."""
        test_cases = [
            ("2024-12-25 15:30:45", None),
            ("2024-12-25", None),
            ("25/12/2024", None),
            ("12/25/2024", None),
        ]

        for time_str, fmt in test_cases:
            result = time_api.time_parse(time_str, fmt or "")
            assert result["year"] == 2024
            assert result["month"] == 12
            assert result["day"] == 25

    def test_time_parse_with_format(self) -> None:
        """Test parsing with explicit format string."""
        result = time_api.time_parse("Dec 25, 2024 3:30 PM", "%b %d, %Y %I:%M %p")

        assert result["year"] == 2024
        assert result["month"] == 12
        assert result["day"] == 25
        assert result["hour"] == 15
        assert result["minute"] == 30


class TestTimeManipulation:
    """Test time manipulation functions."""

    def test_time_in_location(self) -> None:
        """Test timezone conversion."""
        # Create UTC time
        utc_time = time_api.time_create(
            year=2024, month=12, day=25, hour=15, timezone_name="UTC"
        )

        # Convert to New York time
        ny_time = time_api.time_in_location(utc_time, "America/New_York")

        # In December, NY is UTC-5
        assert ny_time["hour"] == 10  # 15:00 UTC = 10:00 EST
        assert "America/New_York" in ny_time["timezone"]

    def test_time_format(self) -> None:
        """Test time formatting."""
        t = time_api.time_create(
            year=2024, month=12, day=25, hour=15, minute=30, second=45
        )

        # Test strftime format
        result = time_api.time_format(t, "%Y-%m-%d %H:%M:%S")
        assert result == "2024-12-25 15:30:45"

        # Test special formats
        iso_result = time_api.time_format(t, "RFC3339")
        assert "2024-12-25T15:30:45" in iso_result

    def test_time_add(self) -> None:
        """Test adding seconds to time."""
        t = time_api.time_create(year=2024, month=12, day=25, hour=15)

        # Add 1 hour (3600 seconds)
        result = time_api.time_add(t, 3600)
        assert result["hour"] == 16

        # Subtract 1 day
        result = time_api.time_add(t, -86400)
        assert result["day"] == 24

    def test_time_add_duration(self) -> None:
        """Test adding duration with units."""
        t = time_api.time_create(year=2024, month=12, day=25, hour=15)

        # Test various units
        result = time_api.time_add_duration(t, 2, "hours")
        assert result["hour"] == 17

        result = time_api.time_add_duration(t, 3, "days")
        assert result["day"] == 28

        result = time_api.time_add_duration(t, -30, "minutes")
        assert result["hour"] == 14
        assert result["minute"] == 30


class TestTimeComponents:
    """Test time component extraction functions."""

    def test_time_components(self) -> None:
        """Test extracting individual components."""
        t = time_api.time_create(
            year=2024, month=12, day=25, hour=15, minute=30, second=45
        )

        assert time_api.time_year(t) == 2024
        assert time_api.time_month(t) == 12
        assert time_api.time_day(t) == 25
        assert time_api.time_hour(t) == 15
        assert time_api.time_minute(t) == 30
        assert time_api.time_second(t) == 45

    def test_time_weekday(self) -> None:
        """Test weekday extraction."""
        # 2024-12-25 is a Wednesday (2)
        t = time_api.time_create(year=2024, month=12, day=25)
        assert time_api.time_weekday(t) == 2

        # 2024-12-28 is a Saturday (5)
        t = time_api.time_create(year=2024, month=12, day=28)
        assert time_api.time_weekday(t) == 5


class TestTimeComparison:
    """Test time comparison functions."""

    def test_time_before_after_equal(self) -> None:
        """Test time comparison functions."""
        t1 = time_api.time_create(year=2024, month=12, day=25, hour=15)
        t2 = time_api.time_create(year=2024, month=12, day=25, hour=16)
        t3 = time_api.time_create(year=2024, month=12, day=25, hour=15)

        assert time_api.time_before(t1, t2)
        assert not time_api.time_before(t2, t1)
        assert not time_api.time_before(t1, t3)

        assert time_api.time_after(t2, t1)
        assert not time_api.time_after(t1, t2)
        assert not time_api.time_after(t1, t3)

        assert time_api.time_equal(t1, t3)
        assert not time_api.time_equal(t1, t2)

    def test_time_diff(self) -> None:
        """Test time difference calculation."""
        t1 = time_api.time_create(year=2024, month=12, day=25, hour=15)
        t2 = time_api.time_create(year=2024, month=12, day=25, hour=16)

        diff = time_api.time_diff(t2, t1)
        assert diff == 3600  # 1 hour in seconds

        diff = time_api.time_diff(t1, t2)
        assert diff == -3600


class TestDurationFunctions:
    """Test duration parsing and formatting."""

    def test_duration_parse(self) -> None:
        """Test parsing duration strings."""
        test_cases = [
            ("30s", 30),
            ("5m", 300),
            ("1h", 3600),
            ("1h30m", 5400),
            ("2d", 172800),
            ("1w", 604800),
            ("1w2d3h4m5s", 788645),
            ("1.5h", 5400),
            ("0.5m", 30),
        ]

        for duration_str, expected_seconds in test_cases:
            result = time_api.duration_parse(duration_str)
            assert result == expected_seconds

    def test_duration_parse_units(self) -> None:
        """Test parsing with various unit names."""
        test_cases = [
            ("1 second", 1),
            ("2 seconds", 2),
            ("1 minute", 60),
            ("2 minutes", 120),
            ("1 hour", 3600),
            ("2 hours", 7200),
            ("1 day", 86400),
            ("2 days", 172800),
            ("1 week", 604800),
            ("2 weeks", 1209600),
        ]

        for duration_str, expected_seconds in test_cases:
            result = time_api.duration_parse(duration_str)
            assert result == expected_seconds

    def test_duration_human(self) -> None:
        """Test formatting seconds as human-readable duration."""
        test_cases = [
            (0, "0s"),
            (30, "30s"),
            (90, "1m30s"),
            (3600, "1h"),
            (3660, "1h1m"),
            (5400, "1h30m"),
            (86400, "1d"),
            (90000, "1d1h"),
            (604800, "1w"),
            (788645, "1w2d3h4m5s"),
            (0.5, "0.5s"),
            (1.234, "1.234s"),
        ]

        for seconds, expected in test_cases:
            result = time_api.duration_human(seconds)
            assert result == expected

    def test_duration_human_negative(self) -> None:
        """Test formatting negative durations."""
        assert time_api.duration_human(-3600) == "-1h"
        assert time_api.duration_human(-90) == "-1m30s"


class TestTimezoneFunctions:
    """Test timezone-related functions."""

    def test_timezone_is_valid(self) -> None:
        """Test timezone validation."""
        # Valid timezones
        assert time_api.timezone_is_valid("UTC")
        assert time_api.timezone_is_valid("America/New_York")
        assert time_api.timezone_is_valid("Europe/London")
        assert time_api.timezone_is_valid("Asia/Tokyo")

        # Invalid timezones
        assert not time_api.timezone_is_valid("Invalid/Timezone")
        assert not time_api.timezone_is_valid("NotATimezone")

    def test_timezone_offset(self) -> None:
        """Test getting timezone offset."""
        # UTC should always be 0
        assert time_api.timezone_offset("UTC") == 0

        # Create a specific time to test offset
        summer_time = time_api.time_create(
            year=2024, month=7, day=1, hour=12, timezone_name="UTC"
        )
        winter_time = time_api.time_create(
            year=2024, month=12, day=1, hour=12, timezone_name="UTC"
        )

        # New York: EDT in summer (UTC-4), EST in winter (UTC-5)
        ny_summer_offset = time_api.timezone_offset("America/New_York", summer_time)
        ny_winter_offset = time_api.timezone_offset("America/New_York", winter_time)

        assert ny_summer_offset == -14400  # -4 hours
        assert ny_winter_offset == -18000  # -5 hours


class TestUtilityFunctions:
    """Test utility functions."""

    def test_is_between(self) -> None:
        """Test checking if time is between hours."""
        # Test with specific time
        morning = time_api.time_create(hour=9, minute=30)
        afternoon = time_api.time_create(hour=15, minute=30)
        night = time_api.time_create(hour=22, minute=30)

        # Normal range
        assert time_api.is_between(9, 17, morning)
        assert time_api.is_between(9, 17, afternoon)
        assert not time_api.is_between(9, 17, night)

        # Overnight range
        assert time_api.is_between(22, 6, night)
        assert not time_api.is_between(22, 6, morning)
        assert not time_api.is_between(22, 6, afternoon)

    def test_is_between_current_time(self) -> None:
        """Test is_between with current time."""
        # Just verify it doesn't crash
        result = time_api.is_between(0, 24)
        assert isinstance(result, bool)

    def test_is_weekend(self) -> None:
        """Test weekend detection."""
        # Monday
        monday = time_api.time_create(year=2024, month=12, day=23)
        assert not time_api.is_weekend(monday)

        # Saturday
        saturday = time_api.time_create(year=2024, month=12, day=28)
        assert time_api.is_weekend(saturday)

        # Sunday
        sunday = time_api.time_create(year=2024, month=12, day=29)
        assert time_api.is_weekend(sunday)

    def test_is_weekend_current_time(self) -> None:
        """Test is_weekend with current time."""
        # Just verify it doesn't crash
        result = time_api.is_weekend()
        assert isinstance(result, bool)


class TestErrorHandling:
    """Test error handling in time functions."""

    def test_invalid_timezone(self) -> None:
        """Test handling of invalid timezones."""
        # Should fall back to UTC with warning
        result = time_api.time_create(timezone_name="Invalid/Zone")
        assert result["timezone"] == "UTC"

    def test_parse_invalid_string(self) -> None:
        """Test parsing invalid time strings."""
        with pytest.raises(ValueError, match="Could not parse time string"):
            time_api.time_parse("not a date")

    def test_parse_invalid_duration(self) -> None:
        """Test parsing invalid duration strings."""
        with pytest.raises(ValueError, match="Could not parse duration"):
            time_api.duration_parse("invalid")

        with pytest.raises(ValueError, match="Unknown duration unit"):
            time_api.duration_parse("5 lightyears")

    def test_timezone_offset_invalid(self) -> None:
        """Test timezone offset with invalid timezone."""
        with pytest.raises(ValueError, match="Invalid timezone"):
            time_api.timezone_offset("Invalid/Zone")
