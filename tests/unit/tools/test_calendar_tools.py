"""Unit tests for calendar tools."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from family_assistant.storage.database import Database
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_METADATA_BY_NAME,
    LocalToolsProvider,
    ToolTag,
)
from family_assistant.tools.calendar import (
    CALENDAR_TOOLS_DEFINITION,
    check_for_duplicate_events,
    list_calendars_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.types import CalendarConfig, ToolExecutionContext


def _create_mock_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="test_conv",
        user_name="TestUser",
        turn_id="test_turn",
        db_context=MagicMock(spec=Database),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        chat_interface=None,
        timezone=ZoneInfo("UTC"),
        camera_backend=None,
        credential_resolvers=None,
        api_backend=None,
    )


@pytest.mark.asyncio
async def test_list_calendars_empty_config() -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {}
    result = await list_calendars_tool(ctx, config)
    assert result == "No calendars configured."


@pytest.mark.asyncio
async def test_list_calendars_mixed_sources() -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pass",
            "calendar_urls": [
                {
                    "id": "family",
                    "name": "Family Calendar",
                    "url": "https://caldav.example.com/family",
                },
                "https://caldav.example.com/work",
            ],
        },
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics?token=secret123",
                },
                "https://school.edu/calendar.ics",
            ]
        },
    }

    result = await list_calendars_tool(ctx, config)

    assert "Available calendars:" in result
    assert (
        "- family: Family Calendar (CalDAV, writable) [default for new events]"
        in result
    )
    assert "- work: Work (CalDAV, writable)" in result
    assert "- tripit: TripIt Trips (iCal feed, read-only)" in result
    assert "- calendar: Calendar (iCal feed, read-only)" in result

    # Security check: Secret token / URLs must NEVER leak
    assert "secret123" not in result
    assert "https://" not in result


def test_list_calendars_registration() -> None:
    assert "list_calendars" in AVAILABLE_FUNCTIONS
    assert AVAILABLE_FUNCTIONS["list_calendars"] is list_calendars_tool

    metadata = LOCAL_TOOL_METADATA_BY_NAME["list_calendars"]
    assert ToolTag.READ_ONLY in metadata.tags
    assert ToolTag.SENSITIVE_DATA in metadata.tags
    assert ToolTag.CALENDAR in metadata.tags
    assert ToolTag.OUTPUT_TRUSTED in metadata.tags
    assert ToolTag.STATE_CHANGING not in metadata.tags
    assert ToolTag.DESTRUCTIVE not in metadata.tags

    def_names = [d["function"]["name"] for d in CALENDAR_TOOLS_DEFINITION]
    assert "list_calendars" in def_names


@pytest.mark.asyncio
async def test_list_calendars_via_local_tools_provider() -> None:
    calendar_config: CalendarConfig = {
        "caldav": {
            "username": "u",
            "password": "p",
            "calendar_urls": ["https://caldav.example.com/cal"],
        }
    }
    provider = LocalToolsProvider(
        definitions=CALENDAR_TOOLS_DEFINITION,
        implementations={"list_calendars": list_calendars_tool},
        calendar_config=calendar_config,
    )
    ctx = _create_mock_context()

    result = await provider.execute_tool("list_calendars", {}, context=ctx)
    assert isinstance(result, str)
    assert "Available calendars:" in result
    assert "- cal: Cal (CalDAV, writable) [default for new events]" in result


@pytest.mark.asyncio
async def test_search_calendar_events_empty_config() -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {}
    result = await search_calendar_events_tool(ctx, config, search_text="meeting")
    assert result == "Error: No calendars configured. Cannot search calendar events."


@pytest.mark.asyncio
async def test_search_calendar_events_with_ical_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    mock_clock = MagicMock()
    mock_clock.now.return_value = now
    ctx = ToolExecutionContext(
        interface_type=ctx.interface_type,
        conversation_id=ctx.conversation_id,
        user_name=ctx.user_name,
        turn_id=ctx.turn_id,
        db_context=ctx.db_context,
        processing_service=ctx.processing_service,
        clock=mock_clock,
        home_assistant_client=ctx.home_assistant_client,
        event_sources=ctx.event_sources,
        attachment_registry=ctx.attachment_registry,
        chat_interface=ctx.chat_interface,
        timezone=ZoneInfo("UTC"),
        camera_backend=ctx.camera_backend,
        credential_resolvers=ctx.credential_resolvers,
        api_backend=ctx.api_backend,
    )

    ics_content = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "X-WR-CALNAME:TripIt Itineraries",
        "PRODID:-//FamilyAssistant Test//EN",
        "BEGIN:VEVENT",
        "UID:flight-syd-mel-123",
        "DTSTART:20260405T090000Z",
        "DTEND:20260405T103000Z",
        "SUMMARY:Flight to Melbourne",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=ics_content)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    config: CalendarConfig = {
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics?token=private_bearer_token",
                }
            ]
        }
    }

    result = await search_calendar_events_tool(ctx, config, search_text="flight")

    assert "Found 1 event(s):" in result
    assert "Flight to Melbourne" in result
    assert "UID: flight-syd-mel-123" in result
    assert "Calendar: TripIt Trips (read-only)" in result
    assert "Source ID: tripit" in result
    # Bearer token must NEVER appear in output
    assert "private_bearer_token" not in result
    assert "https://" not in result


@pytest.mark.asyncio
async def test_search_calendar_events_source_ids_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    mock_clock = MagicMock()
    mock_clock.now.return_value = now
    ctx = ToolExecutionContext(
        interface_type=ctx.interface_type,
        conversation_id=ctx.conversation_id,
        user_name=ctx.user_name,
        turn_id=ctx.turn_id,
        db_context=ctx.db_context,
        processing_service=ctx.processing_service,
        clock=mock_clock,
        home_assistant_client=ctx.home_assistant_client,
        event_sources=ctx.event_sources,
        attachment_registry=ctx.attachment_registry,
        chat_interface=ctx.chat_interface,
        timezone=ZoneInfo("UTC"),
        camera_backend=ctx.camera_backend,
        credential_resolvers=ctx.credential_resolvers,
        api_backend=ctx.api_backend,
    )

    feed_responses = {
        "https://tripit.example.com/feed.ics": "\r\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Test//EN",
            "BEGIN:VEVENT",
            "UID:trip-1",
            "DTSTART:20260405T090000Z",
            "DTEND:20260405T103000Z",
            "SUMMARY:Flight to Melbourne",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]),
        "https://school.example.com/feed.ics": "\r\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Test//EN",
            "BEGIN:VEVENT",
            "UID:school-1",
            "DTSTART:20260406T090000Z",
            "DTEND:20260406T150000Z",
            "SUMMARY:School Sports Carnival",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]),
    }

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        content = feed_responses.get(url, "")
        return httpx.Response(200, text=content)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    config: CalendarConfig = {
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics",
                },
                {
                    "id": "school",
                    "name": "School Calendar",
                    "url": "https://school.example.com/feed.ics",
                },
            ]
        }
    }

    # Filter by source_ids: school only
    result_school = await search_calendar_events_tool(
        ctx, config, source_ids=["school"]
    )
    assert "School Sports Carnival" in result_school
    assert "Flight to Melbourne" not in result_school

    # Filter by source_ids: tripit only
    result_tripit = await search_calendar_events_tool(
        ctx, config, source_ids=["tripit"]
    )
    assert "Flight to Melbourne" in result_tripit
    assert "School Sports Carnival" not in result_tripit

    # Unknown source_id returns helpful error listing available sources
    result_unknown = await search_calendar_events_tool(
        ctx, config, source_ids=["nonexistent"]
    )
    assert (
        "Error: None of the requested calendar source IDs (nonexistent) were found."
        in result_unknown
    )
    assert "tripit" in result_unknown
    assert "school" in result_unknown


@pytest.mark.asyncio
async def test_duplicate_detection_checks_ical_feeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    event_start = "2026-04-10T14:00:00Z"
    event_end = "2026-04-10T15:00:00Z"

    ics_content = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Test//EN",
        "BEGIN:VEVENT",
        "UID:flight-conflicting-1",
        "DTSTART:20260410T143000Z",
        "DTEND:20260410T160000Z",
        "SUMMARY:Flight to Melbourne QF445",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])

    async def fake_get(
        self: httpx.AsyncClient, url: str, **kwargs: object
    ) -> httpx.Response:
        return httpx.Response(200, text=ics_content)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    config: CalendarConfig = {
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics",
                }
            ]
        },
        "duplicate_detection": {
            "similarity_strategy": "fuzzy",
            "similarity_threshold": 0.3,
            "time_window_hours": 2,
        },
    }

    warning = await check_for_duplicate_events(
        exec_context=ctx,
        calendar_config=config,
        summary="Flight to Melbourne",
        start_time=event_start,
        end_time=event_end,
        all_day=False,
    )

    assert warning is not None
    assert "Cannot create event 'Flight to Melbourne'" in warning
    assert "Flight to Melbourne QF445" in warning
    assert "UID: flight-conflicting-1" in warning
    assert "bypass_duplicate_check=true" in warning
