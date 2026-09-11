"""Unit tests for calendar tools."""

from __future__ import annotations

from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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
    list_calendars_tool,
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
