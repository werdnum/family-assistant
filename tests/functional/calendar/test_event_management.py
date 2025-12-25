import asyncio
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import caldav
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.calendar_integration import (
    fetch_upcoming_events,
    format_datetime_or_date,  # Added import
)
from family_assistant.config_models import AppConfig
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
    add_calendar_event_tool,
    search_calendar_events_tool,
)
from family_assistant.tools.types import CalendarConfig, ToolExecutionContext
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
        # Try to search for the event
        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=calendar_config,
            search_text=event_summary,
        )

        # Check if the exact event title appears in results
        if event_summary in search_result:
            return True

        # Sleep briefly before retrying
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Polling for calendar event to appear in search
        await asyncio.sleep(0.1)

    return False


@pytest.mark.asyncio
@pytest.mark.postgres
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
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id_add_original
            and "OK. Event '" in (last_message.content or "")
            and f"'{original_summary}' added" in (last_message.content or "")
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
    test_calendar_config_for_add = cast(
        "CalendarConfig",
        {  # Use a distinct config dict if needed, or reuse
            "caldav": {
                "base_url": radicale_base_url,
                "username": r_user,
                "password": r_pass,
                "calendar_urls": [test_calendar_direct_url],
            },
            "ical": {"urls": []},
        },
    )
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
        app_config=AppConfig(),
    )

    # --- Simulate User Interaction to Create Initial Event ---
    user_message_create_original = (
        f"Please schedule {original_summary} for day after tomorrow at 2 PM."
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service_for_add.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),  # Mock interface for this interaction
            interface_type="test_initial_add",  # Distinguish interface type
            conversation_id=f"{TEST_CHAT_ID}_initial_add",  # Distinguish conversation
            trigger_content_parts=[
                {"type": "text", "text": user_message_create_original}
            ],
            trigger_interface_message_id="msg_create_orig_for_modify",
            user_name=TEST_USER_NAME,
        )
        final_reply_create = result.text_reply
        error_create = result.error_traceback

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
    # Add a small delay to ensure the event is fully saved
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for calendar event to be saved
    await asyncio.sleep(1.0)

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

    # Verify the event can be found by UID before proceeding
    logger.info(
        f"Verifying event exists with UID {event_uid} at calendar URL {test_calendar_direct_url}"
    )

    # Create a database context for the search operation
    async with DatabaseContext(engine=pg_vector_db_engine) as db_ctx:
        search_result = await search_calendar_events_tool(
            exec_context=ToolExecutionContext(
                interface_type="test",
                conversation_id="test-verify",
                user_name="TestUser",
                turn_id="test-turn-verify",
                db_context=db_ctx,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,
                chat_interface=None,
                timezone_str=TEST_TIMEZONE_STR,
                request_confirmation_callback=None,
            ),
            calendar_config=test_calendar_config_for_add,
            search_text=original_summary,
        )
        logger.info(f"Search result for '{original_summary}': {search_result}")

    # Extract the calendar URL from the search result to ensure we use the correct one

    calendar_url_match = re.search(r"Calendar: (.+)", search_result)
    if calendar_url_match:
        actual_calendar_url = calendar_url_match.group(1).strip()
        logger.info(f"Extracted calendar URL from search: {actual_calendar_url}")
    else:
        actual_calendar_url = test_calendar_direct_url
        logger.warning(
            f"Could not extract calendar URL from search result, using: {actual_calendar_url}"
        )

    # --- LLM Rules for Modifying the Event (using the retrieved UID) ---
    tool_call_id_modify = f"call_mod_{uuid.uuid4()}"

    def modify_event_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            f"change event {original_summary.lower()}" in last_text
        )  # User refers to original summary

    # Now that we have event_uid and actual_calendar_url, create the modify response
    def create_modify_response() -> MockLLMOutput:
        return MockLLMOutput(
            content=f"OK, I'll modify '{original_summary}'.",  # LLM confirms based on original summary
            tool_calls=[
                ToolCallItem(
                    id=tool_call_id_modify,
                    type="function",
                    function=ToolCallFunction(
                        name="modify_calendar_event",
                        arguments=json.dumps({
                            "uid": event_uid,  # Use the UID from the LLM-created event
                            "calendar_url": actual_calendar_url,  # Use the actual calendar URL from search
                            "new_summary": modified_summary,
                            "new_start_time": modified_start_dt.isoformat(),
                            "new_end_time": modified_end_dt.isoformat(),  # Ensure end time is included
                        }),
                    ),
                )
            ],
        )

    # Create the actual response object now that we have all the values
    modify_event_response = create_modify_response()

    # This llm_client is for the MODIFICATION step

    # --- Define final response matcher and output for modify step ---
    def final_response_matcher_for_modify(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id_modify
            and "OK. Event '" in (last_message.content or "")
            and "updated" in (last_message.content or "")
        )

    final_llm_response_for_modify_content = (
        f"Alright, '{modified_summary}' has been updated."
    )
    final_llm_response_for_modify = MockLLMOutput(
        content=final_llm_response_for_modify_content, tool_calls=None
    )

    # Add a matcher for error responses
    def error_response_matcher_for_modify(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if not messages or len(messages) < 2:
            return False
        last_message = messages[-1]
        return (
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id_modify
            and "Error:" in (last_message.content or "")
            and "not found" in (last_message.content or "")
        )

    error_llm_response_for_modify = MockLLMOutput(
        content=f"I'm sorry, but I couldn't find the event '{original_summary}' to modify. It may have been deleted or the event details may be incorrect.",
        tool_calls=None,
    )

    llm_client_for_modify: LLMInterface = RuleBasedMockLLMClient(
        rules=[
            (modify_event_matcher, modify_event_response),
            (final_response_matcher_for_modify, final_llm_response_for_modify),
            (error_response_matcher_for_modify, error_llm_response_for_modify),
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
    # Use the same config that was used for creating the event
    test_calendar_config = test_calendar_config_for_add
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
        id="test_cal_mod_profile",
        prompts=dummy_prompts,
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
        app_config=AppConfig(),
    )

    # --- Simulate User Interaction to Modify ---
    user_message_modify = f"Please change event {original_summary} to start at 3 PM and call it {modified_summary}."
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_modify}],
            trigger_interface_message_id="msg_mod",
            user_name=TEST_USER_NAME,
        )
        final_reply_modify = result.text_reply
        error_modify = result.error_traceback

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
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for calendar sync to context providers
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
@pytest.mark.postgres
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
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id_add_to_delete
            and "OK. Event '" in (last_message.content or "")
            and f"'{event_to_delete_summary}' added" in (last_message.content or "")
        )

    final_llm_response_for_add_to_delete_content = (
        f"Alright, '{event_to_delete_summary}' is scheduled."
    )
    final_llm_response_for_add_to_delete = MockLLMOutput(
        content=final_llm_response_for_add_to_delete_content, tool_calls=None
    )

    # --- Setup ProcessingService (similar to other tests) ---
    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": radicale_base_url,  # Add base_url
                "username": r_user,
                "password": r_pass,
                "calendar_urls": [test_calendar_direct_url],
            },
            "ical": {"urls": []},
        },
    )
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

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_del_profile",
        prompts=dummy_prompts,  # type: ignore
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
        app_config=AppConfig(),
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
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[
                {"type": "text", "text": user_message_create_to_delete}
            ],
            trigger_interface_message_id="msg_create_del",
            user_name=TEST_USER_NAME,
        )
        final_reply_create_del = result.text_reply
        error_create_del = result.error_traceback

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
            last_message.role == "tool"
            and last_message.tool_call_id == tool_call_id_delete
            and "OK. Event '" in (last_message.content or "")
            and f"'{event_to_delete_summary}' deleted" in (last_message.content or "")
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
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_delete}],
            trigger_interface_message_id="msg_del",
            user_name=TEST_USER_NAME,
        )
        final_reply_delete = result.text_reply
        error_delete = result.error_traceback

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
    # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for calendar sync to context providers
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
@pytest.mark.postgres
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

    # --- Setup ProcessingService (used for creating events and then searching) ---
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
            "duplicate_detection": {
                "enabled": False,  # Disable for this test - we're testing search, not duplicate detection
            },
        },
    )
    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_search_profile_main",  # Main profile for the test
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
        max_history_messages=5,
        history_max_age_hours=24,
        tools_config={"confirmation_required": []},
        delegation_security_level="unrestricted",
    )
    processing_service = ProcessingService(
        llm_client=MagicMock(),  # Will be replaced for each phase
        tools_provider=composite_provider,
        context_providers=[calendar_context_provider],
        service_config=service_config,
        server_url=None,
        app_config=AppConfig(),
    )

    # --- Phase 1: Create Event 1 using LLM Tool ---
    tool_call_id_add_event1 = f"call_add_event1_{uuid.uuid4()}"

    def add_event1_matcher(kwargs: MatcherArgs) -> bool:
        return (
            f"schedule {event1_summary.lower()}"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    add_event1_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event1_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_event1,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event1_summary,
                        "start_time": event1_start.isoformat(),
                        "end_time": event1_end.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_event1(kwargs: MatcherArgs) -> bool:
        last_msg = kwargs.get("messages", [])[-1]
        return (
            last_msg.role == "tool"
            and last_msg.tool_call_id == tool_call_id_add_event1
            and "OK. Event '" in (last_msg.content or "")
        )

    final_response_event1 = MockLLMOutput(
        content=f"Event '{event1_summary}' scheduled.", tool_calls=None
    )

    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event1_matcher, add_event1_response),
            (final_response_matcher_event1, final_response_event1),
        ]
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test_search",
            conversation_id=f"{TEST_CHAT_ID}_add1",
            trigger_content_parts=[
                {"type": "text", "text": f"schedule {event1_summary.lower()}"}
            ],
            trigger_interface_message_id="msg_add_event1_for_search",
            user_name=TEST_USER_NAME,
        )
        err_add1 = result.error_traceback
    assert err_add1 is None, f"Error creating event1 for search test: {err_add1}"
    assert (
        await get_event_by_summary_from_radicale(radicale_server, event1_summary)
        is not None
    ), f"Event '{event1_summary}' not found in Radicale after creation for search test."
    logger.info(f"Created '{event1_summary}' via LLM tool for search test.")

    # --- Phase 2: Create Event 2 using LLM Tool ---
    tool_call_id_add_event2 = f"call_add_event2_{uuid.uuid4()}"

    def add_event2_matcher(kwargs: MatcherArgs) -> bool:
        return (
            f"schedule {event2_summary.lower()}"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    add_event2_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event2_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_event2,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event2_summary,
                        "start_time": event2_start.isoformat(),
                        "end_time": event2_end.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_event2(kwargs: MatcherArgs) -> bool:
        last_msg = kwargs.get("messages", [])[-1]
        return (
            last_msg.role == "tool"
            and last_msg.tool_call_id == tool_call_id_add_event2
            and "OK. Event '" in (last_msg.content or "")
        )

    final_response_event2 = MockLLMOutput(
        content=f"Event '{event2_summary}' scheduled.", tool_calls=None
    )

    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event2_matcher, add_event2_response),
            (final_response_matcher_event2, final_response_event2),
        ]
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test_search",
            conversation_id=f"{TEST_CHAT_ID}_add2",
            trigger_content_parts=[
                {"type": "text", "text": f"schedule {event2_summary.lower()}"}
            ],
            trigger_interface_message_id="msg_add_event2_for_search",
            user_name=TEST_USER_NAME,
        )
        err_add2 = result.error_traceback
    assert err_add2 is None, f"Error creating event2 for search test: {err_add2}"
    assert (
        await get_event_by_summary_from_radicale(radicale_server, event2_summary)
        is not None
    ), f"Event '{event2_summary}' not found in Radicale after creation for search test."
    logger.info(f"Created '{event2_summary}' via LLM tool for search test.")

    # --- Phase 3: LLM Rules for Searching Events ---
    tool_call_id_search = f"call_search_{uuid.uuid4()}"
    # Use a query text that will actually match the event summaries
    search_query_text = "Search Event"

    # Rule 1: LLM decides to search
    def search_intent_matcher(kwargs: MatcherArgs) -> bool:
        last_text = get_last_message_text(kwargs.get("messages", [])).lower()
        return (
            "what are my events" in last_text
            and "day after tomorrow plus two" in last_text
        )

    search_intent_response = MockLLMOutput(
        content="Let me check your calendar...",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_search,
                type="function",
                function=ToolCallFunction(
                    name="search_calendar_events",
                    arguments=json.dumps({
                        "search_text": search_query_text,  # Tool will search for "Search Event"
                        "start_date": (search_day - timedelta(days=1)).strftime(
                            "%Y-%m-%d"
                        ),  # Start search one day earlier
                        "end_date": (search_day + timedelta(days=2)).strftime(
                            "%Y-%m-%d"
                        ),  # End search two days after search_day (total 4-day window)
                    }),
                ),
            )
        ],
    )

    # Rule 2: LLM processes search results and replies to user
    # This rule's matcher needs to look for the tool's output in the message history
    def present_search_results_matcher(kwargs: MatcherArgs) -> bool:
        messages = kwargs.get("messages", [])
        if len(messages) < 4:  # Need at least system, user, assistant, tool
            logger.debug(
                f"present_search_results_matcher: Not enough messages ({len(messages)})"
            )
            return False

        # The last message in the input to the LLM for this turn should be the tool's result.
        tool_result_message = messages[-1]
        # The message before that should be the assistant's call to the tool.
        assistant_tool_call_message = messages[-2]

        # Log messages for debugging
        logger.debug(
            f"present_search_results_matcher: Assistant tool call message: {assistant_tool_call_message}"
        )
        logger.debug(
            f"present_search_results_matcher: Tool result message: {tool_result_message}"
        )

        # Verify the assistant called the correct tool
        if not (
            assistant_tool_call_message.role == "assistant"
            and assistant_tool_call_message.tool_calls
            and len(assistant_tool_call_message.tool_calls) == 1
            and assistant_tool_call_message.tool_calls[0].id == tool_call_id_search
            and assistant_tool_call_message.tool_calls[0].function.name
            == "search_calendar_events"
        ):
            logger.debug(
                "present_search_results_matcher: Assistant tool call verification failed."
            )
            return False

        # Verify the tool result message contains both event summaries
        content = tool_result_message.content or ""
        return (
            tool_result_message.role == "tool"
            and tool_result_message.tool_call_id == tool_call_id_search
            and event1_summary.lower() in content.lower()
            and event2_summary.lower() in content.lower()
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
    # Assign the new LLM client with search rules to the existing processing_service
    processing_service.llm_client = llm_client

    # --- Simulate User Interaction ---
    # User asks a general question, LLM will refine it to search_query_text ("Search Event")
    user_message_search = "What are my events for day after tomorrow plus two?"
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test",
            conversation_id=TEST_CHAT_ID,
            trigger_content_parts=[{"type": "text", "text": user_message_search}],
            trigger_interface_message_id="msg_search",
            user_name=TEST_USER_NAME,
        )
        final_reply_search = result.text_reply
        error_search = result.error_traceback

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


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_mixed_date_datetime_sorting(
    radicale_server: tuple[str, str, str, str],
    pg_vector_db_engine: AsyncEngine,
) -> None:
    """Test that calendar fetching correctly sorts mixed date and datetime events."""
    _, r_user, r_pass, test_calendar_direct_url = radicale_server

    # Create test events with mixed types directly in CalDAV
    client = get_radicale_client(radicale_server)
    calendar = await asyncio.to_thread(client.calendar, url=test_calendar_direct_url)

    # Create an all-day event (uses date type)
    all_day_event_uid = str(uuid.uuid4())
    all_day_start = datetime.now(ZoneInfo(TEST_TIMEZONE_STR)).date() + timedelta(days=2)
    all_day_event_data = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:{all_day_event_uid}
SUMMARY:All Day Event Test
DTSTART;VALUE=DATE:{all_day_start.strftime("%Y%m%d")}
DTEND;VALUE=DATE:{(all_day_start + timedelta(days=1)).strftime("%Y%m%d")}
DTSTAMP:{datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")}
END:VEVENT
END:VCALENDAR"""

    # Create a timed event (uses datetime type)
    # Set it to tomorrow at 10 AM to ensure it's on a different day than the all-day event
    timed_event_uid = str(uuid.uuid4())
    tomorrow = datetime.now(ZoneInfo(TEST_TIMEZONE_STR)).date() + timedelta(days=1)
    timed_start = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=10, minute=0, second=0, microsecond=0, tzinfo=ZoneInfo(TEST_TIMEZONE_STR)
    )
    timed_end = timed_start + timedelta(hours=2)
    timed_event_data = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:{timed_event_uid}
SUMMARY:Timed Event Test
DTSTART:{timed_start.strftime("%Y%m%dT%H%M%S")}
DTEND:{timed_end.strftime("%Y%m%dT%H%M%S")}
DTSTAMP:{datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")}
END:VEVENT
END:VCALENDAR"""

    # Save both events
    await asyncio.to_thread(calendar.save_event, all_day_event_data)
    await asyncio.to_thread(calendar.save_event, timed_event_data)

    # Setup calendar configuration
    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": radicale_server[0],
                "username": r_user,
                "password": r_pass,
                "calendar_urls": [test_calendar_direct_url],
            },
            "ical": {"urls": []},
        },
    )

    # Import and test the fetch function

    # Fetch events - this should not throw an error even with mixed date/datetime types
    events = await fetch_upcoming_events(test_calendar_config, TEST_TIMEZONE_STR)

    # Verify events were fetched and sorted correctly
    assert len(events) >= 2, "Should have fetched at least 2 events"

    # Find our test events
    found_timed = False
    found_all_day = False
    for event in events:
        if event["summary"] == "Timed Event Test":
            found_timed = True
            assert isinstance(event["start"], datetime), (
                "Timed event should have datetime start"
            )
        elif event["summary"] == "All Day Event Test":
            found_all_day = True
            assert isinstance(event["start"], date) and not isinstance(
                event["start"], datetime
            ), "All-day event should have date start"

    assert found_timed, "Timed event not found in fetched events"
    assert found_all_day, "All-day event not found in fetched events"

    # Verify events are sorted (timed event comes before all-day event based on our dates)
    event_summaries = [
        e["summary"]
        for e in events
        if e["summary"] in {"Timed Event Test", "All Day Event Test"}
    ]
    timed_idx = event_summaries.index("Timed Event Test")
    all_day_idx = event_summaries.index("All Day Event Test")
    assert timed_idx < all_day_idx, (
        "Events should be sorted with timed event before all-day event"
    )

    logger.info("Test mixed date/datetime sorting PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_similarity_based_search_finds_similar_events(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that similarity-based search finds similar events.

    Creates events with similar titles, then verifies search finds them
    using fuzzy similarity (tests use fuzzy for speed/zero dependencies,
    but the same infrastructure works with embedding similarity in production).
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Similarity-Based Search - Semantic Matching (Radicale URL: {test_calendar_direct_url}) ---"
    )

    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    tomorrow = datetime.now(local_tz) + timedelta(days=1)

    # Create semantically similar events
    # Use simple names without UUIDs to avoid breaking similarity scores
    event1_summary = "Doctor appointment"
    event1_start = tomorrow.replace(hour=14, minute=0, second=0, microsecond=0)
    event1_end = event1_start + timedelta(hours=1)

    event2_summary = "Dr. Smith checkup"
    event2_start = tomorrow.replace(hour=15, minute=0, second=0, microsecond=0)
    event2_end = event2_start + timedelta(hours=1)

    # Create a dissimilar event (should not appear in results)
    event3_summary = "Soccer practice"
    event3_start = tomorrow.replace(hour=16, minute=0, second=0, microsecond=0)
    event3_end = event3_start + timedelta(hours=1)

    # Setup calendar config with embedding similarity
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
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",  # Use fuzzy for tests (fast, no deps)
                "similarity_threshold": 0.30,
                "time_window_hours": 2,
            },
        },
    )

    # ast-grep-ignore: no-dict-any - Legacy code - needs structured types
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

    calendar_context_provider = CalendarContextProvider(
        calendar_config=test_calendar_config,
        prompts=dummy_prompts,
        timezone_str=TEST_TIMEZONE_STR,
    )
    service_config = ProcessingServiceConfig(
        id="test_cal_similarity_profile",
        prompts=dummy_prompts,
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
        app_config=AppConfig(),
    )

    # Create event 1
    tool_call_id_add_event1 = f"call_add_event1_{uuid.uuid4()}"

    def add_event1_matcher(kwargs: MatcherArgs) -> bool:
        return (
            f"schedule {event1_summary.lower()}"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    add_event1_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event1_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_event1,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event1_summary,
                        "start_time": event1_start.isoformat(),
                        "end_time": event1_end.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_event1(kwargs: MatcherArgs) -> bool:
        last_msg = kwargs.get("messages", [])[-1]
        return (
            last_msg.role == "tool"
            and last_msg.tool_call_id == tool_call_id_add_event1
            and "OK. Event '" in (last_msg.content or "")
        )

    final_response_event1 = MockLLMOutput(
        content=f"Event '{event1_summary}' scheduled.", tool_calls=None
    )

    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event1_matcher, add_event1_response),
            (final_response_matcher_event1, final_response_event1),
        ]
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test_similarity",
            conversation_id=f"{TEST_CHAT_ID}_similarity_add1",
            trigger_content_parts=[
                {"type": "text", "text": f"schedule {event1_summary}"}
            ],
            trigger_interface_message_id="msg_add_event1_similarity",
            user_name=TEST_USER_NAME,
        )
        err_add1 = result.error_traceback
    assert err_add1 is None, f"Error creating event1: {err_add1}"

    # Create event 2
    tool_call_id_add_event2 = f"call_add_event2_{uuid.uuid4()}"

    def add_event2_matcher(kwargs: MatcherArgs) -> bool:
        return (
            f"schedule {event2_summary.lower()}"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    add_event2_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event2_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_event2,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event2_summary,
                        "start_time": event2_start.isoformat(),
                        "end_time": event2_end.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_event2(kwargs: MatcherArgs) -> bool:
        last_msg = kwargs.get("messages", [])[-1]
        return (
            last_msg.role == "tool"
            and last_msg.tool_call_id == tool_call_id_add_event2
            and "OK. Event '" in (last_msg.content or "")
        )

    final_response_event2 = MockLLMOutput(
        content=f"Event '{event2_summary}' scheduled.", tool_calls=None
    )

    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event2_matcher, add_event2_response),
            (final_response_matcher_event2, final_response_event2),
        ]
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test_similarity",
            conversation_id=f"{TEST_CHAT_ID}_similarity_add2",
            trigger_content_parts=[
                {"type": "text", "text": f"schedule {event2_summary}"}
            ],
            trigger_interface_message_id="msg_add_event2_similarity",
            user_name=TEST_USER_NAME,
        )
        err_add2 = result.error_traceback
    assert err_add2 is None, f"Error creating event2: {err_add2}"

    # Create event 3 (dissimilar)
    tool_call_id_add_event3 = f"call_add_event3_{uuid.uuid4()}"

    def add_event3_matcher(kwargs: MatcherArgs) -> bool:
        return (
            f"schedule {event3_summary.lower()}"
            in get_last_message_text(kwargs.get("messages", [])).lower()
        )

    add_event3_response = MockLLMOutput(
        content=f"OK, I'll schedule '{event3_summary}'.",
        tool_calls=[
            ToolCallItem(
                id=tool_call_id_add_event3,
                type="function",
                function=ToolCallFunction(
                    name="add_calendar_event",
                    arguments=json.dumps({
                        "summary": event3_summary,
                        "start_time": event3_start.isoformat(),
                        "end_time": event3_end.isoformat(),
                        "all_day": False,
                    }),
                ),
            )
        ],
    )

    def final_response_matcher_event3(kwargs: MatcherArgs) -> bool:
        last_msg = kwargs.get("messages", [])[-1]
        return (
            last_msg.role == "tool"
            and last_msg.tool_call_id == tool_call_id_add_event3
            and "OK. Event '" in (last_msg.content or "")
        )

    final_response_event3 = MockLLMOutput(
        content=f"Event '{event3_summary}' scheduled.", tool_calls=None
    )

    processing_service.llm_client = RuleBasedMockLLMClient(
        rules=[
            (add_event3_matcher, add_event3_response),
            (final_response_matcher_event3, final_response_event3),
        ]
    )
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        result = await processing_service.handle_chat_interaction(
            db_context=db_context,
            chat_interface=MagicMock(),
            interface_type="test_similarity",
            conversation_id=f"{TEST_CHAT_ID}_similarity_add3",
            trigger_content_parts=[
                {"type": "text", "text": f"schedule {event3_summary}"}
            ],
            trigger_interface_message_id="msg_add_event3_similarity",
            user_name=TEST_USER_NAME,
        )
        err_add3 = result.error_traceback
    assert err_add3 is None, f"Error creating event3: {err_add3}"

    # Now search for "doctor" directly using the tool
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-similarity-search",
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            chat_interface=None,
            timezone_str=TEST_TIMEZONE_STR,
            request_confirmation_callback=None,
        )

        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            search_text="doctor",
        )

        logger.info(f"Search tool result:\n{search_result}")

        # Verify semantic matching: "doctor" should find both events
        # Event 1: "Doctor appointment" - high similarity
        # Event 2: "Dr. Smith checkup" - moderate similarity (has "Dr.")
        # Event 3: "Soccer practice" - low similarity (should be excluded)

        # Check that similarity scores are present
        assert "similarity:" in search_result.lower(), (
            "Search results should include similarity scores"
        )

        # Both doctor-related events should be in results
        # Note: With fuzzy matching, "doctor" vs "Dr. Smith checkup" has low similarity
        # But "doctor" vs "Doctor appointment" has very high similarity
        # So we should at least find event1
        assert event1_summary in search_result, (
            f"Event 1 '{event1_summary}' should be found by 'doctor' search"
        )

        # Event 3 (soccer) should NOT be in results
        assert event3_summary not in search_result, (
            f"Event 3 '{event3_summary}' should NOT be found by 'doctor' search"
        )

    logger.info("Test similarity-based search semantic matching PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_similarity_search_threshold_filtering(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that similarity threshold correctly filters out low-similarity events.

    Creates events with varying similarity to search term, verifies that
    only events above threshold are returned.
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Similarity Search Threshold Filtering (Radicale URL: {test_calendar_direct_url}) ---"
    )

    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    tomorrow = datetime.now(local_tz) + timedelta(days=1)

    # Create events with different similarity to "meeting"
    # Use simple names without UUIDs to avoid breaking similarity scores
    high_similarity_event = "Weekly team meeting"
    medium_similarity_event = "Team standup"
    low_similarity_event = "Grocery shopping"

    start_time = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    end_time = start_time + timedelta(hours=1)

    # Setup with higher threshold
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
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.50,  # Higher threshold
                "time_window_hours": 2,
            },
        },
    )

    # Create events directly using tool
    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-threshold",
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            chat_interface=None,
            timezone_str=TEST_TIMEZONE_STR,
            request_confirmation_callback=None,
        )

        # Create high similarity event
        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=high_similarity_event,
            start_time=start_time.isoformat(),
            end_time=end_time.isoformat(),
            all_day=False,
        )

        # Create medium similarity event
        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=medium_similarity_event,
            start_time=(start_time + timedelta(hours=1)).isoformat(),
            end_time=(end_time + timedelta(hours=1)).isoformat(),
            all_day=False,
        )

        # Create low similarity event
        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=low_similarity_event,
            start_time=(start_time + timedelta(hours=2)).isoformat(),
            end_time=(end_time + timedelta(hours=2)).isoformat(),
            all_day=False,
        )

        # Search for "meeting" with threshold 0.50
        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            search_text="meeting",
        )

        logger.info(f"Search result with threshold 0.50:\n{search_result}")

        # High similarity event should be found
        assert high_similarity_event in search_result, (
            f"High similarity event '{high_similarity_event}' should be in results"
        )

        # Low similarity event should NOT be found (below threshold)
        assert low_similarity_event not in search_result, (
            f"Low similarity event '{low_similarity_event}' should NOT be in results"
        )

    logger.info("Test similarity search threshold filtering PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_similarity_search_score_sorting(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that search results are sorted by similarity score (highest first).
    """
    radicale_base_url, r_user, r_pass, test_calendar_direct_url = radicale_server
    logger.info(
        f"\n--- Test: Similarity Search Score Sorting (Radicale URL: {test_calendar_direct_url}) ---"
    )

    local_tz = ZoneInfo(TEST_TIMEZONE_STR)
    tomorrow = datetime.now(local_tz) + timedelta(days=1)

    # Create events with varying similarity to "appointment"
    # Use simple names without UUIDs to avoid breaking similarity scores
    exact_match = "Appointment"
    close_match = "Doctor appointment"
    partial_match = "Medical checkup"

    start_time = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

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
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.20,  # Low threshold to get all events
                "time_window_hours": 2,
            },
        },
    )

    async with DatabaseContext(engine=pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test-sorting",
            user_name="TestUser",
            turn_id="test-turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            chat_interface=None,
            timezone_str=TEST_TIMEZONE_STR,
            request_confirmation_callback=None,
        )

        # Create events (in random order)
        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=partial_match,
            start_time=(start_time + timedelta(hours=2)).isoformat(),
            end_time=(start_time + timedelta(hours=3)).isoformat(),
            all_day=False,
        )

        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=exact_match,
            start_time=start_time.isoformat(),
            end_time=(start_time + timedelta(hours=1)).isoformat(),
            all_day=False,
        )

        await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary=close_match,
            start_time=(start_time + timedelta(hours=1)).isoformat(),
            end_time=(start_time + timedelta(hours=2)).isoformat(),
            all_day=False,
            bypass_duplicate_check=True,  # Bypass since we want multiple similar events for sorting test
        )

        # Search for "appointment"
        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            search_text="appointment",
        )

        logger.info(f"Search result:\n{search_result}")

        # Parse the result to verify sorting
        # Results should be sorted by similarity (highest first)
        # exact_match should come before close_match
        exact_match_pos = search_result.find(exact_match)
        close_match_pos = search_result.find(close_match)

        assert exact_match_pos != -1, "Exact match should be in results"
        assert close_match_pos != -1, "Close match should be in results"
        assert exact_match_pos < close_match_pos, (
            "Results should be sorted by similarity: exact match before close match"
        )

        # Verify similarity scores are included
        assert "similarity:" in search_result.lower(), (
            "Results should include similarity scores"
        )

    logger.info("Test similarity search score sorting PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_duplicate_detection_error_shown(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that duplicate detection blocks creation of similar events.

    Verifies that BEFORE creating an event, if similar events exist at nearby times,
    an error is returned with bypass instructions. Also tests that bypass flag allows
    creation.
    """
    logger.info("Starting test_duplicate_detection_error_shown...")

    base_url, username, password, calendar_url = radicale_server

    # Create test config with fuzzy similarity (fast, zero dependencies)
    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": base_url,
                "username": username,
                "password": password,
                "calendar_urls": [calendar_url],
                # RADICALE WORKAROUND: Use naive datetimes for search compatibility
                "_use_naive_datetimes_for_search": True,
            },
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.30,
                "time_window_hours": 2,
            },
        },
    )

    # Create execution context
    async with DatabaseContext(pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conv",
            user_name="testuser",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            timezone_str="America/New_York",
        )

        # Create first event
        tomorrow = date.today() + timedelta(days=1)
        event1_start = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, 0
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        event1_end = event1_start + timedelta(hours=1)

        event1_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Doctor appointment",
            start_time=event1_start.isoformat(),
            end_time=event1_end.isoformat(),
        )

        assert "OK. Event 'Doctor appointment' added" in event1_result
        assert "Error:" not in event1_result, (
            "First event should not trigger duplicate error"
        )

        # RADICALE WORKAROUND: Wait for first event to become searchable
        # Radicale CalDAV server doesn't immediately index events for search
        # Real CalDAV servers (iCloud, Google Calendar) don't need this
        indexed = await wait_for_radicale_indexing(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            event_summary="Doctor appointment",
            timeout_seconds=5.0,
        )
        assert indexed, "First event should become searchable within timeout"

        # Create second event with similar name at nearby time (15 min later)
        # This should be BLOCKED by duplicate detection
        event2_start = event1_start + timedelta(minutes=15)
        event2_end = event2_start + timedelta(hours=1)

        event2_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Doctor appt",  # Very similar to "Doctor appointment" (fuzzy similarity ~0.88)
            start_time=event2_start.isoformat(),
            end_time=event2_end.isoformat(),
        )

        logger.info(f"Event 2 result (should be error):\n{event2_result}")

        # Verify error is shown (event not created)
        assert "Error: Cannot create event" in event2_result, (
            "Error should be shown for similar event at nearby time"
        )
        assert "similar event(s) at nearby times" in event2_result
        assert "Doctor appointment" in event2_result, (
            "Error should mention the similar event"
        )
        assert "similarity:" in event2_result, "Error should include similarity score"
        assert "UID:" in event2_result, "Error should include UID for reference"
        assert "bypass_duplicate_check=true" in event2_result, (
            "Error should tell LLM how to bypass if not a duplicate"
        )

        # Now retry with bypass flag - should succeed
        event2_bypass_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Doctor appt",
            start_time=event2_start.isoformat(),
            end_time=event2_end.isoformat(),
            bypass_duplicate_check=True,
        )

        logger.info(f"Event 2 with bypass result:\n{event2_bypass_result}")

        # Verify success with bypass
        assert "OK. Event 'Doctor appt' added" in event2_bypass_result, (
            "Event should be created with bypass flag"
        )
        assert "duplicate check bypassed" in event2_bypass_result, (
            "Response should indicate bypass was used"
        )

    logger.info("Test duplicate detection error shown PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_duplicate_detection_no_error_different_time(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that duplicate detection does NOT block creation for similar events
    at different times (outside the time window).

    Verifies that the time window filtering works correctly - same-titled
    events on different days should not be blocked.
    """
    logger.info("Starting test_duplicate_detection_no_error_different_time...")

    base_url, username, password, calendar_url = radicale_server

    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": base_url,
                "username": username,
                "password": password,
                "calendar_urls": [calendar_url],
                "_use_naive_datetimes_for_search": True,
            },
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.30,
                "time_window_hours": 2,
            },
        },
    )

    async with DatabaseContext(pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conv",
            user_name="testuser",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            timezone_str="America/New_York",
        )

        # Create first event
        tomorrow = date.today() + timedelta(days=1)
        event1_start = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, 0
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        event1_end = event1_start + timedelta(hours=1)

        event1_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Weekly team meeting",
            start_time=event1_start.isoformat(),
            end_time=event1_end.isoformat(),
        )

        assert "OK. Event 'Weekly team meeting' added" in event1_result

        # Create second event with same title but different day (outside time window)
        next_week = date.today() + timedelta(days=8)
        event2_start = datetime(
            next_week.year, next_week.month, next_week.day, 14, 0, 0
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        event2_end = event2_start + timedelta(hours=1)

        event2_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Weekly team meeting",  # Exact same title
            start_time=event2_start.isoformat(),
            end_time=event2_end.isoformat(),
        )

        logger.info(f"Event 2 result:\n{event2_result}")

        # Verify NO error is shown (events are on different days, outside time window)
        assert "OK. Event 'Weekly team meeting' added" in event2_result
        assert "Error:" not in event2_result, (
            "No error should be shown for events outside time window"
        )

    logger.info("Test duplicate detection no error different time PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_duplicate_detection_disabled(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that duplicate detection can be disabled via configuration.

    Verifies that when duplicate_detection.enabled=False, no errors
    are shown even for similar events at nearby times.
    """
    logger.info("Starting test_duplicate_detection_disabled...")

    base_url, username, password, calendar_url = radicale_server

    # Config with duplicate detection DISABLED
    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": base_url,
                "username": username,
                "password": password,
                "calendar_urls": [calendar_url],
                "_use_naive_datetimes_for_search": True,
            },
            "duplicate_detection": {
                "enabled": False,  # Disabled
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.30,
            },
        },
    )

    async with DatabaseContext(pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conv",
            user_name="testuser",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            timezone_str="America/New_York",
        )

        # Create first event
        tomorrow = date.today() + timedelta(days=1)
        event1_start = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, 0
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        event1_end = event1_start + timedelta(hours=1)

        event1_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Doctor appointment",
            start_time=event1_start.isoformat(),
            end_time=event1_end.isoformat(),
        )

        assert "OK. Event 'Doctor appointment' added" in event1_result

        # Create second similar event at nearby time
        event2_start = event1_start + timedelta(minutes=15)
        event2_end = event2_start + timedelta(hours=1)

        event2_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Dr. Smith checkup",
            start_time=event2_start.isoformat(),
            end_time=event2_end.isoformat(),
        )

        logger.info(f"Event 2 result:\n{event2_result}")

        # Verify NO error is shown (duplicate detection disabled)
        assert "OK. Event 'Dr. Smith checkup' added" in event2_result
        assert "Error:" not in event2_result, (
            "No error should be shown when duplicate detection is disabled"
        )

    logger.info("Test duplicate detection disabled PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_duplicate_detection_all_day_events(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that duplicate detection works for all-day events.

    Verifies that all-day events are blocked for similar events on
    the same date, but not for events on different dates.
    """
    logger.info("Starting test_duplicate_detection_all_day_events...")

    base_url, username, password, calendar_url = radicale_server

    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": base_url,
                "username": username,
                "password": password,
                "calendar_urls": [calendar_url],
                "_use_naive_datetimes_for_search": True,
            },
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.30,
            },
        },
    )

    async with DatabaseContext(pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_conv",
            user_name="testuser",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            timezone_str="America/New_York",
        )

        # Create first all-day event
        tomorrow = date.today() + timedelta(days=1)
        day_after = tomorrow + timedelta(days=1)

        event1_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Birthday party",
            start_time=str(tomorrow),
            end_time=str(day_after),  # All-day events end on the day after
            all_day=True,
        )

        assert "OK. Event 'Birthday party' added" in event1_result

        # Create second all-day event with similar name on SAME date
        event2_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Birthday celebration",  # Similar
            start_time=str(tomorrow),
            end_time=str(day_after),
            all_day=True,
        )

        logger.info(f"Event 2 (same date) result:\n{event2_result}")

        # Should show error for same-date similar event
        assert "Error: Cannot create event" in event2_result, (
            "Error should be shown for similar all-day events on same date"
        )
        assert "Birthday party" in event2_result, "Error should mention similar event"

        # Now create third all-day event with similar name on DIFFERENT date
        # This should succeed (outside time window)
        next_week = date.today() + timedelta(days=8)
        next_week_day_after = next_week + timedelta(days=1)

        event3_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Birthday party",  # Same title as event1
            start_time=str(next_week),
            end_time=str(next_week_day_after),
            all_day=True,
        )

        logger.info(f"Event 3 (different date) result:\n{event3_result}")

        # Should NOT show error for different-date event
        assert "OK. Event 'Birthday party' added" in event3_result
        assert "Error:" not in event3_result, (
            "No error for all-day events on different dates"
        )

    logger.info("Test duplicate detection all-day events PASSED.")


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_duplicate_detection_exact_same_title(
    pg_vector_db_engine: AsyncEngine,
    radicale_server: tuple[str, str, str, str],
) -> None:
    """
    Test that duplicate detection catches events with exact same title.

    Regression test for bug where events with identical summaries were incorrectly
    skipped during duplicate detection, allowing duplicate creation.
    """
    logger.info("Starting test_duplicate_detection_exact_same_title...")

    base_url, username, password, calendar_url = radicale_server

    test_calendar_config = cast(
        "CalendarConfig",
        {
            "caldav": {
                "base_url": base_url,
                "username": username,
                "password": password,
                "calendar_urls": [calendar_url],
                "_use_naive_datetimes_for_search": True,
            },
            "duplicate_detection": {
                "enabled": True,
                "similarity_strategy": "fuzzy",
                "similarity_threshold": 0.30,
                "time_window_hours": 2,
            },
        },
    )

    async with DatabaseContext(pg_vector_db_engine) as db_context:
        exec_context = ToolExecutionContext(
            interface_type="test",
            conversation_id="test_exact_duplicate",
            user_name="testuser",
            turn_id="test_turn",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            attachment_registry=None,
            timezone_str="America/New_York",
        )

        # Create first event
        tomorrow = date.today() + timedelta(days=1)
        event1_start = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, 14, 0, 0
        ).replace(tzinfo=ZoneInfo("America/New_York"))
        event1_end = event1_start + timedelta(hours=1)

        event1_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Team Meeting",  # Exact title
            start_time=event1_start.isoformat(),
            end_time=event1_end.isoformat(),
        )

        logger.info(f"Event 1 result:\n{event1_result}")
        assert "OK. Event 'Team Meeting' added" in event1_result
        assert "Error:" not in event1_result

        # Wait for indexing
        indexed = await wait_for_radicale_indexing(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            event_summary="Team Meeting",
            timeout_seconds=10.0,
        )
        assert indexed, "Radicale failed to index first event"

        # Try to create second event with EXACT SAME title at nearby time
        # This should be blocked by duplicate detection
        event2_start = event1_start + timedelta(minutes=30)  # Within 2-hour window
        event2_end = event2_start + timedelta(hours=1)

        event2_result = await add_calendar_event_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            summary="Team Meeting",  # EXACT same title
            start_time=event2_start.isoformat(),
            end_time=event2_end.isoformat(),
        )

        logger.info(f"Event 2 (exact duplicate) result:\n{event2_result}")

        # Verify error is shown (event not created)
        assert "Error: Cannot create event" in event2_result, (
            "Exact duplicate should be blocked"
        )
        assert "Team Meeting" in event2_result, (
            "Error should mention the duplicate event"
        )
        assert "similarity: 1.00" in event2_result, (
            "Similarity should be 1.00 for exact match"
        )
        assert "bypass_duplicate_check=true" in event2_result

        # Verify only one event exists in calendar
        search_result = await search_calendar_events_tool(
            exec_context=exec_context,
            calendar_config=test_calendar_config,
            search_text="Team Meeting",
        )

        # Count how many times "Team Meeting" appears in the results
        # Should only appear once (for the first event)
        meeting_count = search_result.count("Team Meeting")
        assert meeting_count == 1, (
            f"Expected exactly 1 'Team Meeting' event, found {meeting_count}"
        )

    logger.info("Test duplicate detection exact same title PASSED.")


# TODO: Add tests for basic recurring events.
