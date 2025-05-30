import asyncio
import json
import logging
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import caldav
import pytest
import vobject  # Added import for vobject
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    format_datetime_or_date,  # Added import
)
from family_assistant.context_providers import CalendarContextProvider
from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.context import DatabaseContext, get_db_context
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

if TYPE_CHECKING:
    from family_assistant.llm import LLMInterface

# RADICALE_TEST_CALENDAR_NAME is no longer needed as direct URL is provided by fixture
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
TEST_TIMEZONE_STR = "Europe/Berlin"  # Example timezone for tests


def get_radicale_client(
    radicale_server_details: tuple[str, str, str, str],
) -> caldav.DAVClient:
    """Helper to get a caldav client for the test Radicale server."""
    base_url, user, passwd, _ = radicale_server_details
    return caldav.DAVClient(url=base_url, username=user, password=passwd, timeout=30)


async def get_event_by_summary_from_radicale(
    radicale_server_details: tuple[str, str, str, str],  # Includes calendar_url now
    event_summary: str,
) -> caldav.objects.Event | None:
    """Fetches an event by its summary from the specified calendar_url on Radicale."""
    base_url, user, passwd, calendar_url = radicale_server_details
    client = caldav.DAVClient(
        url=base_url, username=user, password=passwd, timeout=30
    )  # Use base_url for client

    try:
        # Get the calendar object directly using its full URL
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
    for event_obj in events:  # event_obj is caldav.objects.Event
        # caldav.objects.Event.data is a vobject.base.Component
        # We need to access the VEVENT component and then its summary.
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


@pytest.mark.asyncio
async def test_add_event_and_verify_in_system_prompt(
    pg_vector_db_engine: AsyncEngine,  # Renamed from test_db_engine to use Postgres
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
    # Times relative to TEST_TIMEZONE_STR
    # Let's make it tomorrow 10 AM in TEST_TIMEZONE_STR
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    tomorrow = datetime.now(local_tz) + timedelta(days=1)
    start_dt_local = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    end_dt_local = start_dt_local + timedelta(hours=1)

    # Tools expect ISO 8601 with timezone
    start_time_iso = start_dt_local.isoformat()
    end_time_iso = end_dt_local.isoformat()

    tool_call_id = f"call_{uuid.uuid4()}"

    # --- LLM Rule for Adding Event ---
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
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[(add_event_matcher, add_event_response)]
    )

    # --- Setup ProcessingService ---
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts = {
        "system_prompt": "System: {calendar_today_tomorrow}\nFuture: {calendar_next_two_weeks}"
    }

    local_provider = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        calendar_config=test_calendar_config,  # Pass config to local provider
    )
    mcp_provider = MCPToolsProvider(mcp_server_configs={})
    composite_provider = CompositeToolsProvider(
        providers=[local_provider, mcp_provider]
    )
    await composite_provider.get_tool_definitions()

    # Corrected factory function
    def get_test_db_context_factory() -> AbstractAsyncContextManager[DatabaseContext]:
        return get_db_context(engine=pg_vector_db_engine)

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )

    service_config = ProcessingServiceConfig(
        id="test_calendar_profile",
        prompts=dummy_prompts,
        calendar_config=test_calendar_config,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},  # No confirmation for these tests
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction ---
    user_message = f"Please schedule {event_summary} for tomorrow at 10 AM."
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        final_reply, _, _, error = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message}],
            trigger_interface_message_id="msg1",
            user_name=TEST_USER_NAME,
        )

    assert error is None, f"Error during chat interaction: {error}"
    assert final_reply and f"OK, I'll schedule '{event_summary}'." in final_reply

    # --- Verify Event in Radicale ---
    # radicale_server now contains the direct calendar URL as the 4th element
    radicale_event = await get_event_by_summary_from_radicale(
        radicale_server, event_summary
    )
    assert radicale_event is not None, (
        f"Event '{event_summary}' not found in Radicale calendar {radicale_server[3]}."
    )

    # More detailed verification of event properties in Radicale if needed (e.g., start/end times)
    # This requires parsing radicale_event.data (VCALENDAR string)
    # For now, presence is the main check.

    # --- Verify Event in System Prompt ---
    # Allow some time for Radicale to process and for our app to potentially cache/fetch
    await asyncio.sleep(0.5)

    # _aggregate_context_from_providers returns a single string
    aggregated_context_str = (
        await processing_service._aggregate_context_from_providers()
    )

    logger.info(
        f"Generated aggregated context for verification:\n{aggregated_context_str}"
    )

    # Check for summary and a characteristic part of the formatted time
    # format_datetime_or_date for tomorrow 10:00 should be "Tomorrow 10:00"
    # The formatting depends on CalendarContextProvider's internal prompts
    expected_time_str_in_prompt = "Tomorrow 10:00"
    assert event_summary in aggregated_context_str, (
        "Event summary not found in aggregated context string."
    )
    assert expected_time_str_in_prompt in aggregated_context_str, (
        f"Expected time '{expected_time_str_in_prompt}' not found in aggregated context string."
    )

    logger.info("Test Add Event & Verify in System Prompt PASSED.")


@pytest.mark.asyncio
async def test_modify_event(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test:
    1. Create an event directly in Radicale.
    2. LLM decides to modify this event.
    3. ProcessingService executes modify_calendar_event_tool.
    4. Verify event is modified in Radicale.
    5. Verify modified event appears correctly in the system prompt.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Modify Event (Radicale URL: {test_calendar_direct_url}) ---"
    )

    original_summary = f"Original Event {uuid.uuid4()}"
    modified_summary = f"Modified Event {uuid.uuid4()}"

    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    day_after_tomorrow = datetime.now(local_tz) + timedelta(days=2)
    original_start_dt = day_after_tomorrow.replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    original_end_dt = original_start_dt + timedelta(hours=1)
    modified_start_dt = original_start_dt.replace(hour=15)  # Change to 3 PM
    # modified_end_dt was unused, if needed:

    # --- Create initial event directly in Radicale using vobject ---
    # radicale_server now contains the direct calendar URL as the 4th element
    base_url, r_user_modify, r_pass_modify, unique_calendar_url_modify = radicale_server
    client = caldav.DAVClient(
        url=base_url, username=r_user_modify, password=r_pass_modify, timeout=30
    )

    try:
        target_calendar = await asyncio.to_thread(
            client.calendar, url=unique_calendar_url_modify
        )
        assert target_calendar is not None, (
            f"Test calendar not found at URL '{unique_calendar_url_modify}'."
        )
    except Exception as e_get_cal_mod:
        pytest.fail(f"Failed to get calendar for modification test: {e_get_cal_mod}")

    # Use vobject to create the VCALENDAR string
    cal = vobject.iCalendar()  # type: ignore[attr-defined]
    vevent = cal.add("vevent")  # type: ignore[attr-defined]
    event_uid_val = str(uuid.uuid4())
    vevent.add("uid").value = event_uid_val  # type: ignore[attr-defined]
    vevent.add("summary").value = original_summary  # type: ignore[attr-defined]
    vevent.add("dtstart").value = original_start_dt  # type: ignore[attr-defined] # vobject handles aware datetime
    vevent.add("dtend").value = original_end_dt  # type: ignore[attr-defined]   # vobject handles aware datetime
    vevent.add("dtstamp").value = datetime.now(timezone.utc)  # type: ignore[attr-defined]
    event_vcal_str = cal.serialize()  # type: ignore[attr-defined]

    # add_event returns the caldav.objects.Event object after saving
    created_event_object = await asyncio.to_thread(
        target_calendar.add_event, vcal=event_vcal_str
    )
    # Ensure UID is accessible from the created event object if needed, or use the one we generated
    event_uid = created_event_object.vobject_instance.vevent.uid.value  # type: ignore[attr-defined]
    assert event_uid == event_uid_val  # Verify UID consistency
    logger.info(
        f"Directly created event '{original_summary}' with UID {event_uid} in Radicale using vobject."
    )

    # --- LLM Rules ---
    # Rule 1: Search for the event (optional, LLM could "know" the UID)
    # For simplicity, we'll assume LLM gets the UID and proceeds to modify.

    # Rule 2: Modify the event
    tool_call_id_modify = f"call_mod_{uuid.uuid4()}"

    def modify_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return f"change event {original_summary.lower()}" in last_text

    modify_event_response = MockLLMOutput(
        content=f"OK, I'll modify '{original_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_modify,
                type="function",
                function=ToolCallFunction(
                    name="modify_calendar_event",
                    arguments=json.dumps({
                        "uid": event_uid,
                        "calendar_url": test_calendar_direct_url,  # Tool needs the direct calendar URL
                        "new_summary": modified_summary,
                        "new_start_time": modified_start_dt.isoformat(),
                    }),
                ),
            )
        ],
    )
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[(modify_event_matcher, modify_event_response)]
    )

    # --- Setup ProcessingService (similar to add_event test) ---
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts = {
        "system_prompt": "System: {calendar_today_tomorrow}\nFuture: {calendar_next_two_weeks}"
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

    def get_test_db_context_factory() -> AbstractAsyncContextManager[DatabaseContext]:
        return get_db_context(engine=pg_vector_db_engine)

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_mod_profile",
        prompts=dummy_prompts,
        calendar_config=test_calendar_config,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction to Modify ---
    user_message_modify = f"Please change event {original_summary} to start at 3 PM and call it {modified_summary}."
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply_modify,
            _,
            _,
            error_modify,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_modify}],
            trigger_interface_message_id="msg_mod",
            user_name=TEST_USER_NAME,
        )

    assert error_modify is None, f"Error during modify chat interaction: {error_modify}"
    assert (
        final_reply_modify
        and f"OK. Event '{modified_summary}' updated." in final_reply_modify
    )  # Check tool's success message

    # --- Verify Event in Radicale ---
    modified_radicale_event = await get_event_by_summary_from_radicale(
        radicale_server, modified_summary
    )
    assert modified_radicale_event is not None, (
        f"Event '{modified_summary}' not found in Radicale calendar {radicale_server[3]} after modification."
    )

    # Verify original summary event is gone
    original_radicale_event_after_modify = await get_event_by_summary_from_radicale(
        radicale_server, original_summary
    )
    assert original_radicale_event_after_modify is None, (
        f"Event with original summary '{original_summary}' still found in Radicale calendar {radicale_server[3]} after modification."
    )

    # --- Verify Modified Event in System Prompt ---
    await asyncio.sleep(0.5)
    aggregated_context_str_mod = (
        await processing_service._aggregate_context_from_providers()
    )
    logger.info(
        f"Generated aggregated context after modification:\n{aggregated_context_str_mod}"
    )

    formatted_day_after_tomorrow = format_datetime_or_date(
        day_after_tomorrow.date(), TEST_TIMEZONE_STR
    )
    expected_time_str_in_prompt_mod = f"{formatted_day_after_tomorrow} 15:00"  # 3 PM
    assert modified_summary in aggregated_context_str_mod, (
        "Modified event summary not found in aggregated context string."
    )
    assert expected_time_str_in_prompt_mod in aggregated_context_str_mod, (
        f"Expected modified time '{expected_time_str_in_prompt_mod}' not found in aggregated context string."
    )
    assert original_summary not in aggregated_context_str_mod, (
        "Original event summary still found in aggregated context string after modification."
    )

    logger.info("Test Modify Event PASSED.")


@pytest.mark.asyncio
async def test_delete_event(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test:
    1. Create an event directly in Radicale.
    2. LLM decides to delete this event.
    3. ProcessingService executes delete_calendar_event_tool.
    4. Verify event is deleted from Radicale.
    5. Verify event no longer appears in the system prompt.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Delete Event (Radicale URL: {test_calendar_direct_url}) ---"
    )

    event_to_delete_summary = f"Event To Delete {uuid.uuid4()}"
    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    event_start_dt = (datetime.now(local_tz) + timedelta(days=3)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    event_end_dt = event_start_dt + timedelta(hours=1)

    # --- Create initial event directly in Radicale using vobject ---
    base_url_del, r_user_del, r_pass_del, unique_calendar_url_del = radicale_server
    client = caldav.DAVClient(
        url=base_url_del, username=r_user_del, password=r_pass_del, timeout=30
    )

    try:
        target_calendar = await asyncio.to_thread(
            client.calendar, url=unique_calendar_url_del
        )
        assert target_calendar is not None, (
            f"Test calendar not found at URL '{unique_calendar_url_del}'."
        )
    except Exception as e_get_cal_del:
        pytest.fail(f"Failed to get calendar for deletion test: {e_get_cal_del}")

    cal_del = vobject.iCalendar()  # type: ignore[attr-defined]
    vevent_del = cal_del.add("vevent")  # type: ignore[attr-defined]
    event_uid_del_val = str(uuid.uuid4())
    vevent_del.add("uid").value = event_uid_del_val  # type: ignore[attr-defined]
    vevent_del.add("summary").value = event_to_delete_summary  # type: ignore[attr-defined]
    vevent_del.add("dtstart").value = event_start_dt  # type: ignore[attr-defined]
    vevent_del.add("dtend").value = event_end_dt  # type: ignore[attr-defined]
    vevent_del.add("dtstamp").value = datetime.now(timezone.utc)  # type: ignore[attr-defined]
    event_vcal_del_str = cal_del.serialize()  # type: ignore[attr-defined]

    created_event_object_del = await asyncio.to_thread(
        target_calendar.add_event, vcal=event_vcal_del_str
    )
    event_uid_del = created_event_object_del.vobject_instance.vevent.uid.value  # type: ignore[attr-defined]
    assert event_uid_del == event_uid_del_val
    logger.info(
        f"Directly created event '{event_to_delete_summary}' with UID {event_uid_del} for deletion test using vobject."
    )

    # --- LLM Rule for Deleting Event ---
    tool_call_id_delete = f"call_del_{uuid.uuid4()}"

    def delete_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return f"delete event {event_to_delete_summary.lower()}" in last_text

    delete_event_response = MockLLMOutput(
        content=f"OK, I'll delete '{event_to_delete_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_delete,
                type="function",
                function=ToolCallFunction(
                    name="delete_calendar_event",
                    arguments=json.dumps({
                        "uid": event_uid_del,
                        "calendar_url": test_calendar_direct_url,
                    }),
                ),
            )
        ],
    )
    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[(delete_event_matcher, delete_event_response)]
    )

    # --- Setup ProcessingService (similar to other tests) ---
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts = {
        "system_prompt": "System: {calendar_today_tomorrow}\nFuture: {calendar_next_two_weeks}"
    }  # type: ignore
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

    def get_test_db_context_factory() -> AbstractAsyncContextManager[DatabaseContext]:
        return get_db_context(engine=pg_vector_db_engine)

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_del_profile",
        prompts=dummy_prompts,  # type: ignore
        calendar_config=test_calendar_config,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction to Delete ---
    user_message_delete = f"Please delete event {event_to_delete_summary}."
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply_delete,
            _,
            _,
            error_delete,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_delete}],
            trigger_interface_message_id="msg_del",
            user_name=TEST_USER_NAME,
        )

    assert error_delete is None, f"Error during delete chat interaction: {error_delete}"
    assert (
        final_reply_delete
        and f"OK. Event '{event_to_delete_summary}' deleted." in final_reply_delete
    )

    # --- Verify Event in Radicale ---
    deleted_radicale_event = await get_event_by_summary_from_radicale(
        radicale_server, event_to_delete_summary
    )
    assert deleted_radicale_event is None, (
        f"Event '{event_to_delete_summary}' still found in Radicale calendar {radicale_server[3]} after deletion."
    )

    # --- Verify Event NOT in System Prompt ---
    await asyncio.sleep(0.5)
    aggregated_context_str_del = (
        await processing_service._aggregate_context_from_providers()
    )
    logger.info(
        f"Generated aggregated context after deletion:\n{aggregated_context_str_del}"
    )

    assert event_to_delete_summary not in aggregated_context_str_del, (
        "Deleted event summary still found in aggregated context string."
    )

    logger.info("Test Delete Event PASSED.")


@pytest.mark.asyncio
async def test_search_events(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test:
    1. Create a few events directly in Radicale.
    2. LLM decides to search for events.
    3. ProcessingService executes search_calendar_events_tool.
    4. LLM receives search results and formulates a reply.
    5. Verify the LLM's reply contains info about the created events.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Search Events (Radicale URL: {test_calendar_direct_url}) ---"
    )

    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    search_day = datetime.now(local_tz) + timedelta(days=4)

    event1_summary = f"Search Event Alpha {uuid.uuid4()}"
    event1_start = search_day.replace(hour=9, minute=0, second=0, microsecond=0)
    event1_end = event1_start + timedelta(hours=1)

    event2_summary = f"Search Event Bravo {uuid.uuid4()}"
    event2_start = search_day.replace(hour=13, minute=0, second=0, microsecond=0)
    event2_end = event2_start + timedelta(hours=2)

    # --- Create events directly in Radicale ---
    base_url_search, r_user_search, r_pass_search, unique_calendar_url_search = (
        radicale_server
    )
    client = caldav.DAVClient(
        url=base_url_search, username=r_user_search, password=r_pass_search, timeout=30
    )

    try:
        target_calendar = await asyncio.to_thread(
            client.calendar, url=unique_calendar_url_search
        )
        assert target_calendar is not None, (
            f"Test calendar not found at URL '{unique_calendar_url_search}'."
        )
    except Exception as e_get_cal_search:
        pytest.fail(f"Failed to get calendar for search test: {e_get_cal_search}")

    for summ, st, en in [
        (event1_summary, event1_start, event1_end),
        (event2_summary, event2_start, event2_end),
    ]:
        cal_search = vobject.iCalendar()  # type: ignore[attr-defined]
        vevent_search = cal_search.add("vevent")  # type: ignore[attr-defined]
        vevent_search.add("uid").value = str(uuid.uuid4())  # type: ignore[attr-defined]
        vevent_search.add("summary").value = summ  # type: ignore[attr-defined]
        vevent_search.add("dtstart").value = st  # type: ignore[attr-defined]
        vevent_search.add("dtend").value = en  # type: ignore[attr-defined]
        vevent_search.add("dtstamp").value = datetime.now(timezone.utc)  # type: ignore[attr-defined]
        event_vcal_search_str = cal_search.serialize()  # type: ignore[attr-defined]
        await asyncio.to_thread(target_calendar.add_event, vcal=event_vcal_search_str)
    logger.info(
        f"Directly created '{event1_summary}' and '{event2_summary}' for search test using vobject."
    )

    # --- LLM Rules ---
    tool_call_id_search = f"call_search_{uuid.uuid4()}"
    search_query_text = (
        "events for day after tomorrow plus two"  # A bit vague to test search
    )

    # Rule 1: LLM decides to search
    def search_intent_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return "what are my events" in last_text and search_query_text in last_text

    search_intent_response = MockLLMOutput(
        content="Let me check your calendar...",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_search,
                type="function",
                function=ToolCallFunction(
                    name="search_calendar_events",
                    arguments=json.dumps({
                        "query_text": search_query_text,  # LLM might use a more specific query
                        "start_date_str": search_day.strftime("%Y-%m-%d"),
                        "end_date_str": (search_day + timedelta(days=1)).strftime(
                            "%Y-%m-%d"
                        ),  # Search for one day
                    }),
                ),
            )
        ],
    )

    # Rule 2: LLM processes search results and replies to user
    # This rule's matcher needs to look for the tool's output in the message history
    def present_search_results_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        # Last message is assistant's thinking, previous is tool's output
        last_msg = messages[-1]
        prev_msg = messages[-2]
        return (
            prev_msg.get("role") == "tool"
            and prev_msg.get("tool_call_id") == tool_call_id_search
            and event1_summary.lower()
            in prev_msg.get(
                "content", ""
            ).lower()  # Check if tool output contains expected event
            and last_msg.get("role")
            == "user"  # This is actually the system prompt for the LLM to generate the *next* response
        )

    present_search_results_response = MockLLMOutput(
        content=f"Found these events: {event1_summary} at 9 AM and {event2_summary} at 1 PM.",
        tool_calls=None,
    )

    llm_client: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (search_intent_matcher, search_intent_response),
            (present_search_results_matcher, present_search_results_response),
        ]
    )

    # --- Setup ProcessingService (similar to other tests) ---
    test_calendar_config = {
        "caldav": {
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts: dict[str, Any] = {
        "system_prompt": "System: {calendar_today_tomorrow}\nFuture: {calendar_next_two_weeks}"
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

    def get_test_db_context_factory() -> AbstractAsyncContextManager[DatabaseContext]:
        return get_db_context(engine=pg_vector_db_engine)

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_search_profile",
        prompts=dummy_prompts,
        calendar_config=test_calendar_config,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction ---
    user_message_search = f"What are my events for {search_query_text}?"
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply_search,
            _,
            _,
            error_search,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_search}],
            trigger_interface_message_id="msg_search",
            user_name=TEST_USER_NAME,
        )

    assert error_search is None, f"Error during search chat interaction: {error_search}"
    assert final_reply_search is not None
    assert event1_summary in final_reply_search, (
        "Event 1 summary not in LLM's final reply."
    )
    assert event2_summary in final_reply_search, (
        "Event 2 summary not in LLM's final reply."
    )
    assert "9 AM" in final_reply_search or "09:00" in final_reply_search
    assert "1 PM" in final_reply_search or "13:00" in final_reply_search

    logger.info("Test Search Events PASSED.")


# TODO: Add tests for all-day events and basic recurring events.
# TODO: Add test for event created directly in CalDAV appears in application fetch.
