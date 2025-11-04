import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import caldav
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    format_datetime_or_date,
)
from family_assistant.context_providers import CalendarContextProvider
from family_assistant.llm import LLMInterface, ToolCallFunction, ToolCallItem
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    TOOLS_DEFINITION as local_tools_definition,
)
from family_assistant.tools import (
    CompositeToolsProvider,
    LocalToolsProvider,
    MCPToolsProvider,
)
from family_assistant.tools.calendar import (
    search_calendar_events_tool,
)
from family_assistant.tools.types import CalendarConfig, ToolExecutionContext
from family_assistant.utils.clock import MockClock
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

logger = logging.getLogger(__name__)

TEST_CHAT_ID = "cal_test_chat_123"
TEST_USER_NAME = "CalendarTestUser"
TEST_TIMEZONE_STR = "Europe/Berlin"


@pytest.mark.asyncio
async def test_format_datetime_or_date_all_day_tomorrow_with_mock_clock() -> None:
    """
    Test that an all-day event for tomorrow is correctly formatted as "Tomorrow"
    using MockClock.
    """
    timezone_str = "America/New_York"
    local_tz = ZoneInfo(timezone_str)
    mock_now = datetime(2025, 6, 23, 10, 0, 0, tzinfo=local_tz)
    mock_clock = MockClock(initial_time=mock_now)

    event_dt = datetime(2025, 6, 24, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    formatted_str = format_datetime_or_date(
        dt_obj=event_dt, timezone_str=timezone_str, is_end=False, clock=mock_clock
    )

    assert "Tomorrow" in formatted_str


def get_radicale_client(
    radicale_server_details: tuple[str, str, str, str],
) -> caldav.DAVClient:
    """Helper to get a caldav client for the test Radicale server."""
    base_url, user, passwd, _ = radicale_server_details
    return caldav.DAVClient(url=base_url, username=user, password=passwd, timeout=30)


async def get_event_by_summary_from_radicale(
    radicale_server_details: tuple[str, str, str, str],
    event_summary: str,
) -> caldav.objects.Event | None:
    """Fetches an event by its summary from the specified calendar_url on Radicale."""
    base_url, user, passwd, calendar_url = radicale_server_details
    client = caldav.DAVClient(url=base_url, username=user, password=passwd, timeout=30)

    try:
        target_calendar = await asyncio.to_thread(client.calendar, url=calendar_url)
        if not target_calendar:
            logger.warning(f"Calendar not found at URL '{calendar_url}' on Radicale.")
            return None
    except Exception as e_get_cal:
        logger.error(
            f"Error getting calendar at URL '{calendar_url}': {e_get_cal}",
            exc_info=True,
        )
        return None

    events = await asyncio.to_thread(target_calendar.events)
    for event_obj in events:
        try:
            vevent = event_obj.vobject_instance.vevent  # type: ignore[attr-defined]
            if (
                vevent
                and hasattr(vevent, "summary")
                and vevent.summary.value == event_summary
            ):
                return event_obj
        except Exception as e:
            logger.error(f"Error parsing event data from Radicale: {e}", exc_info=True)
    return None


async def wait_for_radicale_indexing(
    exec_context: ToolExecutionContext,
    calendar_config: "CalendarConfig",
    event_summary: str,
    timeout_seconds: float = 5.0,
) -> bool:
    """
    Wait for Radicale to index a newly created event.

    Radicale CalDAV server doesn't immediately make events searchable after creation.
    This function polls the search functionality until the event appears or timeout.

    Args:
        exec_context: Tool execution context
        calendar_config: Calendar configuration
        event_summary: Summary of the event to wait for
        timeout_seconds: Maximum time to wait in seconds (default: 5.0)

    Returns:
        True if event became searchable, False if timeout
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=calendar_config,
            search_text=event_summary,
        )

        if event_summary in search_result:
            return True

        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling for calendar event to appear in search
        await asyncio.sleep(0.1)

    return False


@pytest.mark.asyncio
async def test_add_event_and_verify_in_system_prompt(
    db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test:
    1. LLM decides to add a calendar event.
    2. ProcessingService executes add_calendar_event_tool.
    3. Verify event exists in Radicale.
    4. Verify event appears in the system prompt context.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Add Event & Verify in System Prompt (Radicale URL: {test_calendar_direct_url}) ---"
    )

    event_summary = f"Test Meeting {uuid.uuid4()}"
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    tomorrow = datetime.now(local_tz) + timedelta(days=1)
    start_dt_local = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    end_dt_local = start_dt_local + timedelta(hours=1)

    start_time_iso = start_dt_local.isoformat()
    end_time_iso = end_dt_local.isoformat()

    tool_call_id = f"call_{uuid.uuid4()}"

    def add_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            f"schedule {event_summary.lower()}" in last_text
            and kwargs.get("tools") is not None
        )

    add_event_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event_summary,
                        "start_time": start_time_iso,
                        "end_time": end_time_iso,
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id
            and "OK. Event '" in (last_message.content or "")
            and f"'{event_summary}' added" in (last_message.content or "")
        )

    final_llm_response_content = (
        f"Alright, the event '{event_summary}' has been scheduled successfully."
    )
    final_response_llm_output = MockLLMOutput(
        content=final_llm_response_content, tool_calls=None
    )

    llm_client_for_add_test: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (add_event_matcher, add_event_response),
            (final_response_matcher, final_response_llm_output),
        ]
    )

    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": radicale_base_url,
                "username": r_user,
                "password": r_pass,
                "calendar_urls": [test_calendar_direct_url],
            },
            "ical": {"urls": []},
        },
    )
    dummy_prompts = {
        "system_prompt": "System Time: {current_time}\nAggregated Context:\n{aggregated_other_context}"
    }

    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        calendar_config=test_calendar_config,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_add_profile",
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client_for_add_test,
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    user_message_create = f"Please schedule {event_summary} for tomorrow at 10 AM."
    async with DatabaseContext(engine=db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_create}],
            trigger_interface_message_id="msg_add_event_prompt_test",
            user_name=TEST_USER_NAME,
        )
        final_reply = result.text_reply
        error_create = result.error_traceback

    assert error_create is None, f"Error during event creation: {error_create}"
    assert final_reply and final_llm_response_content in final_reply, (
        f"Expected creation reply '{final_llm_response_content}', but got '{final_reply}'"
    )

    radicale_event_check = await get_event_by_summary_from_radicale(
        radicale_server, event_summary
    )
    assert radicale_event_check is not None, (
        f"Event '{event_summary}' not found in Radicale {test_calendar_direct_url} after tool execution."
    )

    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for calendar sync to context providers
    await asyncio.sleep(0.5)

    aggregated_context_str = (
        await processing_service._aggregate_context_from_providers()
    )

    logger.info(
        f"Generated aggregated context for verification:\n{aggregated_context_str}"
    )

    expected_time_str_in_prompt = "Tomorrow 10:00"
    assert event_summary in aggregated_context_str, (
        "Event summary not found in aggregated context string."
    )
    assert expected_time_str_in_prompt in aggregated_context_str, (
        f"Expected time '{expected_time_str_in_prompt}' not found in aggregated context string."
    )

    logger.info("Test Add Event & Verify in System Prompt PASSED.")
