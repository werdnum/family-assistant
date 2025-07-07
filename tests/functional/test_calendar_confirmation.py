"""Tests for calendar event confirmation rendering with event detail fetching.

This test ensures that when calendar events are modified or deleted with confirmation,
the confirmation prompt properly displays the event details fetched from the calendar.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import caldav
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    fetch_event_details_for_confirmation,
)
from family_assistant.storage.context import get_db_context
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    ConfirmingToolsProvider,
    LocalToolsProvider,
)
from family_assistant.tools.confirmation import (
    render_delete_calendar_event_confirmation,
    render_modify_calendar_event_confirmation,
)
from family_assistant.tools.types import ToolExecutionContext

logger = logging.getLogger(__name__)

TEST_TIMEZONE_STR = "Australia/Sydney"


async def create_test_event_in_radicale(
    radicale_server_details: tuple[str, str, str, str],
    event_summary: str,
    start_dt: datetime,
    end_dt: datetime,
) -> str:
    """Helper to create an event in Radicale and return its UID."""
    base_url, user, passwd, calendar_url = radicale_server_details

    # Create event using the calendar integration tool
    event_uid = f"test-{uuid.uuid4()}@example.com"

    # Use sync caldav client to create event
    def create_event_sync() -> str:
        with caldav.DAVClient(
            url=base_url, username=user, password=passwd, timeout=30
        ) as client:
            calendar = client.calendar(url=calendar_url)

            # Create vEvent using vobject
            import vobject

            cal = vobject.iCalendar()
            vevent = cal.add("vevent")
            vevent.add("summary").value = event_summary
            vevent.add("dtstart").value = start_dt
            vevent.add("dtend").value = end_dt
            vevent.add("uid").value = event_uid

            # Save to calendar
            calendar.save_event(cal.serialize())
            return event_uid

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, create_event_sync)
    return event_uid


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_modify_calendar_event_confirmation_shows_event_details(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """Test that modify_calendar_event confirmation properly fetches and displays real event details."""

    print("\n\n=== TEST STARTING ===\n\n")

    radicale_base_url, r_user, r_pass, test_calendar_url = radicale_server

    # Create a real event in Radicale
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    event_summary = f"Team Meeting {uuid.uuid4()}"
    start_dt = datetime.now(local_tz).replace(
        hour=14, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    end_dt = start_dt + timedelta(hours=1)

    event_uid = await create_test_event_in_radicale(
        radicale_server, event_summary, start_dt, end_dt
    )

    # Setup calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
        }
    }

    # Test fetching event details directly
    fetched_details = await fetch_event_details_for_confirmation(
        uid=event_uid,
        calendar_url=test_calendar_url,
        calendar_config=test_calendar_config,
    )

    # Verify we can fetch the event
    assert fetched_details is not None, "Failed to fetch event details from calendar"
    assert fetched_details["uid"] == event_uid
    assert fetched_details["summary"] == event_summary

    # Test the confirmation renderer with real event details
    test_args = {
        "uid": event_uid,
        "calendar_url": test_calendar_url,
        "new_summary": "Education Open Day",
        "new_start_time": start_dt.replace(hour=9).isoformat(),
        "new_end_time": start_dt.replace(hour=12).isoformat(),
    }

    confirmation_prompt = render_modify_calendar_event_confirmation(
        args=test_args,
        event_details=fetched_details,
        timezone_str=TEST_TIMEZONE_STR,
    )

    # Debug: print the confirmation prompt
    print(f"\n\nDEBUG: Confirmation prompt:\n{confirmation_prompt}\n\n")
    print(f"DEBUG: Looking for event_summary: {event_summary}")
    print(f"DEBUG: Event details: {fetched_details}")

    # Also check for escaped version of the summary since confirmation uses telegramify_markdown
    from family_assistant.telegram_bot import telegramify_markdown

    escaped_summary = telegramify_markdown.escape_markdown(event_summary)
    print(f"DEBUG: Escaped event_summary: {escaped_summary}")

    # Verify the confirmation prompt contains both original and new details
    # Check for either escaped or unescaped version
    assert (
        event_summary in confirmation_prompt or escaped_summary in confirmation_prompt
    ), (
        f"Original event summary not in confirmation. Looking for '{event_summary}' or '{escaped_summary}'"
    )
    assert "Education Open Day" in confirmation_prompt, (
        "New summary not in confirmation"
    )
    assert "Event details not found" not in confirmation_prompt, (
        "Should not show error message"
    )

    # Verify it formats the event time correctly
    # The exact format depends on format_datetime_or_date but should mention the time
    assert (
        "14:00" in confirmation_prompt
        or "2:00" in confirmation_prompt
        or "2 PM" in confirmation_prompt
    ), "Original event time not properly formatted in confirmation"


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_delete_calendar_event_confirmation_shows_event_details(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """Test that delete_calendar_event confirmation properly fetches and displays real event details."""

    radicale_base_url, r_user, r_pass, test_calendar_url = radicale_server

    # Create a real event in Radicale
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    event_summary = f"Doctor Appointment {uuid.uuid4()}"
    start_dt = datetime.now(local_tz).replace(
        hour=15, minute=30, second=0, microsecond=0
    ) + timedelta(days=2)
    end_dt = start_dt + timedelta(hours=1)

    event_uid = await create_test_event_in_radicale(
        radicale_server, event_summary, start_dt, end_dt
    )

    # Setup calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
        }
    }

    # Test fetching event details
    fetched_details = await fetch_event_details_for_confirmation(
        uid=event_uid,
        calendar_url=test_calendar_url,
        calendar_config=test_calendar_config,
    )

    assert fetched_details is not None

    # Test the delete confirmation renderer
    test_args = {
        "uid": event_uid,
        "calendar_url": test_calendar_url,
    }

    confirmation_prompt = render_delete_calendar_event_confirmation(
        args=test_args,
        event_details=fetched_details,
        timezone_str=TEST_TIMEZONE_STR,
    )

    # Verify the confirmation prompt
    # The confirmation uses telegramify_markdown which escapes special characters
    from family_assistant.telegram_bot import telegramify_markdown

    escaped_summary = telegramify_markdown.escape_markdown(event_summary)

    assert (
        event_summary in confirmation_prompt or escaped_summary in confirmation_prompt
    ), (
        f"Event summary '{event_summary}' (or escaped '{escaped_summary}') not in delete confirmation"
    )
    assert "delete" in confirmation_prompt.lower(), "Delete action not mentioned"
    assert "Event details not found" not in confirmation_prompt


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_confirming_tools_provider_with_calendar_events(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """Test the full ConfirmingToolsProvider flow with real calendar events."""

    radicale_base_url, r_user, r_pass, test_calendar_url = radicale_server

    # Create a real event
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    event_summary = f"Project Review {uuid.uuid4()}"
    start_dt = datetime.now(local_tz).replace(
        hour=10, minute=0, second=0, microsecond=0
    ) + timedelta(days=3)
    end_dt = start_dt + timedelta(hours=2)

    event_uid = await create_test_event_in_radicale(
        radicale_server, event_summary, start_dt, end_dt
    )

    # Setup providers with real calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
        }
    }

    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        calendar_config=test_calendar_config,
    )

    # Track confirmation prompts
    confirmation_prompts_shown = []

    async def capture_confirmation_callback(
        conversation_id: str,
        interface_type: str,
        turn_id: str | None,
        prompt_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
        timeout: float,
    ) -> bool:
        """Capture the confirmation prompt and accept it."""
        confirmation_prompts_shown.append(prompt_text)
        return True  # Accept confirmation

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=local_provider,
        tools_requiring_confirmation={"modify_calendar_event"},
        confirmation_timeout=60.0,
    )

    # Create execution context
    async with get_db_context(engine=pg_vector_db_engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-conv-confirm",
            user_name="TestUser",
            turn_id="test-turn-1",
            db_context=db_ctx,
            chat_interface=None,
            timezone_str=TEST_TIMEZONE_STR,
            request_confirmation_callback=capture_confirmation_callback,
        )

        # Execute modify with confirmation
        test_args = {
            "uid": event_uid,
            "calendar_url": test_calendar_url,
            "new_summary": "Quarterly Business Review",
        }

        result = await confirming_provider.execute_tool(
            name="modify_calendar_event",
            arguments=test_args,
            context=exec_context,
        )

        # Verify confirmation was requested with proper event details
        assert len(confirmation_prompts_shown) == 1, (
            "Should have shown exactly one confirmation"
        )
        confirmation_prompt = confirmation_prompts_shown[0]

        # Original event details should be shown
        # The confirmation uses telegramify_markdown which escapes special characters
        from family_assistant.telegram_bot import telegramify_markdown

        escaped_summary = telegramify_markdown.escape_markdown(event_summary)

        assert (
            event_summary in confirmation_prompt
            or escaped_summary in confirmation_prompt
        ), (
            f"Original event summary '{event_summary}' (or escaped '{escaped_summary}') not in confirmation prompt"
        )
        assert "Quarterly Business Review" in confirmation_prompt, (
            "New summary not shown"
        )
        assert "Event details not found" not in confirmation_prompt, (
            f"Should not show 'Event details not found'. Full prompt:\n{confirmation_prompt}"
        )

        # The tool should have executed successfully
        assert (
            "updated" in result.lower()
            or "successfully" in result.lower()
            or "modified" in result.lower()
        ), f"Tool execution failed: {result}"


@pytest.mark.asyncio
async def test_confirmation_when_event_not_found() -> None:
    """Test that confirmation handles gracefully when event doesn't exist."""

    # Test with non-existent event
    test_args = {
        "uid": "non-existent-event@example.com",
        "calendar_url": "https://example.com/calendar/",
        "new_summary": "This will fail",
    }

    # Render confirmation with no event details (simulating fetch failure)
    confirmation_prompt = render_modify_calendar_event_confirmation(
        args=test_args,
        event_details=None,
        timezone_str=TEST_TIMEZONE_STR,
    )

    # Should show error message but still show the changes
    assert "Event details not found" in confirmation_prompt
    assert "This will fail" in confirmation_prompt
