"""Tests for calendar event confirmation rendering with event detail fetching.

This test ensures that when calendar events are modified or deleted with confirmation,
the confirmation prompt properly displays the event details fetched from the calendar.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    fetch_event_details_for_confirmation,
)
from family_assistant.storage.context import get_db_context
from family_assistant.telegram_bot import telegramify_markdown
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
from family_assistant.tools.calendar import (
    add_calendar_event_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.confirmation import (
    render_delete_calendar_event_confirmation,
    render_modify_calendar_event_confirmation,
)
from family_assistant.tools.types import ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

TEST_TIMEZONE_STR = "Australia/Sydney"


async def create_test_event_in_radicale(
    radicale_server_details: tuple[str, str, str, str],
    event_summary: str,
    start_dt: datetime,
    end_dt: datetime,
    engine: AsyncEngine,
) -> str:
    """Helper to create an event in Radicale using the actual calendar tool and return its UID."""
    base_url, user, passwd, calendar_url = radicale_server_details

    # Use the actual add_calendar_event_tool to create the event

    calendar_config = {
        "caldav": {
            "username": user,
            "password": passwd,
            "base_url": base_url,
            "calendar_urls": [calendar_url],
        }
    }

    # Create a minimal database context for the test
    async with get_db_context(engine=engine) as db_ctx:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-create",
            user_name="TestUser",
            turn_id="test-turn-create",
            db_context=db_ctx,
            chat_interface=None,
            timezone_str=TEST_TIMEZONE_STR,
            request_confirmation_callback=None,
        )

        result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=calendar_config,
            summary=event_summary,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            all_day=False,
        )

        logger.info(f"Event creation result: {result}")

        # Now search for the event to get its UID

        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=calendar_config,
            search_text=event_summary,
        )

    # Extract UID from search result

    uid_match = re.search(r"UID: ([^\n]+)", search_result)
    if uid_match:
        uid = uid_match.group(1).strip()
        logger.info(f"Found event UID: {uid}")
        return uid
    else:
        raise ValueError(f"Could not find UID in search result: {search_result}")


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
        radicale_server, event_summary, start_dt, end_dt, pg_vector_db_engine
    )

    # Setup calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
            "calendar_urls": [test_calendar_url],
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
        radicale_server, event_summary, start_dt, end_dt, pg_vector_db_engine
    )

    # Setup calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
            "calendar_urls": [test_calendar_url],
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
        radicale_server, event_summary, start_dt, end_dt, pg_vector_db_engine
    )

    # Debug logging
    logger.info(f"Created event with UID: {event_uid}")
    logger.info(f"Using calendar URL: {test_calendar_url}")

    # Small delay to ensure event is saved
    await asyncio.sleep(0.1)

    # Setup providers with real calendar config
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "base_url": radicale_base_url,
            "calendar_urls": [test_calendar_url],
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
        interface_type: str,
        conversation_id: str,
        turn_id: str | None,
        tool_name: str,
        call_id: str,
        tool_args: dict[str, Any],
        timeout_seconds: float,
    ) -> bool:
        """Capture the confirmation prompt and accept it."""
        # For testing, we just capture that the callback was called with the right tool
        # We don't need to render the actual prompt here
        confirmation_prompts_shown.append(f"{tool_name} called with args: {tool_args}")
        return True  # Accept confirmation

    confirming_provider = ConfirmingToolsProvider(
        wrapped_provider=local_provider,
        tools_requiring_confirmation={"modify_calendar_event"},
        confirmation_timeout=10.0,  # Short timeout for tests (10s instead of default 1hr)
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

        # Verify confirmation was requested
        assert len(confirmation_prompts_shown) == 1, (
            f"Should have shown exactly one confirmation, but got {len(confirmation_prompts_shown)}: {confirmation_prompts_shown}"
        )
        confirmation_info = confirmation_prompts_shown[0]

        # Check that the right tool was called with the right arguments
        assert "modify_calendar_event called with args:" in confirmation_info, (
            f"Expected confirmation for modify_calendar_event, got: {confirmation_info}"
        )
        assert event_uid in confirmation_info, (
            f"Event UID '{event_uid}' not in confirmation info: {confirmation_info}"
        )
        assert "Quarterly Business Review" in confirmation_info, (
            f"New summary not in confirmation info: {confirmation_info}"
        )

        # The tool should have executed successfully

        result_text = result.text if isinstance(result, ToolResult) else str(result)
        assert (
            "updated" in result_text.lower()
            or "successfully" in result_text.lower()
            or "modified" in result_text.lower()
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
