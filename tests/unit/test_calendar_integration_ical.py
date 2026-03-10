from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import pytest

from family_assistant import calendar_integration
from family_assistant.calendar_integration import fetch_upcoming_events, parse_event
from family_assistant.utils.clock import MockClock

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig


NSW_SCHOOL_2026_ICS = Path(
    "tests/fixtures/ical/nsw_school_calendar_2026.ics"
).read_text(encoding="utf-8")
AUSTRALIA_HOLIDAYS_ICS = Path("tests/fixtures/ical/australia_holidays.ics").read_text(
    encoding="utf-8"
)


@pytest.mark.asyncio
async def test_fetch_ical_events_parses_vevents_from_vcalendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(hours=1)
    ics_data = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyAssistant Test//EN",
        "BEGIN:VEVENT",
        "UID:test-vcalendar-event-1",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
        "SUMMARY:VCALENDAR Wrapped Event",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=ics_data)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/calendar.ics"]}
    }

    events = await fetch_upcoming_events(calendar_config, ZoneInfo("UTC"))

    assert len(events) == 1
    assert events[0]["uid"] == "test-vcalendar-event-1"
    assert events[0]["summary"] == "VCALENDAR Wrapped Event"


@pytest.mark.asyncio
async def test_fetch_ical_events_expands_recurring_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    start = now + timedelta(days=1)
    ics_data = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyAssistant Test//EN",
        "BEGIN:VEVENT",
        "UID:test-rrule-1",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        "DURATION:PT30M",
        "RRULE:FREQ=DAILY;COUNT=3",
        "SUMMARY:Recurring Event",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=ics_data)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/recurring.ics"]}
    }

    events = await fetch_upcoming_events(calendar_config, ZoneInfo("UTC"))

    recurring_events = [event for event in events if event["uid"] == "test-rrule-1"]
    assert len(recurring_events) == 3


@pytest.mark.asyncio
async def test_fetch_real_world_nsw_school_calendar_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=NSW_SCHOOL_2026_ICS)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/nsw-school.ics"]}
    }

    mock_clock = MockClock(
        initial_time=datetime(2026, 3, 10, 9, 0, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    )

    events = await fetch_upcoming_events(
        calendar_config,
        ZoneInfo("Australia/Sydney"),
        clock=mock_clock,
    )
    summaries = {event["summary"] for event in events}

    assert "NAPLAN Online Test Window (11-23 March)" in summaries
    assert "Term 1 Week 8 (10 Wk Term)" in summaries
    assert "Harmony Day" in summaries


@pytest.mark.asyncio
async def test_fetch_real_world_australia_holidays_calendar_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=AUSTRALIA_HOLIDAYS_ICS)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/australia-holidays.ics"]}
    }

    mock_clock = MockClock(
        initial_time=datetime(
            2025, 12, 31, 10, 0, 0, tzinfo=ZoneInfo("Australia/Sydney")
        )
    )

    events = await fetch_upcoming_events(
        calendar_config,
        ZoneInfo("Australia/Sydney"),
        clock=mock_clock,
    )
    summaries = {event["summary"] for event in events}

    assert "New Year's Day" in summaries
    assert "Day after New Year's Day" in summaries


@pytest.mark.asyncio
async def test_fetch_ical_events_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirect_flags: list[bool] = []
    ics_data = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyAssistant Test//EN",
        "BEGIN:VEVENT",
        "UID:test-redirect",
        "DTSTART:20260110T100000Z",
        "DTEND:20260110T110000Z",
        "SUMMARY:Redirect Event",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        follow_redirects = kwargs.get("follow_redirects")
        redirect_flags.append(follow_redirects is True)
        return httpx.Response(200, text=ics_data)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/redirect.ics"]}
    }

    mock_clock = MockClock(initial_time=datetime(2026, 1, 10, 8, 0, 0, tzinfo=UTC))

    events = await fetch_upcoming_events(
        calendar_config, ZoneInfo("UTC"), clock=mock_clock
    )

    assert len(events) == 1
    assert redirect_flags == [True]


@pytest.mark.asyncio
async def test_fetch_ical_events_continues_when_individual_event_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ics_data = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyAssistant Test//EN",
        "BEGIN:VEVENT",
        "UID:test-bad-1",
        "DTSTART:20260110T100000Z",
        "DTEND:20260110T110000Z",
        "SUMMARY:Bad Event",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:test-good-2",
        "DTSTART:20260111T100000Z",
        "DTEND:20260111T110000Z",
        "SUMMARY:Good Event",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=ics_data)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    original_parser = calendar_integration._parse_icalendar_event_component
    calls = {"count": 0}

    def flaky_parser(event_component: object, timezone: ZoneInfo) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("broken event")
        return original_parser(event_component, timezone)

    monkeypatch.setattr(
        calendar_integration,
        "_parse_icalendar_event_component",
        flaky_parser,
    )

    calendar_config: CalendarConfig = {
        "ical": {"urls": ["https://example.com/flaky.ics"]}
    }

    mock_clock = MockClock(initial_time=datetime(2026, 1, 10, 8, 0, 0, tzinfo=UTC))

    events = await fetch_upcoming_events(
        calendar_config, ZoneInfo("UTC"), clock=mock_clock
    )

    assert len(events) == 1
    assert events[0]["uid"] == "test-good-2"


def test_parse_event_accepts_vevent_component() -> None:
    vevent_data = "\r\n".join([
        "BEGIN:VEVENT",
        "UID:test-direct-vevent",
        "DTSTART:20260110T100000Z",
        "DTEND:20260110T110000Z",
        "SUMMARY:Direct VEVENT",
        "END:VEVENT",
        "",
    ])

    parsed = parse_event(vevent_data, timezone=ZoneInfo("UTC"))

    assert parsed is not None
    assert parsed["uid"] == "test-direct-vevent"
    assert parsed["summary"] == "Direct VEVENT"
