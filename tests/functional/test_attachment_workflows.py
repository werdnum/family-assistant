"""End-to-end tests for attachment manipulation workflows."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    ATTACHMENT_TOOLS_DEFINITION,
    COMMUNICATION_TOOLS_DEFINITION,
    HOME_ASSISTANT_TOOLS_DEFINITION,
    MOCK_IMAGE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
    ToolsProvider,
)
from family_assistant.tools import AVAILABLE_FUNCTIONS as local_tool_implementations
from family_assistant.tools.types import ToolExecutionContext, ToolResult
from tests.helpers import wait_for_tasks_to_complete
from tests.mocks.mock_llm import LLMOutput, RuleBasedMockLLMClient

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


class TestAttachmentWorkflows:
    """Test complete attachment manipulation workflows."""

    @pytest.fixture
    async def attachment_registry(
        self, tmp_path: Path, db_engine: AsyncEngine
    ) -> AttachmentRegistry:
        """Create a real AttachmentRegistry for testing."""
        # Create a temporary directory for test attachments
        test_storage = tmp_path / "test_attachments"
        test_storage.mkdir(exist_ok=True)
        return AttachmentRegistry(storage_path=str(test_storage), db_engine=db_engine)

    @pytest.fixture
    async def attachment_tools_provider(self) -> ToolsProvider:
        """Create a tools provider with attachment-related tools."""

        local_provider = LocalToolsProvider(
            definitions=(
                HOME_ASSISTANT_TOOLS_DEFINITION
                + ATTACHMENT_TOOLS_DEFINITION
                + MOCK_IMAGE_TOOLS_DEFINITION
                + COMMUNICATION_TOOLS_DEFINITION
            ),
            implementations={
                "mock_camera_snapshot": local_tool_implementations[
                    "mock_camera_snapshot"
                ],
                "attach_to_response": local_tool_implementations["attach_to_response"],
                "annotate_image": local_tool_implementations["annotate_image"],
                "send_message_to_user": local_tool_implementations[
                    "send_message_to_user"
                ],
            },
        )
        tools_provider = CompositeToolsProvider(providers=[local_provider])
        await tools_provider.get_tool_definitions()
        return tools_provider

    async def test_camera_annotate_response_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_tools_provider: ToolsProvider,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test Camera → Annotate → Response workflow."""
        async with DatabaseContext(engine=db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
            )

            # Step 1: Get camera snapshot (using mock camera tool)
            camera_result = await attachment_tools_provider.execute_tool(
                name="mock_camera_snapshot",
                arguments={"entity_id": "camera.front_door"},
                context=exec_context,
            )

            # Should return successful result with attachment
            # The mock camera tool should return a ToolResult with attachment
            if isinstance(camera_result, ToolResult):
                # It's a ToolResult
                assert "snapshot" in camera_result.get_text().lower()
                assert camera_result.attachments and len(camera_result.attachments) > 0
                assert camera_result.attachments[0].mime_type.startswith("image/")
                assert camera_result.attachments[0].content is not None
                camera_content = camera_result.attachments[0].content
                camera_mime = camera_result.attachments[0].mime_type
            else:
                # Fallback if mock tool returns string (should not happen)
                camera_content = b"fake_camera_image_data"
                camera_mime = "image/png"

            # Use the attachment registry directly (already configured)
            # Store file first, then register as tool attachment
            camera_data = await attachment_registry._store_file_only(
                camera_content,
                "camera_snapshot.png",
                camera_mime,
            )
            camera_attachment_id = camera_data.attachment_id

            await attachment_registry.register_tool_attachment(
                db_context=db_context,
                attachment_id=camera_attachment_id,
                tool_name="mock_camera_snapshot",
                mime_type=camera_mime,
                description="Camera snapshot",
                size=len(camera_content),
                content_url=camera_data.content_url or "",
                storage_path=camera_data.storage_path,
                conversation_id="test_conversation",
            )

            # Step 2: Annotate the camera image
            annotate_result = await attachment_tools_provider.execute_tool(
                name="annotate_image",
                arguments={
                    "image_attachment_id": camera_attachment_id,
                    "annotation_text": "Motion detected at 2:30 PM",
                    "position": "top-right",
                },
                context=exec_context,
            )

            # Should return successful annotation with new attachment
            if isinstance(annotate_result, ToolResult):
                # It's a ToolResult
                assert "annotated" in annotate_result.get_text().lower()
                assert (
                    annotate_result.attachments and len(annotate_result.attachments) > 0
                )
                assert annotate_result.attachments[0].mime_type.startswith("image/")
                assert annotate_result.attachments[0].content is not None
                annotated_content = annotate_result.attachments[0].content
                annotated_mime = annotate_result.attachments[0].mime_type
            else:
                # It's a string result, create mock annotated attachment
                annotated_content = camera_content + b"_annotated"
                annotated_mime = camera_mime

            # Store the annotated attachment
            annotated_data = await attachment_registry._store_file_only(
                annotated_content,
                "annotated_image.png",
                annotated_mime,
            )
            annotated_attachment_id = annotated_data.attachment_id

            # Register the annotated attachment
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=annotated_attachment_id,
                source_type="tool",
                source_id="annotate_image",
                mime_type=annotated_mime,
                description="Annotated image",
                size=len(annotated_content),
                content_url=annotated_data.content_url or "",
                storage_path=annotated_data.storage_path,
                conversation_id="test_conversation",
            )

            # Step 3: Attach annotated image to response
            attach_result = await attachment_tools_provider.execute_tool(
                name="attach_to_response",
                arguments={"attachment_ids": [annotated_attachment_id]},
                context=exec_context,
            )

            # Should return successful attachment to response
            result_text = (
                attach_result
                if isinstance(attach_result, str)
                else attach_result.get_text()
            )
            assert (
                "sent" in result_text.lower()
                or "attached" in result_text.lower()
                or "queued" in result_text.lower()
            )

            # Verify the workflow created the expected chain
            # Camera → Annotation → Response
            # We should have two attachments registered
            all_attachments = await attachment_registry.list_attachments(
                db_context=db_context,
                conversation_id="test_conversation",
            )

            assert len(all_attachments) == 2

            # Find camera and annotated attachments
            camera_att = next(
                (
                    att
                    for att in all_attachments
                    if att.source_id == "mock_camera_snapshot"
                ),
                None,
            )
            annotated_att = next(
                (att for att in all_attachments if att.source_id == "annotate_image"),
                None,
            )

            assert camera_att is not None
            assert annotated_att is not None
            assert camera_att.mime_type.startswith("image/")
            assert annotated_att.mime_type.startswith("image/")

    async def test_user_image_process_send_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_tools_provider: ToolsProvider,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test User Image → Process → Send to Another User workflow."""
        async with DatabaseContext(engine=db_engine) as db_context:
            # Create execution context
            exec_context = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
            )

            # Step 1: Simulate user uploading an image
            # In real usage, this would come from Telegram/Web interface
            user_image_content = b"fake_user_uploaded_image_data" + b"\x00" * 200
            # Register user attachment (includes storage)
            user_attachment_metadata = (
                await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=user_image_content,
                    filename="user_photo.jpg",
                    mime_type="image/jpeg",
                    conversation_id="test_conversation",
                    description="User uploaded photo",
                )
            )
            user_attachment_id = user_attachment_metadata.attachment_id

            # User attachment already registered above

            # Step 2: Process the user image (annotate it)
            process_result = await attachment_tools_provider.execute_tool(
                name="annotate_image",
                arguments={
                    "image_attachment_id": user_attachment_id,
                    "annotation_text": "Enhanced by AI assistant",
                    "position": "bottom-right",
                },
                context=exec_context,
            )

            # Should return successful processing with new attachment
            # Enforce strict contract: annotate_image tool must return ToolResult
            assert isinstance(process_result, ToolResult), (
                f"Expected ToolResult, got {type(process_result)}"
            )
            assert "annotated" in process_result.get_text().lower()
            assert process_result.attachments and len(process_result.attachments) > 0
            assert process_result.attachments[0].mime_type.startswith("image/")
            assert process_result.attachments[0].content is not None

            processed_content = process_result.attachments[0].content
            processed_mime = process_result.attachments[0].mime_type

            # Store the processed attachment
            processed_data = await attachment_registry._store_file_only(
                processed_content,
                "processed_photo.jpg",
                processed_mime,
            )
            processed_attachment_id = processed_data.attachment_id

            # Register the processed attachment
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=processed_attachment_id,
                source_type="tool",
                source_id="annotate_image",
                mime_type=processed_mime,
                description="Processed user photo",
                size=len(processed_content),
                content_url=processed_data.content_url or "",
                storage_path=processed_data.storage_path,
                conversation_id="test_conversation",
            )

            # Step 3: Send processed image to another user
            # We'll use a fake target chat ID for testing
            target_chat_id = 987654321

            # Create a mock chat interface for testing
            mock_chat_interface = AsyncMock()
            mock_chat_interface.send_message.return_value = "mock_message_id_123"

            # Temporarily set the chat interface in the execution context
            exec_context_with_chat = ToolExecutionContext(
                interface_type="test",
                conversation_id="test_conversation",
                user_name="test_user",
                turn_id="test_turn",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=attachment_registry,
                chat_interface=mock_chat_interface,
            )

            send_result = await attachment_tools_provider.execute_tool(
                name="send_message_to_user",
                arguments={
                    "target_chat_id": target_chat_id,
                    "message_content": "Here's your enhanced photo!",
                    "attachment_ids": [processed_attachment_id],
                },
                context=exec_context_with_chat,
            )

            # Should return successful sending
            # Handle ToolResult return type from tools provider
            result_text = (
                send_result.get_text()
                if isinstance(send_result, ToolResult)
                else send_result
            )
            assert (
                "sent successfully" in result_text.lower()
                or "message sent" in result_text.lower()
            )
            assert str(target_chat_id) in result_text

            # Verify the chat interface was called correctly
            mock_chat_interface.send_message.assert_called_once_with(
                conversation_id=str(target_chat_id),
                text="Here's your enhanced photo!",
                attachment_ids=[processed_attachment_id],
            )

            # Verify the workflow created the expected attachments
            # User → Processed → Sent to another user
            all_attachments = await attachment_registry.list_attachments(
                db_context=db_context,
                conversation_id="test_conversation",
            )

            assert len(all_attachments) == 2  # User + processed attachment

            # Find user and processed attachments
            user_att = next(
                (att for att in all_attachments if att.source_type == "user"), None
            )
            processed_att = next(
                (att for att in all_attachments if att.source_id == "annotate_image"),
                None,
            )

            assert user_att is not None
            assert processed_att is not None
            assert user_att.mime_type == "image/jpeg"
            assert processed_att.mime_type.startswith("image/")
            assert user_att.description == "User uploaded photo"
            assert processed_att.description == "Processed user photo"

    async def test_event_script_camera_wake_llm_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """Test Event → Script → Camera → Wake LLM workflow with attachments."""
        test_run_id = uuid.uuid4()

        async with DatabaseContext(engine=db_engine) as db_ctx:
            # Step 1: Create event listener with script that calls camera and wake_llm
            await db_ctx.events.create_event_listener(
                name=f"Security Camera Alert {test_run_id}",
                source_id=EventSourceType.home_assistant,
                match_conditions={
                    "entity_id": "binary_sensor.motion_detector",
                },
                conversation_id="test_conversation",
                interface_type="telegram",
                action_type=EventActionType.script,
                action_config={
                    "script_code": """
# Motion detected, take camera snapshot
camera_result = tools_execute("mock_camera_snapshot", entity_id="camera.front_door")

# Check if we got an attachment
if camera_result and "Successfully captured" in camera_result:
    # Get the attachment info from the last tool execution
    # In real usage, the camera tool would return attachment metadata
    wake_llm({
        "alert_type": "motion_detection",
        "location": "front_door",
        "timestamp": time_format(time_now(), "%Y-%m-%d %H:%M:%S"),
        "camera_snapshot": "captured",
        "action_needed": "Review security footage"
    })
else:
    wake_llm({
        "alert_type": "motion_detection_failed",
        "location": "front_door",
        "error": "Camera snapshot failed"
    })
"""
                },
                enabled=True,
            )

        # Step 2: Create infrastructure with attachment support
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

        # Tools provider with camera and attachment tools
        local_provider = LocalToolsProvider(
            definitions=(
                ATTACHMENT_TOOLS_DEFINITION
                + MOCK_IMAGE_TOOLS_DEFINITION
                + COMMUNICATION_TOOLS_DEFINITION
            ),
            implementations={
                "mock_camera_snapshot": local_tool_implementations[
                    "mock_camera_snapshot"
                ],
                "attach_to_response": local_tool_implementations["attach_to_response"],
                "send_message_to_user": local_tool_implementations[
                    "send_message_to_user"
                ],
            },
        )
        tools_provider = CompositeToolsProvider(providers=[local_provider])
        await tools_provider.get_tool_definitions()

        # Mock chat interface
        mock_chat_interface = AsyncMock(spec=ChatInterface)
        mock_chat_interface.send_message.return_value = "mock_security_message_id"

        # LLM client that expects security alert with camera context
        def security_matcher(args: dict) -> bool:
            messages = args.get("messages", [])
            if messages:
                last_msg = messages[-1]
                content = str(last_msg.get("content", ""))
                return (
                    "Script wake_llm call" in content
                    and "motion_detection" in content
                    and "front_door" in content
                    and "camera_snapshot" in content
                    and "captured" in content
                )
            return False

        llm_client = RuleBasedMockLLMClient(
            rules=[
                (
                    security_matcher,
                    LLMOutput(
                        content="🚨 Security Alert: Motion detected at front door! Camera snapshot captured. Reviewing footage now."
                    ),
                )
            ],
            default_response=LLMOutput(content="Security system monitoring."),
        )

        # Processing service with attachment service
        processing_service = ProcessingService(
            llm_client=llm_client,
            tools_provider=tools_provider,
            service_config=ProcessingServiceConfig(
                id="event_handler",
                prompts={"system_prompt": "Security event handler"},
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

        # Task worker
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
            task_worker.run(new_task_event), name=f"SecurityWorker-{test_run_id}"
        )
        await asyncio.sleep(0.1)

        # Step 3: Process motion detection event
        await processor.process_event(
            "home_assistant",
            {
                "entity_id": "binary_sensor.motion_detector",
                "old_state": {"state": "off"},
                "new_state": {"state": "on", "attributes": {"zone": "front_door"}},
            },
        )

        # Signal worker and wait for script execution
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})

        # Wait for LLM callback task
        await asyncio.sleep(0.5)
        new_task_event.set()
        await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

        # Step 4: Verify LLM was woken with security context
        mock_chat_interface.send_message.assert_called_once()
        call_args = mock_chat_interface.send_message.call_args
        sent_text = call_args[1]["text"]

        assert "Security Alert" in sent_text
        assert "Motion detected" in sent_text
        assert "front door" in sent_text
        assert "snapshot captured" in sent_text

        # Step 5: Verify the workflow executed successfully
        # Note: In this test, the mock camera tool creates an attachment
        # but the script doesn't directly access it. In a real implementation,
        # the script would have access to the attachment ID and pass it to wake_llm
        # This test validates the workflow structure and integration

        # Cleanup
        shutdown_event.set()
        new_task_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=2.0)
        except TimeoutError:
            worker_task.cancel()
            await asyncio.sleep(0.1)
