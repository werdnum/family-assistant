"""Home Assistant calendars: the event current at the moment, where one was recorded.

Only the event a calendar's state showed is known, so that is the one event
each calendar holds; the household's real calendars are served elsewhere in
the sandbox.
"""

from __future__ import annotations

# Home Assistant's entity bases declare a sync and an async form of each action and
# an integration implements one; the sync forms stay abstract here by design.
# pylint: disable=abstract-method
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .entity import Attributes, WorldEntity, build_entities

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


def _when(value: str | None, *, all_day: bool) -> date | datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if all_day:
        return parsed.date()
    return (
        parsed
        if parsed.tzinfo
        else parsed.replace(tzinfo=dt_util.get_default_time_zone())
    )


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(
        value.year, value.month, value.day, tzinfo=dt_util.get_default_time_zone()
    )


class WorldCalendar(WorldEntity, CalendarEntity):
    def setup_world(self, attributes: Attributes) -> None:
        all_day = bool(attributes.flag("all_day"))
        summary = attributes.text("message")
        start = _when(attributes.text("start_time"), all_day=all_day)
        end = _when(attributes.text("end_time"), all_day=all_day)
        description = attributes.text("description")
        location = attributes.text("location")
        attributes.take("offset_reached")
        self._event: CalendarEvent | None = None
        if summary and start is not None:
            if end is None or _as_datetime(end) <= _as_datetime(start):
                end = (
                    start + timedelta(days=1) if all_day else start + timedelta(hours=1)
                )
            self._event = CalendarEvent(
                start=start,
                end=end,
                summary=summary,
                description=description,
                location=location,
            )

    @property
    def event(self) -> CalendarEvent | None:
        return self._event

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        event = self._event
        if event is None:
            return []
        if (
            _as_datetime(event.end) <= start_date
            or _as_datetime(event.start) >= end_date
        ):
            return []
        return [event]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(build_entities(hass, entry, "calendar", WorldCalendar))
