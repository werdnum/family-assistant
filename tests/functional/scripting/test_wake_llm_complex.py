"""
Functional tests for script wake_llm with complex scenarios.
Tests for attachments and tool results integration.
"""

import asyncio
import base64
import logging
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import aiofiles
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.llm.messages import ImageUrlContentPart
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
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
    last_real_message,
)

logger = logging.getLogger(__name__)


def _image_parts_sent_to_llm(
    llm_client: RuleBasedMockLLMClient,
) -> list[ImageUrlContentPart]:
    """Every image content part in the messages the LLM was asked to respond to."""
    image_parts: list[ImageUrlContentPart] = []
    for call in llm_client.get_calls():
        if call["method_name"] != "generate_response":
            continue
        for message in call["kwargs"]["messages"]:
            content = get_message_content(message)
            if isinstance(content, list):
                image_parts.extend(
                    part for part in content if isinstance(part, ImageUrlContentPart)
                )
    return image_parts


@pytest.mark.asyncio
async def test_script_wake_llm_with_attachments(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    tmp_path: Path,
) -> None:
    """Test that a script can wake the LLM with attachments included."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Wake LLM With Attachments Test ({test_run_id}) ---"
    )

    # Step 1: Create test attachments
    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )

    test_image_content = b"mock_image_data_for_wake_llm_test"
    db_ctx = Database(engine=db_engine)
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

    # Step 3: Set up LLM mock, chat interface and task worker
    def attachment_wake_matcher(args: dict) -> bool:
        messages = args.get("messages", [])
        if messages:
            last_msg = last_real_message(messages)
            if last_msg is None:
                return False
            msg_content = get_message_content(last_msg)
            content = extract_text_from_content(msg_content)
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

    mock_chat_interface = MagicMock(spec=ChatInterface)
    mock_chat_interface.send_message = AsyncMock(return_value="msg-id")

    mock_processing_service = ProcessingService(
        service_config=ProcessingServiceConfig(
            id="security_assistant",
            prompts={"system_prompt": "You are a security monitoring assistant."},
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
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
        app_config=AppConfig(),
        clock=SystemClock(),
        attachment_registry=attachment_registry,
    )

    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=mock_processing_service,
        chat_interface=mock_chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    # Step 4: Process event that triggers the script
    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )
    await processor.start()
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "binary_sensor.motion_detected",
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        },
    )
    await processor.stop()

    # The script task enqueues its llm_callback before it completes, so waiting
    # on both types covers the woken turn too.
    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    # Step 5: Verify the LLM was woken with the alert and replied to the user
    mock_chat_interface.send_message.assert_called_once()
    sent_text = mock_chat_interface.send_message.call_args.kwargs["text"]
    assert "Security alert acknowledged" in sent_text
    assert "camera snapshot" in sent_text
    assert "front entrance" in sent_text
    assert "security team" in sent_text

    # Step 6: Verify the attachment reached the LLM as the image itself, not
    # merely as an id mentioned in the wake message text
    attached_images = [
        part
        for part in _image_parts_sent_to_llm(mock_llm_client)
        if part.attachment_id == image_attachment.attachment_id
    ]
    assert attached_images, (
        f"LLM should have received attachment {image_attachment.attachment_id} "
        "as an image content part"
    )
    expected_data_uri = (
        f"data:image/png;base64,{base64.b64encode(test_image_content).decode()}"
    )
    assert attached_images[0].image_url["url"] == expected_data_uri

    logger.info(f"--- Script Wake LLM With Attachments Test ({test_run_id}) Passed ---")


@pytest.mark.asyncio
async def test_script_tool_result_attachment_to_wake_llm(
    db_engine: AsyncEngine,
    task_worker_manager: Callable[..., tuple[TaskWorker, asyncio.Event, asyncio.Event]],
    tmp_path: Path,
) -> None:
    """Test that scripts can pass ToolResult attachments from tools to wake_llm."""
    test_run_id = uuid.uuid4()
    logger.info(
        f"\n--- Running Script Tool Result to Wake LLM Test ({test_run_id}) ---"
    )

    attachment_registry = AttachmentRegistry(
        storage_path=str(tmp_path), db_engine=db_engine, config=None
    )

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
    db_ctx = Database(engine=db_engine)
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
# Note: Using type() comparison because the scripting sandbox doesn't have isinstance()
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
    "attachments": [attachment_id]
})
"""
        },
        enabled=True,
    )

    # Set up LLM mock to capture the attachment id it receives
    received_attachment_id = None

    def wake_llm_matcher(args: dict) -> bool:
        nonlocal received_attachment_id
        messages = args.get("messages", [])
        if messages:
            last_msg = last_real_message(messages)
            if last_msg is None:
                return False
            msg_content = get_message_content(last_msg)
            content = extract_text_from_content(msg_content)
            if "Motion detected!" in content and "camera snapshot" in content.lower():
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
    mock_chat_interface.send_message = AsyncMock(return_value="msg-id")

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
            timezone=ZoneInfo("UTC"),
            history_budget_chars=0,
            history_min_turns=0,
            history_max_age_hours=1,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.UNRESTRICTED,
        ),
        llm_client=mock_llm_client,
        tools_provider=tools_provider,
        context_providers=[],
        server_url="http://test:8000",
        app_config=AppConfig(),
        clock=SystemClock(),
        attachment_registry=attachment_registry,
    )

    task_worker, new_task_event, _ = task_worker_manager(
        processing_service=mock_processing_service,
        chat_interface=mock_chat_interface,
        register_delegation_handler=False,
    )
    task_worker.register_task_handler("script_execution", handle_script_execution)
    task_worker.register_task_handler("llm_callback", handle_llm_callback)

    processor = EventProcessor(
        sources={},
        sample_interval_hours=1.0,
        get_db_context_func=lambda: Database(engine=db_engine),
        timezone=ZoneInfo("Australia/Sydney"),
    )
    await processor.start()
    await processor.process_event(
        "home_assistant",
        {
            "entity_id": "binary_sensor.motion",
            "old_state": {"state": "off"},
            "new_state": {"state": "on"},
        },
    )
    await processor.stop()

    new_task_event.set()
    await wait_for_tasks_to_complete(
        db_engine, task_types={"script_execution", "llm_callback"}
    )

    # Verify LLM was called with attachment
    mock_chat_interface.send_message.assert_called_once()
    sent_text = mock_chat_interface.send_message.call_args.kwargs["text"]
    assert "camera snapshot" in sent_text.lower()

    assert received_attachment_id is not None, (
        "LLM should have received an attachment ID in the wake_llm context"
    )

    # Verify the attachment ID is actually registered and has correct content
    attachment_metadata = await attachment_registry.get_attachment(
        db_ctx, received_attachment_id, acting_user_id=None
    )
    assert attachment_metadata is not None, (
        f"Attachment {received_attachment_id} should exist in registry"
    )
    assert attachment_metadata.mime_type == "image/jpeg"

    attachment_path = attachment_registry.get_attachment_path(received_attachment_id)
    assert attachment_path is not None, "Attachment path should be found"
    assert attachment_path.exists(), "Attachment file should exist"
    async with aiofiles.open(attachment_path, "rb") as f:
        content = await f.read()
    assert content == b"mock_camera_image_data", "Attachment content should match"

    attached_images = [
        part
        for part in _image_parts_sent_to_llm(mock_llm_client)
        if part.attachment_id == received_attachment_id
    ]
    assert attached_images, "ToolResult image should reach the LLM"
    expected_data_uri = f"data:image/jpeg;base64,{base64.b64encode(content).decode()}"
    assert attached_images[0].image_url["url"] == expected_data_uri

    logger.info(f"--- Script Tool Result to Wake LLM Test ({test_run_id}) Passed ---")
