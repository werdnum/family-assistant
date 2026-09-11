from typing import TYPE_CHECKING

from pydantic import SecretStr

from family_assistant.calendar_integration import (
    resolve_calendar_sources,
)
from family_assistant.config_models import (
    CalDAVCalendarConfig,
    CalDAVConfig,
    CalendarConfig,
    ICalConfig,
    ICalFeedConfig,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import (
        CalendarConfig as CalendarConfigDict,
    )


def test_resolve_calendar_sources_empty() -> None:
    assert resolve_calendar_sources(None) == []
    assert resolve_calendar_sources({}) == []


def test_resolve_calendar_sources_bare_urls() -> None:
    config: CalendarConfigDict = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "calendar_urls": [
                "https://caldav.example.com/calendars/user/family/",
                "https://caldav.example.com/calendars/user/work",
            ],
        },
        "ical": {
            "urls": [
                "https://example.com/feeds/tripit.ics?token=secret123",
                "https://example.com/holidays/nsw_schools.ics",
            ]
        },
    }

    sources = resolve_calendar_sources(config)
    assert len(sources) == 4

    # CalDAV 1
    assert sources[0].source_id == "family"
    assert sources[0].name == "Family"
    assert sources[0].kind == "caldav"
    assert sources[0].writable is True
    assert sources[0].is_default is True
    assert sources[0].url == "https://caldav.example.com/calendars/user/family/"

    # CalDAV 2
    assert sources[1].source_id == "work"
    assert sources[1].name == "Work"
    assert sources[1].kind == "caldav"
    assert sources[1].writable is True
    assert sources[1].is_default is False

    # iCal 1
    assert sources[2].source_id == "tripit"
    assert sources[2].name == "Tripit"
    assert sources[2].kind == "ical"
    assert sources[2].writable is False
    assert sources[2].is_default is False
    assert sources[2].url == "https://example.com/feeds/tripit.ics?token=secret123"

    # iCal 2
    assert sources[3].source_id == "nsw_schools"
    assert sources[3].name == "Nsw Schools"
    assert sources[3].kind == "ical"
    assert sources[3].writable is False
    assert sources[3].is_default is False


def test_resolve_calendar_sources_rich_entries() -> None:
    config: CalendarConfigDict = {
        "caldav": {
            "calendar_urls": [
                {
                    "url": "https://caldav.example.com/dav/primary/",
                    "id": "personal",
                    "name": "Personal Calendar",
                }
            ]
        },
        "ical": {
            "urls": [
                {
                    "url": "https://tripit.example.com/secret.ics",
                    "id": "travel",
                    "name": "TripIt Itineraries",
                }
            ]
        },
    }

    sources = resolve_calendar_sources(config)
    assert len(sources) == 2

    assert sources[0].source_id == "personal"
    assert sources[0].name == "Personal Calendar"
    assert sources[0].kind == "caldav"
    assert sources[0].writable is True
    assert sources[0].is_default is True

    assert sources[1].source_id == "travel"
    assert sources[1].name == "TripIt Itineraries"
    assert sources[1].kind == "ical"
    assert sources[1].writable is False
    assert sources[1].is_default is False


def test_resolve_calendar_sources_collision_handling() -> None:
    config: CalendarConfigDict = {
        "caldav": {
            "calendar_urls": [
                "https://caldav.example.com/dav/family/",
                "https://other.example.com/dav/family/",
            ]
        },
        "ical": {
            "urls": [
                "https://feed.example.com/family.ics",
            ]
        },
    }

    sources = resolve_calendar_sources(config)
    assert len(sources) == 3
    assert sources[0].source_id == "family"
    assert sources[1].source_id == "family_2"
    assert sources[2].source_id == "family_3"


def test_pydantic_calendar_config_model_dump() -> None:
    pydantic_conf = CalendarConfig(
        caldav=CalDAVConfig(
            username="test_user",
            password=SecretStr("super_secret"),
            calendar_urls=[
                "https://caldav.example.com/bare",
                CalDAVCalendarConfig(
                    url="https://caldav.example.com/rich",
                    id="rich_cal",
                    name="Rich Calendar",
                ),
            ],
        ),
        ical=ICalConfig(
            urls=[
                "https://ical.example.com/bare.ics",
                ICalFeedConfig(
                    url="https://ical.example.com/rich.ics",
                    id="rich_feed",
                    name="Rich Feed",
                ),
            ]
        ),
    )

    dumped = pydantic_conf.model_dump(exclude_none=True)
    sources = resolve_calendar_sources(dumped)  # type: ignore[arg-type]
    assert len(sources) == 4
    assert [s.source_id for s in sources] == [
        "bare",
        "rich_cal",
        "bare_2",
        "rich_feed",
    ]
