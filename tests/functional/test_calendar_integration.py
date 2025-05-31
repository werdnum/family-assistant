import asyncio
import json
import logging
import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
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

    # Rule for LLM to generate final response after successful tool call
    def final_response_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if (
            not messages or len(messages) < 2
        ):  # Need at least user, assistant (tool_call), tool_result
            return False
        # Second to last message should be assistant's tool call
        # Last message should be the tool's result
        last_message = messages[-1]  # This is the tool result
        # Penultimate message is the assistant's request for tool call, not needed for this matcher.
        # The actual last message *passed to the LLM for this turn* is the tool result.
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id
            and "OK. Event '" in last_message.get("content", "")
            and f"'{event_summary}' added" in last_message.get("content", "")
        )

    final_llm_response_content = (
        f"Alright, the event '{event_summary}' has been scheduled successfully."
    )
    final_response_llm_output = MockLLMOutput(
        content=final_llm_response_content, tool_calls=None
    )

    # This is the correct LLM client for this test
    llm_client_for_add_test: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (add_event_matcher, add_event_response),
            (final_response_matcher, final_response_llm_output),
        ]
    )

    # --- Setup ProcessingService ---
    test_calendar_config = {
        "caldav": {
            "base_url": radicale_base_url,  # Add base_url for DAVClient
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts = {
        "system_prompt": "System Time: {current_time}\nAggregated Context:\n{aggregated_other_context}"
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

    def get_test_db_context_factory() -> AbstractAsyncContextManager[DatabaseContext]:
        return get_db_context(engine=pg_vector_db_engine)

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_add_profile",  # Changed profile ID for clarity
        prompts=dummy_prompts,
        calendar_config=test_calendar_config,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=llm_client_for_add_test,  # Use the correctly defined LLM client
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction to Create Event ---
    user_message_create = f"Please schedule {event_summary} for tomorrow at 10 AM."
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply,
            _,
            _,
            error_create,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),  # Added missing new_task_event
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_create}],
            trigger_interface_message_id="msg_add_event_prompt_test",  # Unique message ID
            user_name=TEST_USER_NAME,
        )

    assert error_create is None, f"Error during event creation: {error_create}"
    assert final_reply and final_llm_response_content in final_reply, (
        f"Expected creation reply '{final_llm_response_content}', but got '{final_reply}'"
    )

    # --- Verify Event in Radicale ---
    radicale_event_check = await get_event_by_summary_from_radicale(
        radicale_server, event_summary
    )
    assert radicale_event_check is not None, (
        f"Event '{event_summary}' not found in Radicale {test_calendar_direct_url} after tool execution."
    )

    # --- Verify Event in System Prompt ---
    # Allow some time for Radicale to process and for our app to potentially cache/fetch
    await asyncio.sleep(0.5)  # Keep sleep if necessary for cache/propagation

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
    modified_end_dt = modified_start_dt + timedelta(
        hours=1
    )  # Ensure end time is also set for modification

    # --- LLM Rules for Initial Event Creation ---
    tool_call_id_add_original = f"call_add_orig_{uuid.uuid4()}"

    def add_original_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return f"schedule {original_summary.lower()}" in last_text

    add_original_event_response = MockLLMOutput(
        content=f"OK, I'll schedule '{original_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_original,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": original_summary,
                        "start_time": original_start_dt.isoformat(),
                        "end_time": original_end_dt.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_for_add_original(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id_add_original
            and "OK. Event '" in last_message.get("content", "")
            and f"'{original_summary}' added" in last_message.get("content", "")
        )

    final_llm_response_for_add_original_content = (
        f"Alright, '{original_summary}' is scheduled."
    )
    final_llm_response_for_add_original = MockLLMOutput(
        content=final_llm_response_for_add_original_content, tool_calls=None
    )

    # --- Setup ProcessingService for initial event creation ---
    # Note: This ProcessingService instance is configured for the *initial add*
    # It will be reconfigured later for the *modify* step.
    test_calendar_config_for_add = {  # Use a distinct config dict if needed, or reuse
        "caldav": {
            "base_url": radicale_base_url,
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts_for_add = {
        "system_prompt": "System Time: {current_time}\nAggregated Context:\n{aggregated_other_context}"
    }
    local_provider_for_add = LocalToolsProvider(
        definitions=local_tools_definition,
        implementations=local_tool_implementations,
        calendar_config=test_calendar_config_for_add,
    )
    mcp_provider_for_add = MCPToolsProvider(mcp_server_configs={})
    composite_provider_for_add = CompositeToolsProvider(
        providers=[local_provider_for_add, mcp_provider_for_add]
    )
    await composite_provider_for_add.get_tool_definitions()  # Ensure tools are loaded

    calendar_context_provider_for_add = CalendarContextProvider(
        calendar_config=test_calendar_config_for_add,
        prompts=dummy_prompts_for_add,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config_for_add = ProcessingServiceConfig(
        id="test_cal_initial_add_profile",  # Unique profile ID
        prompts=dummy_prompts_for_add,
        calendar_config=test_calendar_config_for_add,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service_for_add = ProcessingService(
        llm_client=RuleBasedMockLLMClient(
            rules=[
                (add_original_event_matcher, add_original_event_response),
                (
                    final_response_matcher_for_add_original,
                    final_llm_response_for_add_original,
                ),
            ]
        ),
        tools_provider=composite_provider_for_add,
        context_providers=[calendar_context_provider_for_add],
        service_config=service_config_for_add,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction to Create Initial Event ---
    user_message_create_original = (
        f"Please schedule {original_summary} for day after tomorrow at 2 PM."
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply_create,
            _,
            _,
            error_create,
        ) = await processing_service_for_add.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),  # Mock interface for this interaction
            new_task_event=asyncio.Event(),
            interface_type="test_initial_add",  # Distinguish interface type
            conversation_id=f"{TEST_CHAT_ID}_initial_add",  # Distinguish conversation
            trigger_content_parts=[
                {"type": "text", "text": user_message_create_original}
            ],
            trigger_interface_message_id="msg_create_orig_for_modify",
            user_name=TEST_USER_NAME,
        )

    assert error_create is None, (
        f"Error during LLM-based initial event creation: {error_create}"
    )
    assert (
        final_reply_create
        and final_llm_response_for_add_original_content in final_reply_create
    ), (
        f"Expected LLM creation reply '{final_llm_response_for_add_original_content}', but got '{final_reply_create}'"
    )

    # --- Retrieve UID of the event created by the LLM tool ---
    original_radicale_event = await get_event_by_summary_from_radicale(
        radicale_server, original_summary
    )
    assert original_radicale_event is not None, (
        f"Event '{original_summary}' not found in Radicale after LLM tool creation."
    )
    # Ensure event_uid is correctly typed as str for JSON serialization
    event_uid: str = str(original_radicale_event.vobject_instance.vevent.uid.value)  # type: ignore[attr-defined]
    logger.info(
        f"Retrieved UID for '{original_summary}' created by LLM tool: {event_uid}"
    )

    # --- LLM Rules for Modifying the Event (using the retrieved UID) ---
    tool_call_id_modify = f"call_mod_{uuid.uuid4()}"

    def modify_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            f"change event {original_summary.lower()}" in last_text
        )  # User refers to original summary

    modify_event_response = MockLLMOutput(
        content=f"OK, I'll modify '{original_summary}'.",  # LLM confirms based on original summary
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_modify,
                type="function",
                function=ToolCallFunction(
                    name="modify_calendar_event",
                    arguments=json.dumps({
                        "uid": event_uid,  # Use the UID from the LLM-created event
                        "calendar_url": test_calendar_direct_url,
                        "new_summary": modified_summary,
                        "new_start_time": modified_start_dt.isoformat(),
                        "new_end_time": modified_end_dt.isoformat(),  # Ensure end time is included
                    }),
                ),
            )
        ],
    )
    # This llm_client is for the MODIFICATION step

    # --- Define final response matcher and output for modify step ---
    def final_response_matcher_for_modify(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id_modify
            and "OK. Event '" in last_message.get("content", "")
            and f"'{modified_summary}' updated" in last_message.get("content", "")
        )

    final_llm_response_for_modify_content = (
        f"Alright, '{modified_summary}' has been updated."
    )
    final_llm_response_for_modify = MockLLMOutput(
        content=final_llm_response_for_modify_content, tool_calls=None
    )

    llm_client_for_modify: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (modify_event_matcher, modify_event_response),
            (final_response_matcher_for_modify, final_llm_response_for_modify),
        ]
    )

    # --- Setup ProcessingService for the modification step ---
    # Re-use or re-init ProcessingService, ensuring it uses llm_client_for_modify
    # For simplicity, we'll re-initialize parts of ProcessingService or update its LLM client.
    # The existing ProcessingService setup from the original test structure follows this point.
    # The key is that `llm_client` below will be `llm_client_for_modify`.
    # The `composite_provider` and `calendar_context_provider` can be reused if their config is the same.
    # The `service_config` might need a different ID if that's important.
    # The following lines are from the original test structure, now using the `llm_client_for_modify`.
    llm_client: LLMInterface = (
        llm_client_for_modify  # Ensure this is the one used by ProcessingService below
    )

    # --- Setup ProcessingService (similar to add_event test) ---
    test_calendar_config = {
        "caldav": {
            "base_url": radicale_base_url,  # Add base_url
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
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
        and final_llm_response_for_modify_content in final_reply_modify
    ), (
        f"Expected modify reply '{final_llm_response_for_modify_content}', but got '{final_reply_modify}'"
    )

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
    1. Create an event using the LLM `add_calendar_event` tool.
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

    # --- LLM Rules for Initial Event Creation ---
    tool_call_id_add_to_delete = f"call_add_del_{uuid.uuid4()}"

    def add_event_to_delete_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return f"schedule {event_to_delete_summary.lower()}" in last_text

    add_event_to_delete_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event_to_delete_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_to_delete,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event_to_delete_summary,
                        "start_time": event_start_dt.isoformat(),
                        "end_time": event_end_dt.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_for_add_to_delete(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id_add_to_delete
            and "OK. Event '" in last_message.get("content", "")
            and f"'{event_to_delete_summary}' added" in last_message.get("content", "")
        )

    final_llm_response_for_add_to_delete_content = (
        f"Alright, '{event_to_delete_summary}' is scheduled."
    )
    final_llm_response_for_add_to_delete = MockLLMOutput(
        content=final_llm_response_for_add_to_delete_content, tool_calls=None
    )

    # --- Setup ProcessingService (similar to other tests) ---
    test_calendar_config = {
        "caldav": {
            "base_url": radicale_base_url,  # Add base_url
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts = {
        "system_prompt": "System Time: {current_time}\nAggregated Context:\n{aggregated_other_context}"
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
        llm_client=MagicMock(),  # Will be replaced
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config={},
    )

    # --- Simulate User Interaction to Create Event to Delete ---
    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event_to_delete_matcher, add_event_to_delete_response),
            (
                final_response_matcher_for_add_to_delete,
                final_llm_response_for_add_to_delete,
            ),
        ]
    )
    user_message_create_to_delete = (
        f"Please schedule {event_to_delete_summary} for 3 days from now at 11 AM."
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        (
            final_reply_create_del,
            _,
            _,
            error_create_del,
        ) = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            new_task_event=asyncio.Event(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[
                {"type": "text", "text": user_message_create_to_delete}
            ],
            trigger_interface_message_id="msg_create_del",
            user_name=TEST_USER_NAME,
        )

    assert error_create_del is None, (
        f"Error during creation of event to delete: {error_create_del}"
    )
    assert (
        final_reply_create_del
        and final_llm_response_for_add_to_delete_content in final_reply_create_del
    ), (
        f"Expected creation reply '{final_llm_response_for_add_to_delete_content}', but got '{final_reply_create_del}'"
    )

    # --- Retrieve UID of the created event ---
    radicale_event_to_delete = await get_event_by_summary_from_radicale(
        radicale_server, event_to_delete_summary
    )
    assert radicale_event_to_delete is not None, (
        f"Event '{event_to_delete_summary}' not found in Radicale after tool creation."
    )
    event_uid_del = radicale_event_to_delete.vobject_instance.vevent.uid.value  # type: ignore[attr-defined]
    logger.info(f"Retrieved UID for '{event_to_delete_summary}': {event_uid_del}")

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
                        "uid": event_uid_del,  # Use retrieved UID
                        "calendar_url": test_calendar_direct_url,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_for_delete(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.get("role") == "tool"
            and last_message.get("tool_call_id") == tool_call_id_delete
            and "OK. Event '" in last_message.get("content", "")
            and f"'{event_to_delete_summary}' deleted"
            in last_message.get("content", "")
        )

    final_llm_response_for_delete_content = (
        f"Alright, '{event_to_delete_summary}' has been deleted."
    )
    final_llm_response_for_delete = MockLLMOutput(
        content=final_llm_response_for_delete_content, tool_calls=None
    )

    # Update LLM client for the deletion part
    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (delete_event_matcher, delete_event_response),
            (final_response_matcher_for_delete, final_llm_response_for_delete),
        ]
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
        and final_llm_response_for_delete_content in final_reply_delete
    ), (
        f"Expected delete reply '{final_llm_response_for_delete_content}', but got '{final_reply_delete}'"
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
        # Use the new_event_object() pattern for creating events
        def create_event_sync() -> None:
            event = target_calendar.new_event_object()  # type: ignore[attr-defined]
            # new_event_object creates a shell with PRODID "-//python-caldav//caldav//en_DK"
            # We need to populate its vevent component.
            vevent = event.vobject_instance.vevent  # type: ignore[attr-defined]
            vevent.uid.value = str(uuid.uuid4())  # type: ignore[attr-defined]
            vevent.summary.value = summ  # type: ignore[attr-defined]
            vevent.dtstart.value = st  # type: ignore[attr-defined]
            vevent.dtend.value = en  # type: ignore[attr-defined]
            vevent.dtstamp.value = datetime.now(ZoneInfo("UTC"))  # type: ignore[attr-defined]
            # event.data will be updated by the setter of vobject_instance implicitly if not already.
            # Or more explicitly:
            event.data = event.vobject_instance.serialize() # type: ignore[attr-defined]
            event.save() # type: ignore[attr-defined]

        await asyncio.to_thread(create_event_sync)

    logger.info(
        f"Directly created '{event1_summary}' and '{event2_summary}' for search test using new_event_object pattern."
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
            "base_url": radicale_base_url,  # Add base_url
            "username": r_user,
            "password": r_pass,
            "calendar_urls": [test_calendar_direct_url],
        },
        "ical": {"urls": []},
    }
    dummy_prompts: dict[str, Any] = {
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
