"""Unit tests for calendar tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from family_assistant.security.taint import (
    InMemoryTurnTaintTracker,
    SourceTrustTier,
    derive_tool_result_taint_source,
)
from family_assistant.storage.database import Database
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS,
    LOCAL_TOOL_DESCRIPTORS,
    LOCAL_TOOL_METADATA_BY_NAME,
    LocalToolsProvider,
    ToolTag,
)
from family_assistant.tools.calendar import (
    CALENDAR_TOOLS_DEFINITION,
    CalendarSearchResult,
    add_calendar_event_tool,
    check_for_duplicate_events,
    delete_calendar_event_tool,
    list_calendars_tool,
    modify_calendar_event_tool,
    resolve_target_caldav_url,
    search_calendar_events_tool,
)
from family_assistant.tools.confirmation import (
    render_add_calendar_event_confirmation,
    render_delete_calendar_event_confirmation,
    render_modify_calendar_event_confirmation,
)
from family_assistant.tools.types import (
    CalendarConfig,
    CalendarEvent,
    ToolExecutionContext,
)


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
    ctx.taint_tracker = InMemoryTurnTaintTracker()
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
    assert ctx.taint_tracker.snapshot().max_tier == SourceTrustTier.UNKNOWN_EXTERNAL


@pytest.mark.asyncio
async def test_add_calendar_event_targeting_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "base_url": "https://caldav.example.com",
            "calendar_urls": [
                {
                    "id": "personal",
                    "name": "Personal",
                    "url": "https://caldav.example.com/personal",
                },
                {
                    "id": "work",
                    "name": "Work",
                    "url": "https://caldav.example.com/work",
                },
            ],
        },
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics",
                }
            ]
        },
    }

    # 1. Reject read-only iCal source
    ro_result = await add_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        summary="Test Event",
        start_time="2026-05-01T10:00:00Z",
        end_time="2026-05-01T11:00:00Z",
        calendar_id="tripit",
    )
    assert (
        "Error: Calendar 'TripIt Trips' (tripit) is read-only (iCal subscription). Events cannot be added to it."
        in ro_result
    )

    # 2. Unknown calendar_id
    unknown_result = await add_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        summary="Test Event",
        start_time="2026-05-01T10:00:00Z",
        end_time="2026-05-01T11:00:00Z",
        calendar_id="school",
    )
    assert "Error: Calendar 'school' not found." in unknown_result
    assert "personal" in unknown_result
    assert "work" in unknown_result

    # 3. Successful targeting of specific CalDAV calendar
    mock_client_inst = MagicMock()
    mock_cal = MagicMock()
    mock_client_inst.calendar.return_value = mock_cal
    mock_client_inst.__enter__.return_value = mock_client_inst

    mock_dav_client = MagicMock(return_value=mock_client_inst)
    monkeypatch.setattr("caldav.DAVClient", mock_dav_client)

    success_result = await add_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        summary="Work Meeting",
        start_time="2026-05-01T10:00:00Z",
        end_time="2026-05-01T11:00:00Z",
        calendar_id="work",
        bypass_duplicate_check=True,
    )
    assert "Work Meeting" in success_result
    assert "added to the calendar" in success_result
    mock_client_inst.calendar.assert_called_with(url="https://caldav.example.com/work")
    mock_cal.save_event.assert_called_once()


@pytest.mark.asyncio
async def test_modify_calendar_event_targeting_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "base_url": "https://caldav.example.com",
            "calendar_urls": [
                {
                    "id": "work",
                    "name": "Work",
                    "url": "https://caldav.example.com/work",
                }
            ],
        },
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics",
                }
            ]
        },
    }

    # 1. Neither calendar_id nor calendar_url provided
    err_neither = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        new_summary="Updated",
    )
    assert (
        "Error: Either calendar_id or calendar_url must be provided to modify an event."
        in err_neither
    )

    # 2. Reject read-only iCal by calendar_id
    err_ical_id = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="tripit",
        new_summary="Updated",
    )
    assert (
        "Error: Calendar 'TripIt Trips' (tripit) is a read-only subscription. Events cannot be modified."
        in err_ical_id
    )

    # 3. Reject read-only iCal by calendar_url
    err_ical_url = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_url="https://tripit.example.com/feed.ics",
        new_summary="Updated",
    )
    assert (
        "Error: Calendar 'TripIt Trips' is a read-only subscription. Events cannot be modified."
        in err_ical_url
    )

    # 4. Unknown calendar_id
    err_unknown = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="missing",
        new_summary="Updated",
    )
    assert "Error: Calendar 'missing' not found." in err_unknown

    # 5. Successful targeting by calendar_id
    import vobject  # noqa: PLC0415

    mock_client_inst = MagicMock()
    mock_cal = MagicMock()
    mock_event = MagicMock()
    ics_text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-1\r\n"
        "SUMMARY:Old\r\n"
        "DTSTART:20260501T100000Z\r\n"
        "DTEND:20260501T110000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    mock_event.data = ics_text
    mock_event.vobject_instance = vobject.readOne(ics_text)
    mock_cal.events.return_value = [mock_event]
    mock_client_inst.calendar.return_value = mock_cal
    mock_client_inst.__enter__.return_value = mock_client_inst

    mock_dav_client = MagicMock(return_value=mock_client_inst)
    monkeypatch.setattr("caldav.DAVClient", mock_dav_client)

    ok_res = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="work",
        new_summary="New Work Title",
    )
    assert "OK. Event 'Old' updated: title to 'New Work Title'." in ok_res
    mock_client_inst.calendar.assert_called_with(url="https://caldav.example.com/work")
    mock_event.save.assert_called_once()


@pytest.mark.asyncio
async def test_delete_calendar_event_targeting_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "base_url": "https://caldav.example.com",
            "calendar_urls": [
                {
                    "id": "work",
                    "name": "Work",
                    "url": "https://caldav.example.com/work",
                }
            ],
        },
        "ical": {
            "urls": [
                {
                    "id": "tripit",
                    "name": "TripIt Trips",
                    "url": "https://tripit.example.com/feed.ics",
                }
            ]
        },
    }

    # 1. Neither provided
    err_neither = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
    )
    assert (
        "Error: Either calendar_id or calendar_url must be provided to delete an event."
        in err_neither
    )

    # 2. Reject read-only iCal by calendar_id
    err_ro_id = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="tripit",
    )
    assert (
        "Error: Calendar 'TripIt Trips' (tripit) is a read-only subscription. Events cannot be deleted."
        in err_ro_id
    )

    # 3. Reject read-only iCal by calendar_url
    err_ro_url = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_url="https://tripit.example.com/feed.ics",
    )
    assert (
        "Error: Calendar 'TripIt Trips' is a read-only subscription. Events cannot be deleted."
        in err_ro_url
    )

    # 4. Unknown calendar_id
    err_unknown = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="missing",
    )
    assert "Error: Calendar 'missing' not found." in err_unknown

    # 5. Successful deletion by calendar_id
    mock_client_inst = MagicMock()
    mock_cal = MagicMock()
    mock_event = MagicMock()
    mock_event.vobject_instance.vevent.uid.value = "evt-1"
    mock_event.icalendar_component = {"summary": "Meeting"}
    mock_cal.events.return_value = [mock_event]
    mock_client_inst.calendar.return_value = mock_cal
    mock_client_inst.__enter__.return_value = mock_client_inst

    mock_dav_client = MagicMock(return_value=mock_client_inst)
    monkeypatch.setattr("caldav.DAVClient", mock_dav_client)

    ok_res = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="work",
    )
    assert "OK. Event 'Meeting' deleted from calendar." in ok_res
    mock_client_inst.calendar.assert_called_with(url="https://caldav.example.com/work")
    mock_event.delete.assert_called_once()


@pytest.mark.asyncio
async def test_confirmation_renderers_resolve_calendar_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "calendar_urls": [
                {
                    "id": "personal",
                    "name": "Personal",
                    "url": "https://caldav.example.com/personal",
                    "default": True,
                },
                {
                    "id": "work",
                    "name": "Work",
                    "url": "https://caldav.example.com/work",
                },
            ],
        }
    }
    tools_provider = MagicMock(spec=LocalToolsProvider)
    tools_provider.get_calendar_config.return_value = config
    ctx.tools_provider = tools_provider

    async def fake_fetch_details(
        uid: str, calendar_url: str, **kwargs: object
    ) -> CalendarEvent | None:
        assert calendar_url == "https://caldav.example.com/work"
        assert uid == "evt-123"
        return CalendarEvent(
            uid=uid,
            summary="Important Sync",
            start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
            all_day=False,
            calendar_url=calendar_url,
            similarity=None,
        )

    monkeypatch.setattr(
        "family_assistant.calendar_integration.fetch_event_details_for_confirmation",
        fake_fetch_details,
    )

    del_prompt = await render_delete_calendar_event_confirmation(
        args={"uid": "evt-123", "calendar_id": "work"},
        context=ctx,
    )
    assert "Important Sync" in del_prompt
    assert "Please confirm you want to *delete* the event:" in del_prompt

    mod_prompt = await render_modify_calendar_event_confirmation(
        args={
            "uid": "evt-123",
            "calendar_id": "work",
            "new_summary": "Renamed Sync",
        },
        context=ctx,
    )
    assert "Important Sync" in mod_prompt
    assert "Set summary to:" in mod_prompt
    assert "Renamed Sync" in mod_prompt

    # Add event confirmation shows the selected calendar
    add_prompt = await render_add_calendar_event_confirmation(
        args={
            "summary": "Team Offsite",
            "start_time": "2026-05-01T09:00:00Z",
            "end_time": "2026-05-01T17:00:00Z",
            "calendar_id": "work",
        },
        context=ctx,
    )
    assert "Team Offsite" in add_prompt
    assert "Work (work)" in add_prompt

    # Add event confirmation falls back to default calendar if calendar_id is omitted
    add_default_prompt = await render_add_calendar_event_confirmation(
        args={
            "summary": "Doctor Appointment",
            "start_time": "2026-05-01T09:00:00Z",
            "end_time": "2026-05-01T10:00:00Z",
        },
        context=ctx,
    )
    assert "Doctor Appointment" in add_default_prompt
    assert "Personal (personal)" in add_default_prompt


def test_search_calendar_events_output_untrusted_taint() -> None:
    descriptor = next(
        d for d in LOCAL_TOOL_DESCRIPTORS if d.name == "search_calendar_events"
    )
    assert ToolTag.OUTPUT_UNTRUSTED in descriptor.tags
    assert ToolTag.OUTPUT_TRUSTED not in descriptor.tags

    taint_source = derive_tool_result_taint_source(
        descriptor=descriptor, call_id="call_test"
    )
    assert taint_source is not None
    assert taint_source.source_id == "call_test"


async def test_resolve_target_caldav_url_conflict_rejection() -> None:
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "calendar_urls": [
                {
                    "url": "https://caldav.example.com/cal_a",
                    "id": "cal_a",
                    "name": "Cal A",
                },
                {
                    "url": "https://caldav.example.com/cal_b",
                    "id": "cal_b",
                    "name": "Cal B",
                },
            ],
        }
    }

    # Matching selectors: ok
    url, err = resolve_target_caldav_url(
        config,
        calendar_url="https://caldav.example.com/cal_a",
        calendar_id="cal_a",
    )
    assert err is None
    assert url == "https://caldav.example.com/cal_a"

    # Conflicting selectors: rejected
    url, err = resolve_target_caldav_url(
        config,
        calendar_url="https://caldav.example.com/cal_b",
        calendar_id="cal_a",
    )
    assert url is None
    assert err is not None
    assert "Conflicting calendar targets" in err

    ctx = _create_mock_context()
    # Modify tool execution also rejects conflict
    mod_result = await modify_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="cal_a",
        calendar_url="https://caldav.example.com/cal_b",
        new_summary="Changed",
    )
    assert "Conflicting calendar targets" in mod_result

    # Delete tool execution also rejects conflict
    del_result = await delete_calendar_event_tool(
        exec_context=ctx,
        calendar_config=config,
        uid="evt-1",
        calendar_id="cal_a",
        calendar_url="https://caldav.example.com/cal_b",
    )
    assert "Conflicting calendar targets" in del_result


async def test_search_calendar_events_chronological_sorting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _create_mock_context()
    config: CalendarConfig = {
        "caldav": {
            "username": "user",
            "password": "pwd",
            "calendar_urls": ["https://caldav.example.com/cal"],
        },
        "ical": {
            "urls": ["https://example.com/feed.ics"],
        },
    }

    # Simulate CalDAV returning event at 14:00 and iCal returning event at 10:00
    async def fake_search_events(
        exec_context: ToolExecutionContext,
        calendar_config: CalendarConfig,
        search_start: datetime,
        search_end: datetime,
        sources: list[Any] | None = None,
    ) -> list[CalendarSearchResult]:
        # Return unordered list
        return [
            {
                "summary": "Later CalDAV Event",
                "uid": "uid-later",
                "start": "2026-05-01 14:00 UTC",
                "end": "2026-05-01 15:00 UTC",
                "calendar_url": "https://caldav.example.com/cal",
                "start_dt": datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
            },
            {
                "summary": "Earlier iCal Event",
                "uid": "uid-earlier",
                "start": "2026-05-01 10:00 UTC",
                "end": "2026-05-01 11:00 UTC",
                "calendar_url": None,
                "source_name": "Flight Feed",
                "start_dt": datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            },
        ]

    monkeypatch.setattr(
        "family_assistant.tools.calendar._search_events_in_range",
        fake_search_events,
    )

    result = await search_calendar_events_tool(
        exec_context=ctx,
        calendar_config=config,
        start_date="2026-05-01",
        end_date="2026-05-02",
    )

    # When search_text is not provided, results are sorted chronologically
    pos_earlier = result.find("Earlier iCal Event")
    pos_later = result.find("Later CalDAV Event")
    assert pos_earlier != -1
    assert pos_later != -1
    assert pos_earlier < pos_later
