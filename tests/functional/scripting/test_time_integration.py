"""Integration tests for time API in Starlark scripts."""

from datetime import datetime

import pytest

from family_assistant.scripting.engine import StarlarkEngine


class TestTimeAPIIntegration:
    """Test time API integration with Starlark engine."""

    def test_time_functions_in_script(self) -> None:
        """Test using time functions in a Starlark script."""
        engine = StarlarkEngine()

        script = """
# Get current time
now = time_now()
now_utc = time_now_utc()

# Create a specific time
xmas = time_create(year=2024, month=12, day=25, hour=15, minute=30)

# Format it
formatted = time_format(xmas, "%Y-%m-%d %H:%M:%S")

# Add duration
tomorrow = time_add(xmas, DAY)

# Check components
year = time_year(xmas)
month = time_month(xmas)
day = time_day(xmas)

result = {
    "now_year": time_year(now),
    "utc_tz": now_utc["timezone"],
    "xmas_formatted": formatted,
    "xmas_year": year,
    "xmas_month": month,
    "xmas_day": day,
    "tomorrow_day": time_day(tomorrow),
}

# Return the result
result
"""
        result = engine.evaluate(script)

        # Verify results
        assert result["now_year"] == datetime.now().year
        assert result["utc_tz"] == "UTC"
        assert result["xmas_formatted"] == "2024-12-25 15:30:00"
        assert result["xmas_year"] == 2024
        assert result["xmas_month"] == 12
        assert result["xmas_day"] == 25
        assert result["tomorrow_day"] == 26

    def test_timezone_operations(self) -> None:
        """Test timezone operations in scripts."""
        engine = StarlarkEngine()

        script = """
# Create UTC time
utc_time = time_create(year=2024, month=12, day=25, hour=15, timezone_name="UTC")

# Convert to different timezones
ny_time = time_in_location(utc_time, "America/New_York")
tokyo_time = time_in_location(utc_time, "Asia/Tokyo")

# Check timezone validity
valid_tz = timezone_is_valid("Europe/London")
invalid_tz = timezone_is_valid("Fake/Timezone")

result = {
    "utc_hour": time_hour(utc_time),
    "ny_hour": time_hour(ny_time),
    "tokyo_hour": time_hour(tokyo_time),
    "london_valid": valid_tz,
    "fake_valid": invalid_tz,
}

# Return the result
result
"""
        result = engine.evaluate(script)

        assert result["utc_hour"] == 15
        assert result["ny_hour"] == 10  # UTC-5 in winter
        assert result["tokyo_hour"] == 0  # Next day, UTC+9
        assert result["london_valid"] is True
        assert result["fake_valid"] is False

    def test_duration_operations(self) -> None:
        """Test duration parsing and arithmetic."""
        engine = StarlarkEngine()

        script = """
# Parse durations
one_hour = duration_parse("1h")
ninety_min = duration_parse("1h30m")
two_days = duration_parse("2d")

# Duration arithmetic
total = one_hour + ninety_min  # Just number addition

# Format durations
human_90m = duration_human(ninety_min)
human_2d = duration_human(two_days)

# Use duration constants
three_hours = 3 * HOUR
formatted_3h = duration_human(three_hours)

result = {
    "one_hour": one_hour,
    "ninety_min": ninety_min,
    "total_seconds": total,
    "human_90m": human_90m,
    "human_2d": human_2d,
    "three_hours": three_hours,
    "formatted_3h": formatted_3h,
}

# Return the result
result
"""
        result = engine.evaluate(script)

        assert result["one_hour"] == 3600
        assert result["ninety_min"] == 5400
        assert result["total_seconds"] == 9000
        assert result["human_90m"] == "1h30m"
        assert result["human_2d"] == "2d"
        assert result["three_hours"] == 10800
        assert result["formatted_3h"] == "3h"

    def test_time_comparisons(self) -> None:
        """Test time comparison operations."""
        engine = StarlarkEngine()

        script = """
# Create times
t1 = time_create(year=2024, month=12, day=25, hour=10)
t2 = time_create(year=2024, month=12, day=25, hour=15)
t3 = time_create(year=2024, month=12, day=26, hour=10)

# Compare times
before = time_before(t1, t2)
after = time_after(t2, t1)
equal = time_equal(t1, t1)
not_equal = time_equal(t1, t2)

# Calculate differences
diff_hours = time_diff(t2, t1)
diff_days = time_diff(t3, t1)

result = {
    "before": before,
    "after": after,
    "equal": equal,
    "not_equal": not_equal,
    "diff_hours": diff_hours,
    "diff_days": diff_days,
    "diff_hours_human": duration_human(diff_hours),
    "diff_days_human": duration_human(diff_days),
}

# Return the result
result
"""
        result = engine.evaluate(script)

        assert result["before"] is True
        assert result["after"] is True
        assert result["equal"] is True
        assert result["not_equal"] is False
        assert result["diff_hours"] == 18000  # 5 hours
        assert result["diff_days"] == 86400  # 1 day
        assert result["diff_hours_human"] == "5h"
        assert result["diff_days_human"] == "1d"

    def test_utility_functions(self) -> None:
        """Test utility functions like is_between and is_weekend."""
        engine = StarlarkEngine()

        script = """
# Create times for testing
morning = time_create(year=2024, month=12, day=23, hour=9)  # Monday morning
evening = time_create(year=2024, month=12, day=23, hour=20) # Monday evening
saturday = time_create(year=2024, month=12, day=28)         # Saturday

# Test is_between
morning_work = is_between(8, 17, morning)
evening_work = is_between(8, 17, evening)
evening_night = is_between(18, 23, evening)

# Test is_weekend
monday_weekend = is_weekend(morning)
saturday_weekend = is_weekend(saturday)

# Test weekday
monday_day = time_weekday(morning)
saturday_day = time_weekday(saturday)

result = {
    "morning_work": morning_work,
    "evening_work": evening_work,
    "evening_night": evening_night,
    "monday_weekend": monday_weekend,
    "saturday_weekend": saturday_weekend,
    "monday_day": monday_day,
    "saturday_day": saturday_day,
}

# Return the result
result
"""
        result = engine.evaluate(script)

        assert result["morning_work"] is True
        assert result["evening_work"] is False
        assert result["evening_night"] is True
        assert result["monday_weekend"] is False
        assert result["saturday_weekend"] is True
        assert result["monday_day"] == 0  # Monday
        assert result["saturday_day"] == 5  # Saturday

    def test_real_world_automation_example(self) -> None:
        """Test a realistic automation script using time functions."""
        engine = StarlarkEngine()

        script = """
# Automation: Check if it's time for a reminder
def should_send_reminder(event_time_str):
    # Parse the event time
    event_time = time_parse(event_time_str, "%Y-%m-%d %H:%M:%S")
    
    # Get current time
    now = time_now()
    
    # Calculate time until event
    time_until = time_diff(event_time, now)
    
    # Send reminder if event is within 24 hours but not past
    if time_until > 0 and time_until <= DAY:
        return True, duration_human(time_until)
    return False, ""

# Test with a future event
future_time = time_add(time_now(), HOUR * 12)  # 12 hours from now
future_str = time_format(future_time, "%Y-%m-%d %H:%M:%S")

should_remind, time_left = should_send_reminder(future_str)

# Business hours check
def is_business_hours():
    now = time_now()
    # Monday-Friday, 9 AM - 5 PM
    if is_weekend(now):
        return False
    return is_between(9, 17, now)

# Calendar math - find next Monday
def next_monday():
    today = time_now()
    weekday = time_weekday(today)
    
    # Calculate days until Monday (0)
    if weekday == 0:  # Today is Monday
        days_ahead = 7
    else:
        days_ahead = (7 - weekday) % 7
    
    return time_add(today, days_ahead * DAY)

next_mon = next_monday()

result = {
    "should_remind": should_remind,
    "time_left": time_left,
    "is_business_hours": is_business_hours(),
    "next_monday_day": time_day(next_mon),
    "next_monday_weekday": time_weekday(next_mon),
}

# Return the result
result
"""
        result = engine.evaluate(script)

        # Verify the reminder logic works
        assert result["should_remind"] is True
        assert "h" in result["time_left"]  # Should show hours

        # Business hours check depends on when test runs
        assert isinstance(result["is_business_hours"], bool)

        # Next Monday should be a Monday (weekday 0)
        assert result["next_monday_weekday"] == 0

    def test_time_parsing_formats(self) -> None:
        """Test various time parsing formats."""
        engine = StarlarkEngine()

        script = """
# Define a function to work around for-loop restriction
def parse_and_check_times():
    times = []
    
    # ISO formats
    times.append(time_parse("2024-12-25T15:30:45Z"))
    times.append(time_parse("2024-12-25T15:30:45+05:00"))
    
    # Common formats
    times.append(time_parse("2024-12-25 15:30:45"))
    times.append(time_parse("25/12/2024"))
    
    # Custom format
    times.append(time_parse("Dec 25, 2024 3:30 PM", "%b %d, %Y %I:%M %p"))
    
    # Extract years to verify parsing worked
    years = []
    for t in times:
        years.append(time_year(t))
    
    # Test timezone preservation
    tz_time = time_parse("2024-12-25T15:30:45", timezone_name="Europe/Paris")
    paris_tz = tz_time["timezone"]
    
    # Check if all years are 2024
    all_2024 = True
    for y in years:
        if y != 2024:
            all_2024 = False
            break
    
    return {
        "years": years,
        "paris_tz": paris_tz,
        "all_2024": all_2024,
    }

# Call the function and return result
parse_and_check_times()
"""
        result = engine.evaluate(script)

        assert result["all_2024"] is True
        assert len(result["years"]) == 5
        assert "Europe/Paris" in result["paris_tz"]

    @pytest.mark.asyncio
    async def test_time_api_with_async_evaluation(self) -> None:
        """Test time API works with async script evaluation."""
        engine = StarlarkEngine()

        script = """
# Simple time operation
now = time_now()
tomorrow = time_add(now, DAY)
diff = time_diff(tomorrow, now)
diff
"""
        result = await engine.evaluate_async(script)

        assert result == 86400  # 1 day in seconds
