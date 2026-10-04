import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import caldav
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    format_datetime_or_date,
)
from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import (
    ProcessingService,
    ProcessingServiceConfig,
)
from family_assistant.storage.database import Database
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
from family_assistant.utils.clock import MockClock
from tests.mocks.mock_llm import (
    LLMOutput as MockLLMOutput,
)
from tests.mocks.mock_llm import (
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

if TYPE_CHECKING:
    from family_assistant.tools.types import CalendarConfig

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
    local_tz = ZoneInfo("America/New_York")
    mock_now = datetime(2025, 6, 23, 10, 0, 0, tzinfo=local_tz)
    mock_clock = MockClock(initial_time=mock_now)

    event_dt = datetime(2025, 6, 24, 0, 0, 0, tzinfo=ZoneInfo("UTC"))

    formatted_str = format_datetime_or_date(
        dt_obj=event_dt, timezone=local_tz, is_end=False, clock=mock_clock
    )

    assert formatted_str == "Tomorrow (Jun 24)"


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
        logger.exception(f"Error getting calendar at URL '{calendar_url}': {e_get_cal}")
        return None

    events = await asyncio.to_thread(target_calendar.events)
    for event_obj in events:
        try:
            vevent = event_obj.vobject_instance.vevent
            if (
                vevent
                and hasattr(vevent, "summary")
                and vevent.summary.value == event_summary
            ):
                return event_obj
        except Exception as e:
            logger.exception(f"Error parsing event data from Radicale: {e}")
    return None


@pytest.mark.asyncio
async def test_add_event(
    db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test:
    1. LLM decides to add a calendar event.
    2. ProcessingService executes add_calendar_event_tool.
    3. Verify event exists in Radicale.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(f"\n--- Test: Add Event (Radicale URL: {test_calendar_direct_url}) ---")

    event_summary = f"Test Meeting {uuid.uuid4()}"
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    clock = MockClock(initial_time=datetime.now(local_tz))
    tomorrow = clock.now() + timedelta(days=1)
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

    llm_client_for_add_test = RuleBasedMockLLMClient(
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
    dummy_prompts = {"system_prompt": "You are a helpful assistant."}

    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    service_config = ProcessingServiceConfig(
        id="test_cal_add_profile",
        prompts=dummy_prompts,
        timezone=ZoneInfo(TEST_TIMEZONE_STR),
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config=ToolsConfig(),
        delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        include_aggregated_context=True,
        calendar_config=test_calendar_config,
    )
    processing_service = ProcessingService(
        llm_client=llm_client_for_add_test,
        tools_provider=composite_provider,
        context_providers=[],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
        credential_resolvers=None,
        api_backend=None,
    )

    user_message_create = f"Please schedule {event_summary} for tomorrow at 10 AM."
    db_context = Database(engine=db_engine)
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

    logger.info("Test Add Event PASSED.")
