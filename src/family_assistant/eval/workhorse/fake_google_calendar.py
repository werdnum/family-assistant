"""Google Calendar for the eval's fake Google, served from a :class:`CalendarWorld`.

Answers the Calendar v3 paths the shipped calendar code calls: the calendar
list, events in a time range, one event, and insert, patch and delete. The
Google calendars are the world's Google sources; each event is as it stood
at the trial's moment, and writes change the trial's own copy and are
recorded, never sent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from urllib.parse import unquote

from family_assistant.services.api_backend import ApiResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.eval.workhorse.calendar_world import CalendarWorld

__all__ = ["CALENDAR_PREFIX", "FakeGoogleCalendar"]

CALENDAR_PREFIX = "/calendar/v3/"
_PRIMARY_SOURCE = "google:primary"


def _json(status: int, payload: object) -> ApiResponse:
    return ApiResponse(status_code=status, content=json.dumps(payload).encode("utf-8"))


def _not_found() -> ApiResponse:
    return _json(404, {"error": {"code": 404, "message": "Not Found"}})


def _time_value(value: str) -> dict[str, str]:
    return {"date": value} if len(value) == 10 else {"dateTime": value}


def _instant(value: object) -> datetime | None:
    """The start of a Google ``start``/``end`` value, as an aware instant."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("dateTime"), str):
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(value.get("date"), str):
        return datetime.combine(date.fromisoformat(value["date"]), time(), tzinfo=UTC)
    return None


@dataclass
class _Calendar:
    google_id: str
    name: str
    writable: bool
    hidden: bool
    primary: bool


@dataclass
class FakeGoogleCalendar:
    """The Google calendars of ``world``, as of ``now``."""

    world: CalendarWorld
    now: datetime
    writes: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._calendars: dict[str, _Calendar] = {}
        self._by_source: dict[str, str] = {}
        for calendar in self.world.calendars:
            if calendar.kind != "google":
                continue
            # The primary calendar is listed under the account's address; the
            # world names it by the address as its display name.
            google_id = (
                calendar.name
                if calendar.primary
                else calendar.source_id.removeprefix("google:")
            )
            self._calendars[google_id] = _Calendar(
                google_id=google_id,
                name=calendar.name,
                writable=calendar.writable,
                hidden=calendar.hidden,
                primary=calendar.primary,
            )
            self._by_source[calendar.source_id] = google_id
        self._events: dict[str, dict[str, dict[str, object]]] = {
            google_id: {} for google_id in self._calendars
        }
        for event in self.world.events:
            google_id = self._by_source.get(event.source_id)
            state = event.as_of(self.now)
            if google_id is None or state is None:
                continue
            item: dict[str, object] = {
                "id": event.uid,
                "status": "confirmed",
                "summary": state.summary,
                "start": _time_value(state.start),
                "end": _time_value(state.end),
                "creator": {"self": self._calendars[google_id].writable},
            }
            if state.description:
                item["description"] = state.description
            if state.location:
                item["location"] = state.location
            if event.recurring_series_uid:
                item["recurringEventId"] = event.recurring_series_uid
            self._events[google_id][event.uid] = item

    def _resolve(self, calendar_id: str) -> str | None:
        if calendar_id == "primary":
            return next(
                (c.google_id for c in self._calendars.values() if c.primary), None
            )
        return calendar_id if calendar_id in self._calendars else None

    def handle(
        self,
        method: str,
        rest: str,
        query: Mapping[str, str],
        content: bytes | None,
    ) -> ApiResponse | None:
        """Answer one Calendar v3 request; None when the path is not modelled."""
        parts = [unquote(part) for part in rest.strip("/").split("/") if part]
        if method == "GET" and parts == ["users", "me", "calendarList"]:
            return self._calendar_list()
        if len(parts) < 3 or parts[0] != "calendars" or parts[2] != "events":
            return None
        google_id = self._resolve(parts[1])
        if google_id is None:
            return _not_found()
        events = self._events[google_id]
        if len(parts) == 3 and method == "GET":
            return self._list_events(events, query)
        if len(parts) == 3 and method == "POST":
            return self._insert(google_id, events, content)
        if len(parts) != 4:
            return None
        event = events.get(parts[3])
        if event is None:
            return _not_found()
        if method == "GET":
            return _json(200, event)
        if method == "PATCH":
            changes = json.loads(content or b"{}")
            event.update(changes)
            self.writes.append({
                "kind": "calendar_patch",
                "calendar": google_id,
                "id": parts[3],
                "changes": changes,
            })
            return _json(200, event)
        if method == "DELETE":
            del events[parts[3]]
            self.writes.append({
                "kind": "calendar_delete",
                "calendar": google_id,
                "id": parts[3],
            })
            return ApiResponse(status_code=204, content=b"")
        return None

    def _calendar_list(self) -> ApiResponse:
        items = [
            {
                "id": calendar.google_id,
                "summary": calendar.name,
                "accessRole": "owner" if calendar.writable else "reader",
                "primary": calendar.primary,
                "selected": not calendar.hidden,
                "hidden": calendar.hidden,
            }
            for calendar in self._calendars.values()
        ]
        return _json(200, {"items": items})

    @staticmethod
    def _list_events(
        events: Mapping[str, dict[str, object]], query: Mapping[str, str]
    ) -> ApiResponse:
        lower = _instant({"dateTime": query["timeMin"]}) if "timeMin" in query else None
        upper = _instant({"dateTime": query["timeMax"]}) if "timeMax" in query else None
        matching = []
        for item in events.values():
            start = _instant(item.get("start"))
            end = _instant(item.get("end")) or start
            if start is None or end is None:
                continue
            if end == start:
                end = start + timedelta(seconds=1)
            if lower is not None and end <= lower:
                continue
            if upper is not None and start >= upper:
                continue
            matching.append((start, item))
        matching.sort(key=lambda pair: pair[0])
        limit = int(query.get("maxResults", "250"))
        return _json(200, {"items": [item for _, item in matching[:limit]]})

    def _insert(
        self,
        google_id: str,
        events: dict[str, dict[str, object]],
        content: bytes | None,
    ) -> ApiResponse:
        body = json.loads(content or b"{}")
        seed = json.dumps(body, sort_keys=True) + str(len(self.writes))
        event_id = hashlib.sha256(seed.encode()).hexdigest()[:26]
        item = {
            "id": event_id,
            "status": "confirmed",
            "creator": {"self": True},
            **body,
        }
        events[event_id] = item
        self.writes.append({
            "kind": "calendar_insert",
            "calendar": google_id,
            "id": event_id,
            "event": body,
        })
        return _json(200, item)
