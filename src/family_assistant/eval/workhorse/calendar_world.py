"""The household's calendars, rebuilt from recorded calendar tool results.

The snapshot has no calendar data, but every ``list_calendars`` and
``search_calendar_events`` result a turn received is recorded, along with the
arguments of every add, modify and delete. Pooled, they give the calendars
the deployment had (one writable CalDAV calendar, iCal feeds, Google
calendars) and the events a turn ever saw on them.

Each event keeps its observations over time, so a case sees the event as it
looked when the case was asked: an event a later turn renamed shows its old
name, one a later turn created does not exist yet, and one a later turn
deleted still does. What was never observed -- descriptions of events nobody
created through the assistant, recurrence rules -- is absent rather than
invented.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from zoneinfo import ZoneInfo

    from family_assistant.storage.types import MessageHistoryRow

__all__ = [
    "CalendarWorld",
    "EventObservation",
    "WorldCalendar",
    "WorldEvent",
    "build_calendar_world",
]

CalendarKind = Literal["caldav", "ical", "google"]

_LIST_ENTRY = re.compile(
    r"^- (?P<source>[^:]+(?::[^:\s]+)?): (?P<name>.*?) \((?P<kind>CalDAV|iCal feed|"
    r"Google Calendar)(?P<flags>[^)]*)\)"
)
_EVENT_BLOCK = re.compile(
    r"^\d+\. (?P<summary>.*?)(?: \((?:match|similarity): [\d.]+\))?\n"
    r"\s+Start: (?P<start>[^\n]*)\n"
    r"\s+End: (?P<end>[^\n]*)\n"
    r"\s+UID: (?P<uid>[^\n]*)\n"
    r"\s+Calendar: (?P<calendar>[^\n]*)"
    r"(?:\n\s+Source ID: (?P<source>[^\n]*))?"
    r"(?:\n\s+Recurring series UID: (?P<series>\S+)[^\n]*)?",
    re.MULTILINE,
)
_DISPLAY_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})(?: \S+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYNTHETIC_PREFIX = "eval-"
_KINDS: dict[str, CalendarKind] = {
    "CalDAV": "caldav",
    "iCal feed": "ical",
    "Google Calendar": "google",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldCalendar(_Model):
    source_id: str
    name: str
    kind: CalendarKind
    writable: bool
    # Hidden calendars are searched only when named.
    hidden: bool = False
    primary: bool = False
    is_default: bool = False


class EventObservation(_Model):
    """How an event looked at one moment a turn saw or wrote it."""

    seen_at: datetime
    summary: str
    # ISO date for an all-day event, ISO datetime with offset otherwise.
    start: str
    end: str
    all_day: bool
    description: str | None = None
    location: str | None = None


class WorldEvent(_Model):
    uid: str
    source_id: str
    recurring_series_uid: str | None = None
    observations: list[EventObservation]
    # Further starts of a recurring series that reuses one UID for every
    # occurrence, as CalDAV does; each lasts as long as the observed one.
    occurrence_starts: list[str] = []
    # Set when a recorded turn created or deleted it.
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    matched_queries: list[str] = []

    def as_of(self, moment: datetime) -> EventObservation | None:
        """The event as it stood at ``moment``, or None if it did not exist."""
        if self.created_at is not None and self.created_at > moment:
            return None
        if self.deleted_at is not None and self.deleted_at <= moment:
            return None
        ordered = sorted(self.observations, key=lambda o: o.seen_at)
        before = [o for o in ordered if o.seen_at <= moment]
        return before[-1] if before else ordered[0]


class CalendarWorld(_Model):
    calendars: list[WorldCalendar]
    events: list[WorldEvent]

    def calendar(self, source_id: str) -> WorldCalendar | None:
        return next((c for c in self.calendars if c.source_id == source_id), None)


def _to_iso(display: str, timezone: ZoneInfo) -> tuple[str, bool] | None:
    """A rendered start/end back to (ISO value, is_all_day)."""
    value = display.strip()
    if _DATE.match(value):
        return value, True
    if match := _DISPLAY_TIME.match(value):
        day = date.fromisoformat(match[1])
        moment = datetime(
            day.year, day.month, day.day, int(match[2]), int(match[3]), tzinfo=timezone
        )
        return moment.isoformat(), False
    return None


def _source_from_calendar_line(calendar: str) -> str:
    """CalDAV results name the calendar by URL; its id is the URL's last segment."""
    return calendar.rstrip("/").rsplit("/", 1)[-1]


def _arguments(raw: object) -> dict[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _synthetic_uid(arguments: Mapping[str, object]) -> str:
    digest = hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=str).encode()
    ).hexdigest()
    return f"{_SYNTHETIC_PREFIX}{digest[:24]}"


def _span(observation: EventObservation) -> tuple[str, str, timedelta | None]:
    """Title, time of day and duration: what a series keeps between occurrences."""
    if observation.all_day:
        start, end = (
            date.fromisoformat(observation.start),
            date.fromisoformat(observation.end[:10]),
        )
        return observation.summary, "", end - start
    start_at = datetime.fromisoformat(observation.start)
    end_at = (
        datetime.fromisoformat(observation.end) if len(observation.end) > 10 else None
    )
    return (
        observation.summary,
        start_at.strftime("%H:%M"),
        end_at - start_at if end_at is not None else None,
    )


def _another_occurrence(known: list[EventObservation], seen: EventObservation) -> bool:
    """Whether ``seen`` is a different occurrence of a series, not an edit.

    CalDAV gives every occurrence of a recurring series the same UID, which
    looks like one event moving. An occurrence keeps the title, time of day
    and length and lands on another date; an edit changes one of those.
    """
    latest = known[-1]
    return latest.start[:10] != seen.start[:10] and _span(latest) == _span(seen)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _iso_argument(value: object, timezone: ZoneInfo) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _DATE.match(value):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.isoformat()


class _Builder:
    def __init__(self, timezone: ZoneInfo) -> None:
        self.timezone = timezone
        self.calendars: dict[str, WorldCalendar] = {}
        self.calendar_seen: dict[str, datetime] = {}
        self.events: dict[str, WorldEvent] = {}

    def add_calendar_listing(self, content: str, seen: datetime) -> None:
        for line in content.splitlines():
            match = _LIST_ENTRY.match(line.strip())
            if match is None:
                continue
            source = match["source"]
            if self.calendar_seen.get(source, seen) > seen:
                continue
            flags = match["flags"]
            kind = _KINDS[match["kind"]]
            self.calendars[source] = WorldCalendar(
                source_id=source,
                name=match["name"],
                kind=kind,
                writable="writable" in flags,
                hidden="searched only when named" in flags,
                primary=source == "google:primary",
                is_default="[default for new events]" in line,
            )
            self.calendar_seen[source] = seen

    def add_search_results(self, content: str, query: str, seen: datetime) -> None:
        for match in _EVENT_BLOCK.finditer(content):
            start = _to_iso(match["start"], self.timezone)
            end = _to_iso(match["end"], self.timezone)
            if start is None:
                continue
            source = match["source"] or _source_from_calendar_line(match["calendar"])
            observation = EventObservation(
                seen_at=seen,
                summary=match["summary"],
                start=start[0],
                end=(end or start)[0],
                all_day=start[1],
            )
            uid = match["uid"].strip()
            existing = self.events.get(uid)
            if existing is None:
                self.events[uid] = WorldEvent(
                    uid=uid,
                    source_id=source,
                    recurring_series_uid=match["series"],
                    observations=[observation],
                    matched_queries=[query] if query else [],
                )
                continue
            if _another_occurrence(existing.observations, observation):
                starts = {o.start for o in existing.observations}
                if (
                    observation.start not in starts
                    and observation.start not in existing.occurrence_starts
                ):
                    existing.occurrence_starts.append(observation.start)
            else:
                existing.observations.append(observation)
            if query and query not in existing.matched_queries:
                existing.matched_queries.append(query)

    def add_created(
        self, arguments: Mapping[str, object], result: str, seen: datetime
    ) -> None:
        start = _iso_argument(arguments.get("start_time"), self.timezone)
        end = _iso_argument(arguments.get("end_time"), self.timezone) or start
        summary = arguments.get("summary")
        if start is None or end is None or not isinstance(summary, str):
            return
        uid_match = re.search(r"UID: ([^)\s]+)", result)
        uid = uid_match[1] if uid_match else _synthetic_uid(arguments)
        source = arguments.get("calendar_id")
        observation = EventObservation(
            seen_at=seen,
            summary=summary,
            start=start,
            end=end,
            all_day=bool(arguments.get("all_day")) or _DATE.match(start) is not None,
            description=_optional_str(arguments.get("description")),
            location=_optional_str(arguments.get("location")),
        )
        existing = self.events.get(uid)
        if existing is None:
            self.events[uid] = WorldEvent(
                uid=uid,
                source_id=source if isinstance(source, str) else "",
                observations=[observation],
                created_at=seen,
            )
        else:
            existing.observations.append(observation)
            existing.created_at = min(existing.created_at or seen, seen)

    def add_deleted(self, arguments: Mapping[str, object], seen: datetime) -> None:
        uid = arguments.get("uid")
        if isinstance(uid, str) and uid in self.events:
            self.events[uid].deleted_at = seen

    def _reconcile_creations(self) -> None:
        """Fold a UID-less creation into the event later seen under its real UID.

        CalDAV creations report no UID, so a creation is first given a
        synthetic one; when a search later shows an event with the same title
        and start, that is the same event, and keeping both would serve it
        twice -- once before it was created.
        """
        observed = {
            (o.summary, o.start): event
            for event in self.events.values()
            if not event.uid.startswith(_SYNTHETIC_PREFIX)
            for o in event.observations
        }
        for uid in [u for u in self.events if u.startswith(_SYNTHETIC_PREFIX)]:
            created = self.events[uid]
            first = created.observations[0]
            match = observed.get((first.summary, first.start))
            if match is None:
                continue
            match.created_at = min(
                t for t in (match.created_at, created.created_at) if t is not None
            )
            match.observations.extend(created.observations)
            match.observations.sort(key=lambda o: o.seen_at)
            del self.events[uid]

    def finish(self) -> CalendarWorld:
        self._reconcile_creations()
        default = next(
            (c.source_id for c in self.calendars.values() if c.is_default), None
        )
        # An event created without naming a calendar went to the default.
        events = [
            event.model_copy(update={"source_id": default})
            if not event.source_id and default is not None
            else event
            for event in self.events.values()
        ]
        return CalendarWorld(
            calendars=sorted(self.calendars.values(), key=lambda c: c.source_id),
            events=sorted(events, key=lambda e: e.uid),
        )


def _is_failure(content: str) -> bool:
    return content.startswith("Error")


def build_calendar_world(
    grouped: Mapping[tuple[str, str], Iterable[MessageHistoryRow]],
    *,
    timezone: ZoneInfo,
) -> CalendarWorld:
    """Pool every recorded calendar listing, search and write into one world."""
    builder = _Builder(timezone)
    writes: list[tuple[datetime, str, dict[str, object], str]] = []
    for rows in grouped.values():
        calls: dict[str, tuple[str, dict[str, object]]] = {}
        results: list[MessageHistoryRow] = []
        for row in rows:
            if row["role"] == "assistant":
                for item in row["tool_calls"] or []:
                    calls[item.id] = (
                        item.function.name,
                        _arguments(item.function.arguments),
                    )
            elif row["role"] == "tool" and row["tool_call_id"] and row["content"]:
                results.append(row)
        for row in results:
            name, arguments = calls.get(
                row["tool_call_id"] or "", (row["tool_name"], {})
            )
            content = row["content"] or ""
            if _is_failure(content):
                continue
            if name == "list_calendars":
                builder.add_calendar_listing(content, row["timestamp"])
            elif name == "search_calendar_events":
                query = str(arguments.get("search_text") or "")
                builder.add_search_results(content, query, row["timestamp"])
            elif name in {"add_calendar_event", "delete_calendar_event"}:
                writes.append((row["timestamp"], name, arguments, content))
    # Writes apply after every observation is known, in time order, so a
    # create can match the UID a later search revealed.
    for seen, name, arguments, content in sorted(writes, key=lambda w: w[0]):
        if name == "add_calendar_event":
            builder.add_created(arguments, content, seen)
        else:
            builder.add_deleted(arguments, seen)
    return builder.finish()
