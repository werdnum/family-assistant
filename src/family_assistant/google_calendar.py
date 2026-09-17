"""Per-user Google Calendar access for the calendar tools and calendar context.

Google calendars are not configured by the operator: each user's calendars come
from their own connected Google account, so they are resolved per turn from the
acting user. They appear to the calendar tools as :class:`CalendarSource` entries
with ``kind="google"`` and source ids ``google:primary`` / ``google:<calendarId>``,
so a tool can address a calendar directly without listing calendars first.

Reads need the ``calendar.readonly`` scope (the calendar list lives only there);
writes additionally need ``calendar.events``. Writes always pass
``sendUpdates=none`` and never set attendees, so nothing here can email anyone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from dateutil.parser import isoparse

from family_assistant.calendar_integration import CalendarSource
from family_assistant.services.google_api import GoogleUserApi
from family_assistant.services.google_provider import GOOGLE_PROVIDER, GoogleScope

if TYPE_CHECKING:
    from collections.abc import Mapping
    from zoneinfo import ZoneInfo

    from family_assistant.services.api_backend import ApiBackend
    from family_assistant.services.oauth_credentials import OAuthCredentialResolver
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import CalendarEvent, ToolExecutionContext

# Google REST JSON has an open, endpoint-specific shape; callers narrow it with
# ``.get(...)`` and explicit ``isinstance`` checks at each use site.
# ast-grep-ignore: no-dict-any - Free-form Google REST JSON with arbitrary keys.
type GoogleJson = dict[str, Any]

GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_SOURCE_PREFIX = "google:"
GOOGLE_PRIMARY_SOURCE_ID = f"{GOOGLE_SOURCE_PREFIX}primary"

# One page of events.list; the API's own maximum. Search windows are bounded
# (default 90 days), so a single page covers any ordinary calendar.
_EVENTS_PAGE_SIZE = 2500
_WRITABLE_ACCESS_ROLES = frozenset({"owner", "writer"})
# A free/busy-only share exposes no event details to list.
_UNREADABLE_ACCESS_ROLES = frozenset({"freeBusyReader"})
_ACCEPTED_RESPONSES = frozenset({"accepted", "tentative"})


def is_google_source_id(source_id: str | None) -> bool:
    """Whether a calendar source id names a Google calendar."""
    return bool(source_id) and cast("str", source_id).startswith(GOOGLE_SOURCE_PREFIX)


def google_calendar_id_from_source_id(source_id: str) -> str | None:
    """Return the Google calendar id a ``google:`` source id names, else None."""
    if not is_google_source_id(source_id):
        return None
    calendar_id = source_id[len(GOOGLE_SOURCE_PREFIX) :]
    return calendar_id or None


def google_source_id(calendar_id: str, *, primary: bool) -> str:
    """The source id the calendar tools use for a Google calendar."""
    if primary:
        return GOOGLE_PRIMARY_SOURCE_ID
    return f"{GOOGLE_SOURCE_PREFIX}{calendar_id}"


def is_user_vetted_event(item: GoogleJson) -> bool:
    """Whether the user themselves put this event on their calendar.

    Anyone can send an invitation, and Google adds invitations to the calendar
    before they are answered; Gmail-extracted events are built from email
    content. Neither is the user's own word, so ambient surfaces (the per-turn
    calendar context) show only events the user created, organises, or accepted.
    The tools still find everything, and their output is tainted as untrusted.
    """
    if item.get("eventType") == "fromGmail":
        return False
    organizer = item.get("organizer")
    if isinstance(organizer, dict) and organizer.get("self"):
        return True
    attendees = item.get("attendees")
    if isinstance(attendees, list):
        for attendee in attendees:
            if isinstance(attendee, dict) and attendee.get("self"):
                return attendee.get("responseStatus") in _ACCEPTED_RESPONSES
    # No self attendee: created directly on the calendar by the owner or by
    # someone the owner granted edit access, not delivered as an invitation.
    return True


def _parse_event_time(
    value: object, timezone: ZoneInfo
) -> tuple[datetime | date | None, bool]:
    """Parse a Google ``start``/``end`` object into (value, is_all_day)."""
    if not isinstance(value, dict):
        return None, False
    raw_date = value.get("date")
    if isinstance(raw_date, str):
        return date.fromisoformat(raw_date), True
    raw_datetime = value.get("dateTime")
    if isinstance(raw_datetime, str):
        parsed = isoparse(raw_datetime)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.astimezone(timezone), False
    return None, False


def google_event_to_calendar_event(
    item: GoogleJson, source: CalendarSource, timezone: ZoneInfo
) -> CalendarEvent | None:
    """Convert one events.list item into a :class:`CalendarEvent`.

    Returns None for cancelled occurrences and items without usable times.
    """
    if item.get("status") == "cancelled":
        return None
    event_id = item.get("id")
    start, all_day = _parse_event_time(item.get("start"), timezone)
    end, _ = _parse_event_time(item.get("end"), timezone)
    if not isinstance(event_id, str) or start is None:
        return None
    summary = item.get("summary")
    return {
        "uid": event_id,
        "summary": summary if isinstance(summary, str) and summary else "(No title)",
        "start": start,
        "end": end if end is not None else start,
        "all_day": all_day,
        "calendar_url": None,
        "similarity": None,
        "source_id": source.source_id,
        "source_name": source.name,
        "source_kind": "google",
        "writable": source.writable,
    }


def _rfc3339(value: datetime) -> str:
    return value.isoformat()


def build_event_time(value: str, *, all_day: bool, timezone: ZoneInfo) -> GoogleJson:
    """Build a Google ``start``/``end`` object from an ISO 8601 string.

    All-day values become ``{"date": ...}``; timed values without an offset are
    read in the user's timezone, as the CalDAV tools do.
    """
    parsed = isoparse(value)
    if all_day:
        return {"date": parsed.date().isoformat()}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return {"dateTime": parsed.isoformat()}


def iso_value_is_date_only(value: str) -> bool:
    """Whether an ISO 8601 string names a date without a time of day."""
    return "T" not in value and len(value.strip()) <= len("YYYY-MM-DD")


class GoogleCalendarClient:
    """Google Calendar REST calls for one acting user."""

    def __init__(self, api: GoogleUserApi) -> None:
        self._api = api

    @classmethod
    def for_api(cls, api: GoogleUserApi | None) -> GoogleCalendarClient | None:
        """Wrap ``api`` when this deployment requests calendar access, else None."""
        if api is None or not api.scope_configured(GoogleScope.CALENDAR_READONLY):
            return None
        return cls(api)

    @classmethod
    def from_exec_context(
        cls, exec_context: ToolExecutionContext
    ) -> GoogleCalendarClient | None:
        """Client for the tool call's acting user, or None when unavailable.

        Unavailable means the Google integration is off, the deployment does not
        request calendar access, or the turn has no acting user.
        """
        return cls.for_api(GoogleUserApi.from_exec_context(exec_context))

    @property
    def can_write(self) -> bool:
        """Whether this deployment requests the scope that edits events."""
        return self._api.scope_configured(GoogleScope.CALENDAR_EVENTS)

    async def list_calendars(self) -> list[CalendarSource]:
        """The acting user's readable calendars, primary first."""
        response = await self._api.request(
            GoogleScope.CALENDAR_READONLY,
            url=f"{GOOGLE_CALENDAR_API_BASE}/users/me/calendarList",
        )
        items = response.json().get("items")
        sources: list[CalendarSource] = []
        for item in items if isinstance(items, list) else []:
            source = self._calendar_list_entry_to_source(item)
            if source is not None:
                sources.append(source)
        sources.sort(key=lambda source: source.source_id != GOOGLE_PRIMARY_SOURCE_ID)
        return sources

    def primary_source(self) -> CalendarSource:
        """A source for the primary calendar, without listing calendars."""
        return CalendarSource(
            source_id=GOOGLE_PRIMARY_SOURCE_ID,
            name="Google Calendar",
            kind="google",
            url="",
            writable=self.can_write,
            google_calendar_id="primary",
        )

    def _calendar_list_entry_to_source(self, item: object) -> CalendarSource | None:
        if not isinstance(item, dict):
            return None
        entry = cast("GoogleJson", item)
        calendar_id = entry.get("id")
        access_role = entry.get("accessRole")
        if not isinstance(calendar_id, str) or access_role in _UNREADABLE_ACCESS_ROLES:
            return None
        primary = entry.get("primary") is True
        name = entry.get("summaryOverride") or entry.get("summary") or calendar_id
        return CalendarSource(
            source_id=google_source_id(calendar_id, primary=primary),
            name=str(name),
            kind="google",
            url="",
            writable=self.can_write and access_role in _WRITABLE_ACCESS_ROLES,
            google_calendar_id="primary" if primary else calendar_id,
            searched_by_default=primary
            or (entry.get("selected") is True and entry.get("hidden") is not True),
            owned=access_role == "owner",
        )

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[GoogleJson]:
        """Expanded event occurrences overlapping [time_min, time_max)."""
        response = await self._api.request(
            GoogleScope.CALENDAR_READONLY,
            url=f"{self._events_url(calendar_id)}",
            params={
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(_EVENTS_PAGE_SIZE),
            },
        )
        items = response.json().get("items")
        return [
            cast("GoogleJson", item)
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        ]

    async def get_event(self, calendar_id: str, event_id: str) -> GoogleJson:
        response = await self._api.request(
            GoogleScope.CALENDAR_READONLY,
            url=self._event_url(calendar_id, event_id),
        )
        return cast("GoogleJson", response.json())

    async def insert_event(self, calendar_id: str, body: GoogleJson) -> GoogleJson:
        response = await self._api.request(
            GoogleScope.CALENDAR_EVENTS,
            method="POST",
            url=self._events_url(calendar_id),
            params={"sendUpdates": "none"},
            content=json.dumps(body).encode("utf-8"),
            content_type="application/json",
        )
        return cast("GoogleJson", response.json())

    async def patch_event(
        self, calendar_id: str, event_id: str, body: GoogleJson
    ) -> GoogleJson:
        response = await self._api.request(
            GoogleScope.CALENDAR_EVENTS,
            method="PATCH",
            url=self._event_url(calendar_id, event_id),
            params={"sendUpdates": "none"},
            content=json.dumps(body).encode("utf-8"),
            content_type="application/json",
        )
        return cast("GoogleJson", response.json())

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        await self._api.request(
            GoogleScope.CALENDAR_EVENTS,
            method="DELETE",
            url=self._event_url(calendar_id, event_id),
            params={"sendUpdates": "none"},
        )

    @staticmethod
    def _events_url(calendar_id: str) -> str:
        return (
            f"{GOOGLE_CALENDAR_API_BASE}/calendars/{quote(calendar_id, safe='')}/events"
        )

    @classmethod
    def _event_url(cls, calendar_id: str, event_id: str) -> str:
        return f"{cls._events_url(calendar_id)}/{quote(event_id, safe='')}"


type GoogleCalendarFactory = Callable[[str], GoogleCalendarClient | None]
"""Builds a Google Calendar client for a turn's acting user, or None."""


def google_calendar_factory(
    credential_resolvers: Mapping[str, OAuthCredentialResolver],
    api_backend: ApiBackend | None,
    database: Callable[[], Database],
) -> GoogleCalendarFactory | None:
    """A per-user client factory, or None when Google Calendar is not offered."""
    resolver = credential_resolvers.get(GOOGLE_PROVIDER.name)
    if resolver is None or api_backend is None:
        return None
    if GoogleScope.CALENDAR_READONLY.value not in resolver.configured_scopes:
        return None

    def for_user(user_id: str) -> GoogleCalendarClient | None:
        return GoogleCalendarClient.for_api(
            GoogleUserApi(
                resolver=resolver, backend=api_backend, db=database(), user_id=user_id
            )
        )

    return for_user
