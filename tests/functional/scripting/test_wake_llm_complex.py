"""
Functional tests for script wake_llm with complex scenarios.
Tests for attachments and tool results integration.
"""

import asyncio
import logging
import re
import tempfile
import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import aiofiles
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
from family_assistant.tools.types import (
    ToolAttachment,
    ToolExecutionContext,
    ToolResult,
)
from family_assistant.utils.clock import SystemClock
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput,
    RuleBasedMockLLMClient,
    extract_text_from_content,
    get_message_content,
)

logger = logging.getLogger(__name__)


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
                # Use helper functions to work with typed messages
                msg_content = get_message_content(last_msg)
                content = extract_text_from_content(msg_content)
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
            clock=SystemClock(),
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
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start
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
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for LLM callback task to be created
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
        except TimeoutError:
            worker_task.cancel()
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Allowing task cancellation to complete
            await asyncio.sleep(0.1)

        logger.info(
            f"--- Script Wake LLM With Attachments Test ({test_run_id}) Passed ---"
        )


@pytest.mark.asyncio
async def test_script_tool_result_attachment_to_wake_llm(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
) -> None:
    """Test that scripts can pass ToolResult attachments from tools to wake_llm."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Tool Result to Wake LLM Test ({test_run_id}) ---"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        attachment_registry = AttachmentRegistry(
            storage_path=temp_dir, db_engine=db_engine, config=None
        )

        # Create a mock tool that returns ToolResult with camera snapshot
        async def mock_camera_snapshot_tool(
            exec_context: ToolExecutionContext,
        ) -> ToolResult:
            """Mock camera tool that returns snapshot as ToolResult."""
            return ToolResult(
                text="Retrieved snapshot from camera",
                attachments=[
                    ToolAttachment(
                        mime_type="image/jpeg",
                        description="Camera snapshot",
                        content=b"mock_camera_image_data",
                    )
                ],
            )

        # Create event listener with script that gets camera snapshot and wakes LLM
        async with DatabaseContext(engine=db_engine) as db_ctx:
            await db_ctx.events.create_event_listener(
                name=f"Camera Check {test_run_id}",
                source_id=EventSourceType.home_assistant,
                match_conditions={"entity_id": "binary_sensor.motion"},
                conversation_id="camera_system",
                interface_type="telegram",
                action_type=EventActionType.script,
                action_config={
                    "script_code": """
# Get camera snapshot - returns dict with text + attachments
snapshot_result = get_camera_snapshot()

# Extract attachment ID from the result dict
# Tools that return text + attachments return: {"text": "...", "attachments": [{...}, ...]}
# Tools that return single attachment with no text return: {"id": uuid, "mime_type": ..., ...}
# Note: Using type() comparison because Starlark doesn't have isinstance()
if type(snapshot_result) == type({}):
    if "id" in snapshot_result:
        # Single attachment, no text
        attachment_id = snapshot_result["id"]
    elif "attachments" in snapshot_result and len(snapshot_result["attachments"]) > 0:
        # Text + attachments - get first attachment
        attachment_id = snapshot_result["attachments"][0]["id"]
    else:
        attachment_id = None
else:
    # Fallback for string UUID
    attachment_id = snapshot_result

# Wake LLM with the snapshot attachment
wake_llm({
    "message": "Motion detected! Check the camera snapshot.",
    "attachment_id": attachment_id
})
"""
                },
                enabled=True,
            )

        # Set up LLM mock to verify it receives the attachment
        received_attachment_id = None

        def wake_llm_matcher(args: dict) -> bool:
            nonlocal received_attachment_id
            messages = args.get("messages", [])
            if messages:
                last_msg = messages[-1]
                # Use helper functions to work with typed messages
                msg_content = get_message_content(last_msg)
                content = extract_text_from_content(msg_content)
                # Check if wake_llm was called with our content
                if (
                    "Motion detected!" in content
                    and "camera snapshot" in content.lower()
                ):
                    # Extract attachment ID from the context
                    # Look for attachment ID pattern in the message
                    match = re.search(
                        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
                        content,
                    )
                    if match:
                        received_attachment_id = match.group(0)
                    return True
            return False

        mock_llm_client = RuleBasedMockLLMClient(
            rules=[
                (
                    wake_llm_matcher,
                    LLMOutput(content="I see the camera snapshot. All clear."),
                ),
            ],
            default_response=LLMOutput(content="Default response"),
        )

        mock_chat_interface = MagicMock(spec=ChatInterface)
        mock_chat_interface.send_message = AsyncMock()

        tools_provider = CompositeToolsProvider(
            providers=[
                LocalToolsProvider(
                    definitions=[
                        {
                            "type": "function",
                            "function": {
                                "name": "get_camera_snapshot",
                                "description": "Get camera snapshot",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                    implementations={"get_camera_snapshot": mock_camera_snapshot_tool},
                )
            ]
        )

        mock_processing_service = ProcessingService(
            service_config=ProcessingServiceConfig(
                id="camera_assistant",
                prompts={"system_prompt": "You are a security camera assistant."},
                timezone_str="UTC",
                max_history_messages=1,
                history_max_age_hours=1,
                tools_config={},
                delegation_security_level="unrestricted",
            ),
            llm_client=mock_llm_client,
            tools_provider=tools_provider,
            context_providers=[],
            server_url="http://test:8000",
            app_config={},
            clock=SystemClock(),
            attachment_registry=attachment_registry,
        )

        worker, new_task_event, shutdown_event = task_worker_manager(
            processing_service=mock_processing_service,
            chat_interface=mock_chat_interface,
        )

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
            name=f"CameraWakeLLMWorker-{test_run_id}",
        )
        # ast-grep-ignore: no-asyncio-sleep-in-tests - Waiting for task worker to start
        await asyncio.sleep(0.1)

        # Trigger the event
        await processor.process_event(
            "home_assistant",
            {
                "entity_id": "binary_sensor.motion",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            },
        )

        # Wait for script execution
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

        # Wait for LLM callback
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

        # Verify LLM was called with attachment
        mock_chat_interface.send_message.assert_called_once()
        call_args = mock_chat_interface.send_message.call_args
        sent_text = call_args[1]["text"]
        assert "camera snapshot" in sent_text.lower()

        # Verify we extracted an attachment ID from the wake_llm context
        assert received_attachment_id is not None, (
            "LLM should have received an attachment ID in the wake_llm context"
        )
        assert len(received_attachment_id) == 36, "Should be a valid UUID"

        # Verify the attachment ID is actually registered and has correct content
        async with DatabaseContext(engine=db_engine) as db_ctx:
            attachment_metadata = await attachment_registry.get_attachment(
                db_ctx, received_attachment_id
            )
        assert attachment_metadata is not None, (
            f"Attachment {received_attachment_id} should exist in registry"
        )
        assert attachment_metadata.mime_type == "image/jpeg"

        # Verify the actual attachment content
        attachment_path = attachment_registry.get_attachment_path(
            received_attachment_id
        )
        assert attachment_path is not None, "Attachment path should be found"
        assert attachment_path.exists(), "Attachment file should exist"
        async with aiofiles.open(attachment_path, "rb") as f:
            content = await f.read()
        assert content == b"mock_camera_image_data", "Attachment content should match"

        logger.info(
            f"Successfully verified: ToolResult attachment was passed to wake_llm with ID {received_attachment_id} "
            f"and exists in registry with correct content"
        )

        # Cleanup
        shutdown_event.set()
        new_task_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=2.0)
        except TimeoutError:
            worker_task.cancel()
            # ast-grep-ignore: no-asyncio-sleep-in-tests - Allowing task cancellation to complete
            await asyncio.sleep(0.1)

        logger.info(
            f"--- Script Tool Result to Wake LLM Test ({test_run_id}) Passed ---"
        )
