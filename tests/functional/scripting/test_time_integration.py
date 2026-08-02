"""Integration tests for time API in scripting engines."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from family_assistant.scripting.errors import ScriptExecutionError
from family_assistant.scripting.monty_engine import MontyEngine
from family_assistant.storage.database import Database
from family_assistant.tools.types import ToolExecutionContext


class TestTimeAPIIntegration:
    """Test time API integration with both engines."""

    @pytest.mark.asyncio
    async def test_time_functions_in_script(self, engine_class: type) -> None:
        """Test using time functions in a script."""
        engine = engine_class()

        script = """
now = time_now()
now_utc = time_now_utc()
xmas = time_create(year=2024, month=12, day=25, hour=15, minute=30)
formatted = time_format(xmas, "%Y-%m-%d %H:%M:%S")
tomorrow = time_add(xmas, DAY)
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
result
"""
        result = await engine.evaluate_async(script)

        assert result["now_year"] == datetime.now(UTC).year
        assert result["utc_tz"] == "UTC"
        assert result["xmas_formatted"] == "2024-12-25 15:30:00"
        assert result["xmas_year"] == 2024
        assert result["xmas_month"] == 12
        assert result["xmas_day"] == 25
        assert result["tomorrow_day"] == 26

    @pytest.mark.asyncio
    async def test_timezone_operations(self, engine_class: type) -> None:
        """Test timezone operations in scripts."""
        engine = engine_class()

        script = """
utc_time = time_create(year=2024, month=12, day=25, hour=15, timezone_name="UTC")
ny_time = time_in_location(utc_time, "America/New_York")
tokyo_time = time_in_location(utc_time, "Asia/Tokyo")
valid_tz = timezone_is_valid("Europe/London")
invalid_tz = timezone_is_valid("Fake/Timezone")

result = {
    "utc_hour": time_hour(utc_time),
    "ny_hour": time_hour(ny_time),
    "tokyo_hour": time_hour(tokyo_time),
    "london_valid": valid_tz,
    "fake_valid": invalid_tz,
}
result
"""
        result = await engine.evaluate_async(script)

        assert result["utc_hour"] == 15
        assert result["ny_hour"] == 10  # UTC-5 in winter
        assert result["tokyo_hour"] == 0  # Next day, UTC+9
        assert result["london_valid"] is True
        assert result["fake_valid"] is False

    @pytest.mark.asyncio
    async def test_duration_operations(self, engine_class: type) -> None:
        """Test duration parsing and arithmetic."""
        engine = engine_class()

        script = """
one_hour = duration_parse("1h")
ninety_min = duration_parse("1h30m")
two_days = duration_parse("2d")
total = one_hour + ninety_min
human_90m = duration_human(ninety_min)
human_2d = duration_human(two_days)
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
result
"""
        result = await engine.evaluate_async(script)

        assert result["one_hour"] == 3600
        assert result["ninety_min"] == 5400
        assert result["total_seconds"] == 9000
        assert result["human_90m"] == "1h30m"
        assert result["human_2d"] == "2d"
        assert result["three_hours"] == 10800
        assert result["formatted_3h"] == "3h"

    @pytest.mark.asyncio
    async def test_time_comparisons(self, engine_class: type) -> None:
        """Test time comparison operations."""
        engine = engine_class()

        script = """
t1 = time_create(year=2024, month=12, day=25, hour=10)
t2 = time_create(year=2024, month=12, day=25, hour=15)
t3 = time_create(year=2024, month=12, day=26, hour=10)

result = {
    "before": time_before(t1, t2),
    "after": time_after(t2, t1),
    "equal": time_equal(t1, t1),
    "not_equal": time_equal(t1, t2),
    "diff_hours": time_diff(t2, t1),
    "diff_days": time_diff(t3, t1),
    "diff_hours_human": duration_human(time_diff(t2, t1)),
    "diff_days_human": duration_human(time_diff(t3, t1)),
}
result
"""
        result = await engine.evaluate_async(script)

        assert result["before"] is True
        assert result["after"] is True
        assert result["equal"] is True
        assert result["not_equal"] is False
        assert result["diff_hours"] == 18000
        assert result["diff_days"] == 86400
        assert result["diff_hours_human"] == "5h"
        assert result["diff_days_human"] == "1d"

    @pytest.mark.asyncio
    async def test_utility_functions(self, engine_class: type) -> None:
        """Test utility functions like is_between and is_weekend."""
        engine = engine_class()

        script = """
morning = time_create(year=2024, month=12, day=23, hour=9)
evening = time_create(year=2024, month=12, day=23, hour=20)
saturday = time_create(year=2024, month=12, day=28)

result = {
    "morning_work": is_between(8, 17, morning),
    "evening_work": is_between(8, 17, evening),
    "evening_night": is_between(18, 23, evening),
    "monday_weekend": is_weekend(morning),
    "saturday_weekend": is_weekend(saturday),
    "monday_day": time_weekday(morning),
    "saturday_day": time_weekday(saturday),
}
result
"""
        result = await engine.evaluate_async(script)

        assert result["morning_work"] is True
        assert result["evening_work"] is False
        assert result["evening_night"] is True
        assert result["monday_weekend"] is False
        assert result["saturday_weekend"] is True
        assert result["monday_day"] == 0
        assert result["saturday_day"] == 5

    @pytest.mark.asyncio
    async def test_real_world_automation_example(self, engine_class: type) -> None:
        """Test a realistic automation script using time functions."""
        engine = engine_class()

        script = """
def should_send_reminder(event_time_str):
    event_time = time_parse(event_time_str, "%Y-%m-%d %H:%M:%S")
    now = time_now()
    time_until = time_diff(event_time, now)
    if time_until > 0 and time_until <= DAY:
        return True, duration_human(time_until)
    return False, ""

future_time = time_add(time_now(), HOUR * 12)
future_str = time_format(future_time, "%Y-%m-%d %H:%M:%S")
should_remind, time_left = should_send_reminder(future_str)

def is_business_hours():
    now = time_now()
    if is_weekend(now):
        return False
    return is_between(9, 17, now)

def next_monday():
    today = time_now()
    weekday = time_weekday(today)
    if weekday == 0:
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
result
"""
        result = await engine.evaluate_async(script)

        assert result["should_remind"] is True
        assert "h" in result["time_left"]
        assert isinstance(result["is_business_hours"], bool)
        assert result["next_monday_weekday"] == 0

    @pytest.mark.asyncio
    async def test_time_parsing_formats(self, engine_class: type) -> None:
        """Test various time parsing formats."""
        engine = engine_class()

        script = """
def parse_and_check_times():
    times = []
    times.append(time_parse("2024-12-25T15:30:45Z"))
    times.append(time_parse("2024-12-25T15:30:45+05:00"))
    times.append(time_parse("2024-12-25 15:30:45"))
    times.append(time_parse("25/12/2024"))
    times.append(time_parse("Dec 25, 2024 3:30 PM", "%b %d, %Y %I:%M %p"))

    years = []
    for t in times:
        years.append(time_year(t))

    tz_time = time_parse("2024-12-25T15:30:45", timezone_name="Europe/Paris")
    paris_tz = tz_time["timezone"]

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

parse_and_check_times()
"""
        result = await engine.evaluate_async(script)

        assert result["all_2024"] is True
        assert len(result["years"]) == 5
        assert "Europe/Paris" in result["paris_tz"]

    @pytest.mark.asyncio
    async def test_time_api_with_async_evaluation(self, engine_class: type) -> None:
        """Test time API works with async script evaluation."""
        engine = engine_class()

        script = """
now = time_now()
tomorrow = time_add(now, DAY)
diff = time_diff(tomorrow, now)
diff
"""
        result = await engine.evaluate_async(script)
        assert result == 86400


class TestTimeAPIHonorsContextTimezone:
    """Verify that time_now / time_from_timestamp use the configured timezone.

    Regression test for the bug where ``time_now()`` returned naive local
    machine time mislabelled as UTC, ignoring the assistant's configured
    ``processing_config.timezone``.
    """

    @pytest.mark.asyncio
    async def test_time_now_uses_context_timezone(self, db_engine: object) -> None:
        script = """
now = time_now()
ts = time_from_timestamp(1704067200)
result = {
    "now_tz": now["timezone"],
    "ts_tz": ts["timezone"],
    "ts_hour": time_hour(ts),
    "ts_unix": ts["unix"],
}
result
"""
        db = Database(engine=db_engine)  # type: ignore[arg-type]
        context = ToolExecutionContext(
            interface_type="test",
            conversation_id="tz-test",
            user_name="tester",
            turn_id="turn-tz",
            db_context=db,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            camera_backend=None,
            timezone=ZoneInfo("America/New_York"),
            credential_resolvers=None,
            api_backend=None,
        )

        engine = MontyEngine(default_timezone=ZoneInfo("Australia/Sydney"))
        result = await engine.evaluate_async(script, execution_context=context)

        assert "America/New_York" in result["now_tz"]
        assert "America/New_York" in result["ts_tz"]
        # 2024-01-01 00:00:00 UTC is 2023-12-31 19:00:00 EST (UTC-5)
        assert result["ts_hour"] == 19
        # Unix timestamp is an absolute instant and remains unchanged
        assert result["ts_unix"] == 1704067200

    @pytest.mark.asyncio
    async def test_time_now_uses_default_timezone(self, engine_class: type) -> None:
        """Without an exec context, time_now uses the engine's default timezone."""
        engine = engine_class()
        script = """
now = time_now()
now["timezone"]
"""
        result = await engine.evaluate_async(script)
        assert result == "Australia/Sydney"

    @pytest.mark.asyncio
    async def test_no_timezone_raises_error(self) -> None:
        """Without any timezone, time API raises an error instead of silently using UTC."""
        engine = MontyEngine()
        with pytest.raises(ScriptExecutionError, match="no timezone was provided"):
            await engine.evaluate_async("time_now()")

    @pytest.mark.asyncio
    async def test_script_can_override_with_timezone_string(
        self, engine_class: type
    ) -> None:
        """Scripts must be able to override tz with a timezone NAME string.

        Scripts run inside the Monty sandbox and cannot construct ZoneInfo
        objects, so time_now(tz=...) and time_from_timestamp(tz=...) must
        accept the same kind of name strings the rest of the time API
        already takes (e.g. ``"Europe/London"``). Regression guard for the
        Codex review comment on PR #749 which flagged that passing a string
        would previously crash with TypeError.
        """
        engine = engine_class()
        script = """
now = time_now("Europe/London")
ts = time_from_timestamp(1704067200, tz="Asia/Tokyo")
{
    "now_tz": now["timezone"],
    "ts_tz": ts["timezone"],
    "ts_hour": time_hour(ts),
}
"""
        result = await engine.evaluate_async(script)
        assert "Europe/London" in result["now_tz"]
        assert "Asia/Tokyo" in result["ts_tz"]
        # 2024-01-01 00:00:00 UTC is 2024-01-01 09:00:00 JST (UTC+9)
        assert result["ts_hour"] == 9
