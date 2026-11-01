"""The eval's Google Calendar and Radicale-backed calendars."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import caldav
import httpx
import pytest

from family_assistant.eval.workhorse.calendar_sandbox import calendar_server
from family_assistant.eval.workhorse.calendar_world import (
    CalendarWorld,
    EventObservation,
    WorldCalendar,
    WorldEvent,
)
from family_assistant.eval.workhorse.fake_google_calendar import FakeGoogleCalendar

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.no_db

_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _event(uid: str, source: str, start: str, end: str, summary: str) -> WorldEvent:
    return WorldEvent(
        uid=uid,
        source_id=source,
        observations=[
            EventObservation(
                seen_at=_NOW,
                summary=summary,
                start=start,
                end=end,
                all_day=len(start) == 10,
            )
        ],
    )


_WORLD = CalendarWorld(
    calendars=[
        WorldCalendar(
            source_id="home", name="Home", kind="caldav", writable=True, is_default=True
        ),
        WorldCalendar(
            source_id="terms", name="School terms", kind="ical", writable=False
        ),
        WorldCalendar(
            source_id="google:primary",
            name="me@example.com",
            kind="google",
            writable=True,
            primary=True,
        ),
        WorldCalendar(
            source_id="google:shared@example.com",
            name="Shared",
            kind="google",
            writable=False,
        ),
    ],
    events=[
        _event(
            "dentist",
            "home",
            "2026-10-20T09:00:00+11:00",
            "2026-10-20T10:00:00+11:00",
            "Dentist",
        ),
        _event(
            "ballet",
            "home",
            "2026-09-26T09:10:00+10:00",
            "2026-09-26T10:40:00+10:00",
            "Ballet",
        ).model_copy(update={"occurrence_starts": ["2026-10-03T09:10:00+10:00"]}),
        _event("term4", "terms", "2026-10-13", "2026-12-19", "Term 4"),
        _event(
            "standup",
            "google:primary",
            "2026-10-02T09:00:00+10:00",
            "2026-10-02T09:15:00+10:00",
            "Standup",
        ),
        _event(
            "later",
            "google:primary",
            "2026-11-02T09:00:00+11:00",
            "2026-11-02T10:00:00+11:00",
            "Later",
        ),
        _event(
            "oncall", "google:shared@example.com", "2026-10-03", "2026-10-04", "On call"
        ),
    ],
)


def test_calendar_list_has_the_google_calendars_only() -> None:
    fake = FakeGoogleCalendar(world=_WORLD, now=_NOW)

    response = fake.handle("GET", "users/me/calendarList", {}, None)

    assert response is not None
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == {"me@example.com", "shared@example.com"}
    assert items["me@example.com"]["primary"] is True
    assert items["me@example.com"]["accessRole"] == "owner"
    assert items["shared@example.com"]["accessRole"] == "reader"


def test_events_are_listed_by_time_range_in_order() -> None:
    fake = FakeGoogleCalendar(world=_WORLD, now=_NOW)

    response = fake.handle(
        "GET",
        "calendars/primary/events",
        {"timeMin": "2026-10-01T00:00:00Z", "timeMax": "2026-10-31T00:00:00Z"},
        None,
    )

    assert response is not None
    assert [item["summary"] for item in response.json()["items"]] == ["Standup"]


def test_writes_change_the_trials_copy_and_are_recorded() -> None:
    fake = FakeGoogleCalendar(world=_WORLD, now=_NOW)

    created = fake.handle(
        "POST",
        "calendars/primary/events",
        {"sendUpdates": "none"},
        json.dumps({
            "summary": "Haircut",
            "start": {"dateTime": "2026-10-05T10:00:00+11:00"},
            "end": {"dateTime": "2026-10-05T10:30:00+11:00"},
        }).encode(),
    )
    assert created is not None
    event_id = created.json()["id"]
    fake.handle(
        "PATCH",
        f"calendars/primary/events/{event_id}",
        {},
        json.dumps({"summary": "Haircut (moved)"}).encode(),
    )
    fetched = fake.handle("GET", f"calendars/primary/events/{event_id}", {}, None)
    deleted = fake.handle("DELETE", "calendars/primary/events/standup", {}, None)

    assert fetched is not None and fetched.json()["summary"] == "Haircut (moved)"
    assert deleted is not None and deleted.status_code == 204
    assert [w["kind"] for w in fake.writes] == [
        "calendar_insert",
        "calendar_patch",
        "calendar_delete",
    ]


def test_caldav_calendar_and_feeds_are_served_by_radicale(tmp_path: Path) -> None:
    with calendar_server(_WORLD, _NOW, tmp_path) as server:
        caldav_config = server.config.caldav
        ical_config = server.config.ical
        assert caldav_config is not None and ical_config is not None
        (home,) = caldav_config.calendar_urls
        assert not isinstance(home, str)
        assert (home.id, home.name, home.default) == ("home", "Home", True)
        password = caldav_config.password
        assert password is not None
        client = caldav.DAVClient(
            url=home.url,
            username=caldav_config.username,
            password=password.get_secret_value(),
        )
        calendar = client.calendar(url=home.url)
        events = calendar.events()
        # A time-range search is what the calendar tools issue, and it is
        # what fails if event times carry a zone the server cannot resolve.
        in_range = calendar.search(
            start=datetime(2026, 10, 19, tzinfo=UTC),
            end=datetime(2026, 10, 21, tzinfo=UTC),
            event=True,
        )
        # A later occurrence of a series that reuses its UID.
        later_ballet = calendar.search(
            start=datetime(2026, 10, 2, tzinfo=UTC),
            end=datetime(2026, 10, 4, tzinfo=UTC),
            event=True,
        )
        (feed,) = ical_config.urls
        assert not isinstance(feed, str)
        body = httpx.get(feed.url).text

    assert sorted(str(e.icalendar_component["summary"]) for e in events) == [
        "Ballet",
        "Dentist",
    ]
    assert [str(e.icalendar_component["summary"]) for e in in_range] == ["Dentist"]
    assert [str(e.icalendar_component["summary"]) for e in later_ballet] == ["Ballet"]
    assert "SUMMARY:Term 4" in body
