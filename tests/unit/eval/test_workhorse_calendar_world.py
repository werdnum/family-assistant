"""Rebuilding the household's calendars from recorded calendar tool results."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from family_assistant.eval.workhorse.calendar_world import build_calendar_world
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.storage.types import MessageHistoryRow

pytestmark = pytest.mark.no_db

_SYDNEY = ZoneInfo("Australia/Sydney")
_T = datetime(2026, 10, 1, tzinfo=UTC)

_LISTING = """Available calendars:
- abc123: abc123 (CalDAV, writable) [default for new events]
- tripit: Tripit (iCal feed, read-only)
- google:primary: me@example.com (Google Calendar, writable, the user's primary Google calendar)
- google:work@example.com: Work Days (Google Calendar, writable, hidden in Google Calendar; searched only when named)"""

_SEARCH = """Found 2 event(s):

1. Dentist (match: 0.91)
   Start: 2026-10-20 09:00 AEDT
   End: 2026-10-20 10:00 AEDT
   UID: dentist-1
   Calendar: https://caldav.example.com/calendars/abc123
   Source ID: abc123

2. Term 4 Week 2
   Start: 2026-10-19
   End: 2026-10-24
   UID: term-4-2
   Calendar: School terms (read-only)
   Source ID: tripit"""


def _row(
    index: int,
    role: str,
    at: datetime,
    *,
    content: str | None = None,
    tool_calls: list[ToolCallItem] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> MessageHistoryRow:
    return MessageHistoryRow(
        internal_id=index,
        interface_type="web",
        conversation_id="c1",
        interface_message_id=None,
        turn_id="t1",
        thread_root_id=None,
        timestamp=at,
        role=role,
        content=content,
        tool_calls=tool_calls,
        reasoning_info=None,
        tool_call_id=tool_call_id,
        error_traceback=None,
        processing_profile_id="default_assistant",
        subconversation_id=None,
        user_id="andrew",
        attachments=None,
        tool_name=tool_name,
        provider_metadata=None,
        taint_metadata_json=None,
        taint_metadata_version=None,
        taint_metadata=None,
        is_internal=False,
        echoed_from_conversation_id=None,
        moved_to_conversation_id=None,
    )


def _history(
    calls: list[tuple[datetime, str, dict[str, object], str]],
) -> dict[tuple[str, str], list[MessageHistoryRow]]:
    rows: list[MessageHistoryRow] = []
    for index, (at, name, arguments, result) in enumerate(calls):
        call = ToolCallItem(
            id=f"call-{index}",
            type="function",
            function=ToolCallFunction(name=name, arguments=arguments),
        )
        rows.append(_row(index * 2, "assistant", at, tool_calls=[call]))
        rows.append(
            _row(
                index * 2 + 1,
                "tool",
                at,
                content=result,
                tool_call_id=call.id,
                tool_name=name,
            )
        )
    return {("web", "c1"): rows}


def test_calendars_come_from_the_latest_listing() -> None:
    world = build_calendar_world(
        _history([(_T, "list_calendars", {}, _LISTING)]), timezone=_SYDNEY
    )

    by_id = {c.source_id: c for c in world.calendars}
    assert set(by_id) == {
        "abc123",
        "tripit",
        "google:primary",
        "google:work@example.com",
    }
    assert by_id["abc123"].kind == "caldav"
    assert by_id["abc123"].is_default
    assert by_id["tripit"].kind == "ical"
    assert not by_id["tripit"].writable
    assert by_id["google:primary"].primary
    assert by_id["google:work@example.com"].hidden


def test_searched_events_keep_their_times_and_calendar() -> None:
    world = build_calendar_world(
        _history([(_T, "search_calendar_events", {"search_text": "dentist"}, _SEARCH)]),
        timezone=_SYDNEY,
    )

    events = {e.uid: e for e in world.events}
    dentist = events["dentist-1"].as_of(_T)
    assert dentist is not None
    assert dentist.summary == "Dentist"
    assert dentist.start == "2026-10-20T09:00:00+11:00"
    assert not dentist.all_day
    assert events["dentist-1"].matched_queries == ["dentist"]
    term = events["term-4-2"].as_of(_T)
    assert term is not None
    assert (term.start, term.end, term.all_day) == ("2026-10-19", "2026-10-24", True)
    assert events["term-4-2"].source_id == "tripit"


def test_an_event_exists_between_its_creation_and_its_deletion() -> None:
    created = {
        "summary": "Gardeners",
        "start_time": "2026-10-13",
        "end_time": "2026-10-14",
        "all_day": True,
    }
    world = build_calendar_world(
        _history([
            (_T, "list_calendars", {}, _LISTING),
            (
                _T,
                "add_calendar_event",
                created,
                "OK. Event 'Gardeners' added (UID: g-1).",
            ),
            (
                _T + timedelta(days=5),
                "delete_calendar_event",
                {"uid": "g-1"},
                "OK. Event 'Gardeners' deleted from calendar.",
            ),
        ]),
        timezone=_SYDNEY,
    )

    (event,) = world.events
    assert event.source_id == "abc123"
    assert event.as_of(_T - timedelta(hours=1)) is None
    assert event.as_of(_T + timedelta(days=1)) is not None
    assert event.as_of(_T + timedelta(days=6)) is None


def test_a_renamed_event_shows_its_name_at_the_time() -> None:
    renamed = _SEARCH.replace("1. Dentist (match: 0.91)", "1. Dentist (moved)")
    world = build_calendar_world(
        _history([
            (_T, "search_calendar_events", {}, _SEARCH),
            (_T + timedelta(days=3), "search_calendar_events", {}, renamed),
        ]),
        timezone=_SYDNEY,
    )

    dentist = next(e for e in world.events if e.uid == "dentist-1")
    before = dentist.as_of(_T + timedelta(days=1))
    after = dentist.as_of(_T + timedelta(days=4))
    assert before is not None and before.summary == "Dentist"
    assert after is not None and after.summary == "Dentist (moved)"


def test_failed_and_unparseable_results_are_ignored() -> None:
    world = build_calendar_world(
        _history([
            (_T, "search_calendar_events", {}, "Error: No calendars configured."),
            (_T, "add_calendar_event", {"summary": "No time"}, "OK."),
        ]),
        timezone=_SYDNEY,
    )

    assert world.events == []
    assert json.loads(world.model_dump_json())["calendars"] == []


def _ballet(start: str, end: str) -> str:
    return f"""Found 1 event(s):

1. Ballet
   Start: {start}
   End: {end}
   UID: ballet-series
   Calendar: https://caldav.example.com/calendars/abc123
   Source ID: abc123"""


def test_a_weekly_occurrence_is_part_of_the_series_not_a_move() -> None:
    world = build_calendar_world(
        _history([
            (
                _T,
                "search_calendar_events",
                {},
                _ballet("2026-09-26 09:10 AEST", "2026-09-26 10:40 AEST"),
            ),
            (
                _T,
                "search_calendar_events",
                {},
                _ballet("2026-10-03 09:10 AEST", "2026-10-03 10:40 AEST"),
            ),
        ]),
        timezone=_SYDNEY,
    )

    (series,) = world.events
    first = series.as_of(_T)
    assert first is not None and first.start.startswith("2026-09-26")
    assert [s[:10] for s in series.occurrence_starts] == ["2026-10-03"]


def test_a_moved_appointment_is_an_edit() -> None:
    moved = _SEARCH.replace("2026-10-20 09:00 AEDT", "2026-10-22 12:30 AEDT").replace(
        "2026-10-20 10:00 AEDT", "2026-10-22 13:30 AEDT"
    )
    world = build_calendar_world(
        _history([
            (_T, "search_calendar_events", {}, _SEARCH),
            (_T + timedelta(days=1), "search_calendar_events", {}, moved),
        ]),
        timezone=_SYDNEY,
    )

    dentist = next(e for e in world.events if e.uid == "dentist-1")
    after = dentist.as_of(_T + timedelta(days=2))
    assert dentist.occurrence_starts == []
    assert after is not None and after.start.startswith("2026-10-22T12:30")


def test_a_creation_without_a_uid_is_the_event_later_seen_with_one() -> None:
    created: dict[str, object] = {
        "summary": "Dentist",
        "start_time": "2026-10-20T09:00:00+11:00",
        "end_time": "2026-10-20T10:00:00+11:00",
    }
    world = build_calendar_world(
        _history([
            (_T, "list_calendars", {}, _LISTING),
            (
                _T,
                "add_calendar_event",
                created,
                "OK. Event 'Dentist' added to the calendar.",
            ),
            (_T + timedelta(days=1), "search_calendar_events", {}, _SEARCH),
        ]),
        timezone=_SYDNEY,
    )

    dentist = [e for e in world.events if e.observations[0].summary == "Dentist"]
    assert [e.uid for e in dentist] == ["dentist-1"]
    assert dentist[0].created_at == _T
    assert dentist[0].as_of(_T - timedelta(hours=1)) is None
