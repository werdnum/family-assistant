"""
Functional tests for script wake_llm functionality.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.storage.database import Database
from family_assistant.storage.events import EventActionType, EventSourceType
from family_assistant.storage.repositories.notes import NoteReadPolicy
from family_assistant.task_worker import (
    TaskWorker,
    handle_llm_callback,
    handle_script_execution,
)
from family_assistant.tools import (
    AVAILABLE_FUNCTIONS as local_tool_implementations,
)
from family_assistant.tools import (
    NOTE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
)
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
    last_real_message,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_script_wake_llm_single_call(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that a script can wake the LLM with a single context."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Wake LLM Single Call Test ({test_run_id}) ---")

    # Step 1: Create event listener with script that calls wake_llm
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name=f"Temperature Alert {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={
            "entity_id": "sensor.test_temperature",
        },
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={
            "script_code": """
temp = float(event["new_state"]["state"])
if temp > 25.0:
    wake_llm({
        "alert": "High temperature detected",
        "temperature": temp,
        "action_needed": "Please check the cooling system"
    })
"""
        },
        enabled=True,
    )

    # Step 2: Create infrastructure
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    # Real tools provider
    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    # Mock chat interface
    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_wake_message_id"

    # LLM client with rule to match wake_llm context
    def wake_llm_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        if messages:
            last_msg = last_real_message(messages)
            content = str(getattr(last_msg, "content", "") or "")
            return (
                "Script wake_llm call" in content
                and "High temperature detected" in content
                and '"temperature": 27.5' in content
            )
        return False

    llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                wake_llm_matcher,
                LLMOutput(
                    content="⚠️ Temperature Alert: The temperature has reached 27.5°C. I'll check the cooling system status now."
                ),
            )
        ],
        default_response=LLMOutput(content="Acknowledged."),
    )

    # Processing service for event_handler profile
    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    # Step 3: Process event that triggers the script
    await processor.start()
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.test_temperature",
            "old_state": {"state": "24.0"},
            "new_state": {"state": "27.5"},
        },
    )
    await processor.stop()

    # The script task enqueues its llm_callback before it completes, so waiting
    # on both types covers the woken turn too.
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    # Step 4: Verify LLM was woken with correct context
    mock_chat_interface.send_message.assert_called_once()
    call_args = mock_chat_interface.send_message.call_args
    sent_text = call_args[1]["text"]
    assert "Temperature Alert" in sent_text
    assert "27.5°C" in sent_text
    assert "cooling system" in sent_text

    logger.info(f"--- Script Wake LLM Single Call Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_wake_llm_multiple_contexts(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that multiple wake_llm calls accumulate into a single LLM wake."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Wake LLM Multiple Contexts Test ({test_run_id}) ---"
    )

    # Step 1: Create event listener with script that calls wake_llm multiple times
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name=f"Multi-Sensor Monitor {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={
            "entity_id": "sensor.environment",
        },
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={
            "script_code": """
# Check temperature
temp = float(event["new_state"]["attributes"]["temperature"])
if temp > 25:
    wake_llm({
        "sensor": "temperature",
        "value": temp,
        "threshold": 25,
        "severity": "warning"
    })

# Check humidity
humidity = float(event["new_state"]["attributes"]["humidity"])
if humidity > 80:
    wake_llm({
        "sensor": "humidity",
        "value": humidity,
        "threshold": 80,
        "severity": "critical"
    })

# Check air quality
air_quality = float(event["new_state"]["attributes"]["air_quality"])
if air_quality < 50:
    wake_llm({
        "sensor": "air_quality",
        "value": air_quality,
        "threshold": 50,
        "severity": "warning"
    })
"""
        },
        enabled=True,
    )

    # Step 2: Create infrastructure
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_multi_wake_message_id"

    llm_client = RuleBasedMockLLMClient(
        rules=[],
        default_response=LLMOutput(content="Monitoring environment."),
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    # Step 3: Process event with multiple threshold violations
    await processor.start()
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.environment",
            "old_state": {
                "state": "normal",
                "attributes": {"temperature": 22, "humidity": 60, "air_quality": 75},
            },
            "new_state": {
                "state": "alert",
                "attributes": {"temperature": 28, "humidity": 85, "air_quality": 45},
            },
        },
    )
    await processor.stop()

    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    # Step 4: Verify the LLM was woken exactly once, with every context
    assert len(await db_ctx.tasks.get_all(task_type="llm_callback")) == 1
    mock_chat_interface.send_message.assert_called_once()

    wake_prompts = {
        text
        for call in llm_client.get_calls()
        if call["method_name"] == "generate_response"
        for message in call["kwargs"]["messages"]
        if "Script wake_llm call"
        in (text := extract_text_from_content(get_message_content(message)))
    }
    assert len(wake_prompts) == 1, wake_prompts
    wake_prompt = wake_prompts.pop()
    assert "Multiple wake requests (3)" in wake_prompt
    for expected in (
        '"sensor": "temperature"',
        '"value": 28.0',
        '"sensor": "humidity"',
        '"value": 85.0',
        '"severity": "critical"',
        '"sensor": "air_quality"',
        '"value": 45.0',
    ):
        assert expected in wake_prompt, f"{expected!r} missing from:\n{wake_prompt}"

    logger.info(
        f"--- Script Wake LLM Multiple Contexts Test ({test_run_id}) Passed ---"
    )


@pytest.mark.asyncio
async def test_script_conditional_wake_llm(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that wake_llm is only called when conditions are met."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Conditional Wake LLM Test ({test_run_id}) ---")

    # Step 1: Create event listener with conditional wake_llm
    db_ctx = Database(engine=db_engine)
    await db_ctx.events.create_event_listener(
        name=f"Smart Temperature Monitor {test_run_id}",
        source_id=EventSourceType.home_assistant,
        match_conditions={
            "entity_id": "sensor.smart_temp",
        },
        conversation_id="test_conv",
        interface_type="telegram",
        action_type=EventActionType.script,
        action_config={
            "script_code": """
temp = float(event["new_state"]["state"])

# Log all temperature changes
add_or_update_note(
    title="Temperature Log",
    content=f"Temperature changed to {temp}°C at " + time_format(time_now(), "%H:%M:%S")
)

# Only wake LLM for extreme temperatures
if temp > 30 or temp < 10:
    wake_llm({
        "alert_type": "extreme_temperature",
        "temperature": temp,
        "temp_is_high": temp > 30,
        "recommendation": "immediate_action"
    })
"""
        },
        enabled=True,
    )

    # Step 2: Create infrastructure
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )

    local_provider = LocalToolsProvider(
        definitions=NOTE_TOOLS_DEFINITION,
        implementations={
            "add_or_update_note": local_tool_implementations["add_or_update_note"]
        },
    )
    tools_provider = CompositeToolsProvider(providers=[local_provider])
    await tools_provider.get_tool_definitions()

    mock_chat_interface = AsyncMock(spec=ChatInterface)
    mock_chat_interface.send_message.return_value = "mock_conditional_message_id"

    llm_client = RuleBasedMockLLMClient(
        rules=[],  # No rules needed - LLM shouldn't be called
        default_response=LLMOutput(content="This should not be called."),
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone=ZoneInfo("UTC"),
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
        ),
        app_config=AppConfig(),
        context_providers=[],
        server_url=None,
    )

    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    # Step 3: Process event with normal temperature (shouldn't wake LLM)
    await processor.start()
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.smart_temp",
            "old_state": {"state": "20.0"},
            "new_state": {"state": "22.5"},  # Normal temperature
        },
    )
    await processor.stop()

    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Step 4: Verify note was created but LLM was NOT woken. A wake would have
    # been enqueued inside the completed script task, so its absence is final.
    note = await db_ctx.notes.get_by_title(
        "Temperature Log", read_policy=NoteReadPolicy.UNRESTRICTED
    )
    assert note is not None
    assert "22.5°C" in note.content

    assert await db_ctx.tasks.get_all(task_type="llm_callback") == []
    assert llm_client.get_calls() == []
    mock_chat_interface.send_message.assert_not_called()

    logger.info(f"--- Script Conditional Wake LLM Test ({test_run_id}) Passed ---")
