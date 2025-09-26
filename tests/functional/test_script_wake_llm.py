"""
Functional tests for script wake_llm functionality.
"""

import asyncio
import logging
import tempfile
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext, get_db_context
from family_assistant.storage.events import EventActionType, EventSourceType
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
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

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
    async with DatabaseContext(engine=db_engine) as db_ctx:
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
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    # Event processor
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    processor._running = True
    await processor._refresh_listener_cache()

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
            last_msg = messages[-1]
            content = str(last_msg.get("content", ""))
            return (
                "Script wake_llm call" in content
                and "High temperature detected" in content
                and "temperature" in content
                and "27.5" in content
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
            timezone_str="UTC",
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config={},
            delegation_security_level="blocked",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
    )

    # Start task worker
    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        calendar_config={},
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    worker_task = asyncio.create_task(
        task_worker.run(new_task_event), name=f"WakeLLMWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 3: Process event that triggers the script
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.test_temperature",
            "old_state": {"state": "24.0"},
            "new_state": {"state": "27.5"},
        },
    )

    # Signal worker and wait for script execution
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Wait for LLM callback task
    await asyncio.sleep(0.5)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    # Step 4: Verify LLM was woken with correct context
    mock_chat_interface.send_message.assert_called_once()
    call_args = mock_chat_interface.send_message.call_args
    sent_text = call_args[1]["text"]
    assert "Temperature Alert" in sent_text
    assert "27.5°C" in sent_text
    assert "cooling system" in sent_text

    logger.info("LLM was successfully woken by script with temperature alert context")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Wake LLM Single Call Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_wake_llm_multiple_contexts(db_engine: AsyncEngine) -> None:
    """Test that multiple wake_llm calls accumulate into a single LLM wake."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Wake LLM Multiple Contexts Test ({test_run_id}) ---"
    )

    # Step 1: Create event listener with script that calls wake_llm multiple times
    async with DatabaseContext(engine=db_engine) as db_ctx:
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
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    processor._running = True
    await processor._refresh_listener_cache()

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

    # LLM client that checks for multiple accumulated contexts
    def multi_wake_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content = str(last_msg.get("content", ""))
            return (
                "Script wake_llm call" in content
                and "Multiple wake requests" in content
                and ("temperature" in content or "sensor" in content)
                and ("humidity" in content or "sensor" in content)
                and ("air_quality" in content or "sensor" in content)
            )
        return False

    llm_client = RuleBasedMockLLMClient(
        rules=[
            (
                multi_wake_matcher,
                LLMOutput(
                    content="🚨 Environment Alert: Multiple thresholds exceeded:\n- Temperature: 28°C (warning)\n- Humidity: 85% (critical)\n- Air Quality: 45 (warning)\n\nImmediate attention required!"
                ),
            )
        ],
        default_response=LLMOutput(content="Monitoring environment."),
    )

    processing_service = ProcessingService(
        llm_client=llm_client,
        tools_provider=tools_provider,
        service_config=ProcessingServiceConfig(
            id="event_handler",
            prompts={"system_prompt": "Event handler"},
            timezone_str="UTC",
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config={},
            delegation_security_level="blocked",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
    )

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        calendar_config={},
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    worker_task = asyncio.create_task(
        task_worker.run(new_task_event), name=f"MultiWakeWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 3: Process event with multiple threshold violations
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

    # Signal worker and wait for processing
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Wait for LLM callback
    await asyncio.sleep(0.5)
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

    # Step 4: Verify LLM was woken only once with all contexts
    mock_chat_interface.send_message.assert_called_once()
    call_args = mock_chat_interface.send_message.call_args
    sent_text = call_args[1]["text"]

    # Verify all three sensor alerts are mentioned
    assert "Temperature: 28°C" in sent_text
    assert "Humidity: 85%" in sent_text
    assert "Air Quality: 45" in sent_text
    assert "critical" in sent_text

    logger.info("LLM was woken once with all accumulated contexts")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(
        f"--- Script Wake LLM Multiple Contexts Test ({test_run_id}) Passed ---"
    )


@pytest.mark.asyncio
async def test_script_conditional_wake_llm(db_engine: AsyncEngine) -> None:
    """Test that wake_llm is only called when conditions are met."""
    test_run_id = uuid.uuid4()
    logger.info(f"\n--- Running Script Conditional Wake LLM Test ({test_run_id}) ---")

    # Step 1: Create event listener with conditional wake_llm
    async with DatabaseContext(engine=db_engine) as db_ctx:
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
    shutdown_event = asyncio.Event()
    new_task_event = asyncio.Event()

    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: get_db_context(db_engine),
    )
    processor._running = True
    await processor._refresh_listener_cache()

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
            timezone_str="UTC",
            max_history_messages=1,
            history_max_age_hours=1,
            tools_config={},
            delegation_security_level="blocked",
        ),
        app_config={},
        context_providers=[],
        server_url=None,
    )

    task_worker = TaskWorker(
        processing_service=processing_service,
        chat_interface=mock_chat_interface,
        timezone_str="UTC",
        embedding_generator=MagicMock(),
        calendar_config={},
        shutdown_event_instance=shutdown_event,
        engine=db_engine,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    worker_task = asyncio.create_task(
        task_worker.run(new_task_event), name=f"ConditionalWorker-{test_run_id}"
    )
    await asyncio.sleep(0.1)

    # Step 3: Process event with normal temperature (shouldn't wake LLM)
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "sensor.smart_temp",
            "old_state": {"state": "20.0"},
            "new_state": {"state": "22.5"},  # Normal temperature
        },
    )

    # Signal worker and wait for script execution
    new_task_event.set()
    await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

    # Give some time for any potential LLM callback
    await asyncio.sleep(0.5)
    new_task_event.set()

    # Step 4: Verify note was created but LLM was NOT woken
    async with DatabaseContext(engine=db_engine) as db_ctx:
        note = await db_ctx.notes.get_by_title("Temperature Log")
        assert note is not None
        assert "22.5°C" in note["content"]

    # LLM should not have been called
    mock_chat_interface.send_message.assert_not_called()

    logger.info("Script created note but did not wake LLM for normal temperature")

    # Cleanup
    shutdown_event.set()
    new_task_event.set()
    try:
        await asyncio.wait_for(worker_task, timeout=2.0)
    except asyncio.TimeoutError:
        worker_task.cancel()
        await asyncio.sleep(0.1)

    logger.info(f"--- Script Conditional Wake LLM Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_wake_llm_with_attachments(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that a script can wake the LLM with attachments included."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Wake LLM With Attachments Test ({test_run_id}) ---"
    )

    # Step 1: Create test attachments

    with tempfile.TemporaryDirectory() as temp_dir:
        attachment_registry = AttachmentRegistry(
            storage_path=temp_dir, db_engine=db_engine, config=None
        )

        # Create mock image attachment
        test_image_content = b"mock_image_data_for_wake_llm_test"
        async with DatabaseContext(engine=db_engine) as db_ctx:
            image_attachment = await attachment_registry.register_user_attachment(
                db_context=db_ctx,
                content=test_image_content,
                mime_type="image/png",
                filename="security_snapshot.png",
                conversation_id="security_system",
                user_id="security_camera",
                description="Security camera snapshot",
            )

        # Step 2: Create event listener with script that calls wake_llm with attachments
        async with DatabaseContext(engine=db_engine) as db_ctx:
            await db_ctx.events.create_event_listener(
                name=f"Security Alert {test_run_id}",
                source_id=EventSourceType.home_assistant,
                match_conditions={"entity_id": "binary_sensor.motion_detected"},
                conversation_id="security_system",
                interface_type="telegram",
                action_type=EventActionType.script,
                action_config={
                    "script_code": f'''
# Security motion detection script with image attachment
motion_detected = event["new_state"]["state"] == "on"

if motion_detected:
    # Wake LLM with security alert including camera attachment
    wake_llm({{
        "message": "Motion detected by security system",
        "alert_level": "medium",
        "location": "front_entrance",
        "timestamp": time_format(time_now(), "%Y-%m-%d %H:%M:%S"),
        "attachments": ["{image_attachment.attachment_id}"],
        "action_required": "Review security footage"
    }})
'''
                },
                enabled=True,
            )

        # Step 3: Set up LLM mock (task worker will be created later)

        # Mock LLM that detects wake_llm call with attachment
        def attachment_wake_matcher(args: dict) -> bool:
            messages = args.get("messages", [])
            if messages:
                last_msg = messages[-1]
                content = str(last_msg.get("content", ""))
                # Check if it's a wake_llm call with our attachment ID
                return (
                    "Script wake_llm call" in content
                    and "Motion detected by security system" in content
                    and image_attachment.attachment_id in content
                    and "front_entrance" in content
                )
            return False

        mock_llm_client = RuleBasedMockLLMClient(
            rules=[
                (
                    attachment_wake_matcher,
                    LLMOutput(
                        content="Security alert acknowledged. I can see the camera snapshot shows motion at the front entrance. Notifying security team."
                    ),
                ),
            ],
            default_response=LLMOutput(content="Default wake_llm response"),
        )

        # Mock chat interface to verify message sending
        mock_chat_interface = MagicMock(spec=ChatInterface)
        mock_chat_interface.send_message = AsyncMock()

        # Mock processing service with our LLM
        mock_processing_service = ProcessingService(
            service_config=ProcessingServiceConfig(
                id="security_assistant",
                prompts={"system_prompt": "You are a security monitoring assistant."},
                timezone_str="UTC",
                max_history_messages=1,
                history_max_age_hours=1,
                tools_config={},
                delegation_security_level="unrestricted",
            ),
            llm_client=mock_llm_client,
            tools_provider=CompositeToolsProvider(
                providers=[
                    LocalToolsProvider(
                        definitions=NOTE_TOOLS_DEFINITION,
                        implementations={
                            "add_or_update_note": local_tool_implementations[
                                "add_or_update_note"
                            ]
                        },
                    )
                ]
            ),
            context_providers=[],
            server_url="http://test:8000",
            app_config={},
            clock=MagicMock(),
            attachment_registry=attachment_registry,
        )

        # Get task worker events from manager
        worker, new_task_event, shutdown_event = task_worker_manager(
            processing_service=mock_processing_service,
            chat_interface=mock_chat_interface,
        )

        # Set up event processor and task worker
        processor = EventProcessor(
            sources={},
            sample_interval_hours=1.0,
            get_db_context_func=lambda: get_db_context(engine=db_engine),
        )
        processor._running = True

        task_worker = TaskWorker(
            processing_service=mock_processing_service,
            chat_interface=mock_chat_interface,
            timezone_str="UTC",
            embedding_generator=MagicMock(),
            calendar_config={},
            shutdown_event_instance=shutdown_event,
            engine=db_engine,
        )
        task_worker.register_task_handler("script_execution", handle_script_execution)
        task_worker.register_task_handler("llm_callback", handle_llm_callback)

        worker_task = asyncio.create_task(
            task_worker.run(new_task_event),
            name=f"WakeLLMAttachmentsWorker-{test_run_id}",
        )
        await asyncio.sleep(0.1)

        # Step 4: Process event that triggers the script
        await processor.process_event(
            "home_assistant",
            {
                "entity_id": "binary_sensor.motion_detected",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            },
        )

        # Signal worker and wait for script execution
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

        # Wait for LLM callback task
        await asyncio.sleep(0.5)
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

        # Step 5: Verify LLM was woken with correct context and attachment
        mock_chat_interface.send_message.assert_called_once()
        call_args = mock_chat_interface.send_message.call_args
        sent_text = call_args[1]["text"]

        # Verify the wake_llm message content
        assert "Security alert acknowledged" in sent_text
        assert "camera snapshot" in sent_text
        assert "front entrance" in sent_text
        assert "security team" in sent_text

        logger.info(
            "LLM was successfully woken by script with security alert and attachment"
        )

        # Step 6: Verify attachment was properly included in the LLM call
        # The LLM client would have received the attachment in the trigger_attachments
        # This is validated by our attachment_wake_matcher function above

        # Cleanup
        shutdown_event.set()
        new_task_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=2.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
            await asyncio.sleep(0.1)

        logger.info(
            f"--- Script Wake LLM With Attachments Test ({test_run_id}) Passed ---"
        )
