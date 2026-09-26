"""End-to-end tests for attachment manipulation workflows."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from family_assistant.config_models import AppConfig, ToolsConfig
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.events.processor import EventProcessor
from family_assistant.interfaces import ChatInterface
from family_assistant.processing import ProcessingService, ProcessingServiceConfig
from family_assistant.processing.utils import get_file_extension_from_mime_type
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.storage.events import EventActionType, EventSourceType
from family_assistant.task_worker import handle_llm_callback, handle_script_execution
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
from tests.helpers import seed_known_conversation, wait_for_tasks_to_complete
from tests.mocks.mock_llm import (
    LLMOutput,
    MatcherArgs,
    RuleBasedMockLLMClient,
    get_last_message_text,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

    from family_assistant.services.attachment_registry import AttachmentMetadata
    from family_assistant.task_worker import TaskWorker

CONVERSATION_ID = "test_conversation"


def _exec_context(
    db_context: Database,
    attachment_registry: AttachmentRegistry,
    chat_interface: ChatInterface | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        interface_type="test",
        conversation_id=CONVERSATION_ID,
        user_name="test_user",
        turn_id="test_turn",
        db_context=db_context,
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=attachment_registry,
        camera_backend=None,
        chat_interface=chat_interface,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


async def _register_image_output(
    attachment_registry: AttachmentRegistry,
    db_context: Database,
    result: str | ToolResult,
    tool_name: str,
) -> tuple[AttachmentMetadata, bytes]:
    """Persist a tool's single image output the way the processing loop does."""
    assert isinstance(result, ToolResult), result
    assert result.attachments is not None and len(result.attachments) == 1, result
    output = result.attachments[0]
    assert output.content is not None
    assert output.mime_type.startswith("image/")
    metadata = await attachment_registry.store_and_register_tool_attachment(
        file_content=output.content,
        filename=f"{tool_name}_output{get_file_extension_from_mime_type(output.mime_type)}",
        content_type=output.mime_type,
        tool_name=tool_name,
        description=output.description,
        conversation_id=CONVERSATION_ID,
        db_context=db_context,
        taint_state=TurnTaintState.empty(),
    )
    return metadata, output.content


class TestAttachmentWorkflows:
    """Test complete attachment manipulation workflows."""

    @pytest.fixture
    async def attachment_registry(
        self, tmp_path: Path, db_engine: AsyncEngine
    ) -> AttachmentRegistry:
        """Create a real AttachmentRegistry for testing."""
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
        """Camera snapshot -> annotation -> the annotated snapshot is queued for the reply."""
        db_context = Database(engine=db_engine)
        exec_context = _exec_context(db_context, attachment_registry)

        camera_result = await attachment_tools_provider.execute_tool(
            name="mock_camera_snapshot",
            arguments={"entity_id": "camera.front_door"},
            context=exec_context,
        )
        camera_attachment, camera_content = await _register_image_output(
            attachment_registry, db_context, camera_result, "mock_camera_snapshot"
        )

        annotate_result = await attachment_tools_provider.execute_tool(
            name="annotate_image",
            arguments={
                "image_attachment_id": camera_attachment.attachment_id,
                "annotation_text": "Motion detected at 2:30 PM",
                "position": "top-right",
            },
            context=exec_context,
        )
        annotated_attachment, _ = await _register_image_output(
            attachment_registry, db_context, annotate_result, "annotate_image"
        )

        attach_result = await attachment_tools_provider.execute_tool(
            name="attach_to_response",
            arguments={"attachment_ids": [annotated_attachment.attachment_id]},
            context=exec_context,
        )

        queued = json.loads(
            attach_result
            if isinstance(attach_result, str)
            else attach_result.get_text()
        )
        assert queued["status"] == "attachments_queued"
        assert queued["attachment_ids"] == [annotated_attachment.attachment_id]

        queued_content = await attachment_registry.get_attachment_content(
            db_context, queued["attachment_ids"][0], acting_user_id=None
        )
        assert queued_content is not None
        assert queued_content.startswith(camera_content)
        assert b"Motion detected at 2:30 PM" in queued_content[len(camera_content) :]

    async def test_user_image_process_send_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_tools_provider: ToolsProvider,
        attachment_registry: AttachmentRegistry,
    ) -> None:
        """User image -> annotation -> the annotated image is sent to another user."""
        db_context = Database(engine=db_engine)
        target_chat_id = 987654321
        await seed_known_conversation(db_engine, str(target_chat_id))
        mock_chat_interface = AsyncMock(spec=ChatInterface)
        mock_chat_interface.send_message.return_value = "mock_message_id_123"
        exec_context = _exec_context(
            db_context, attachment_registry, chat_interface=mock_chat_interface
        )

        user_image_content = b"fake_user_uploaded_image_data" + b"\x00" * 200
        user_attachment = await attachment_registry.register_user_attachment(
            db_context=db_context,
            content=user_image_content,
            filename="user_photo.jpg",
            mime_type="image/jpeg",
            conversation_id=CONVERSATION_ID,
            description="User uploaded photo",
        )

        process_result = await attachment_tools_provider.execute_tool(
            name="annotate_image",
            arguments={
                "image_attachment_id": user_attachment.attachment_id,
                "annotation_text": "Enhanced by AI assistant",
                "position": "bottom-right",
            },
            context=exec_context,
        )
        processed_attachment, _ = await _register_image_output(
            attachment_registry, db_context, process_result, "annotate_image"
        )

        send_result = await attachment_tools_provider.execute_tool(
            name="send_message_to_user",
            arguments={
                "target_chat_id": target_chat_id,
                "message_content": "Here's your enhanced photo!",
                "attachment_ids": [processed_attachment.attachment_id],
            },
            context=exec_context,
        )

        result_text = (
            send_result.get_text()
            if isinstance(send_result, ToolResult)
            else send_result
        )
        assert "sent successfully" in result_text.lower()
        assert str(target_chat_id) in result_text

        mock_chat_interface.send_message.assert_called_once_with(
            conversation_id=str(target_chat_id),
            text="Here's your enhanced photo!",
            attachment_ids=[processed_attachment.attachment_id],
            on_behalf_of_user_id=None,
            taint_metadata=TurnTaintState.empty().to_metadata(),
        )

        sent_content = await attachment_registry.get_attachment_content(
            db_context, processed_attachment.attachment_id, acting_user_id=None
        )
        assert sent_content is not None
        assert sent_content.startswith(user_image_content)
        assert b"Enhanced by AI assistant" in sent_content[len(user_image_content) :]

    async def test_event_script_camera_wake_llm_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_registry: AttachmentRegistry,
        task_worker_manager: Callable[
            ..., tuple[TaskWorker, asyncio.Event, asyncio.Event]
        ],
    ) -> None:
        """A motion event runs a script whose camera snapshot is handed to the woken LLM."""
        db_ctx = Database(engine=db_engine)
        await db_ctx.events.create_event_listener(
            name=f"Security Camera Alert {uuid.uuid4()}",
            source_id=EventSourceType.home_assistant,
            match_conditions={
                "entity_id": "binary_sensor.motion_detector",
            },
            conversation_id=CONVERSATION_ID,
            interface_type="telegram",
            action_type=EventActionType.script,
            action_config={
                "script_code": """
camera_result = tools_execute("mock_camera_snapshot", entity_id="camera.front_door")
snapshot = camera_result["attachments"][0]
wake_llm({
    "alert_type": "motion_detection",
    "location": "front_door",
    "action_needed": "Review security footage",
    "attachments": [snapshot["id"]],
})
"""
            },
            enabled=True,
        )

        processor = EventProcessor(
            sources={},
            sample_interval_hours=1.0,
            get_db_context_func=lambda: Database(db_engine),
            timezone=ZoneInfo("Australia/Sydney"),
        )
        await processor.start()

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

        mock_chat_interface = AsyncMock(spec=ChatInterface)
        mock_chat_interface.send_message.return_value = "mock_security_message_id"

        wake_messages: list[str] = []

        def security_matcher(args: MatcherArgs) -> bool:
            text = get_last_message_text(args.get("messages", []))
            if (
                "Script wake_llm call" in text
                and "motion_detection" in text
                and "front_door" in text
            ):
                wake_messages.append(text)
                return True
            return False

        llm_client = RuleBasedMockLLMClient(
            rules=[
                (
                    security_matcher,
                    LLMOutput(
                        content="Security Alert: Motion detected at front door! Camera snapshot captured. Reviewing footage now."
                    ),
                )
            ],
            default_response=LLMOutput(content="Security system monitoring."),
        )

        processing_service = ProcessingService(
            llm_client=llm_client,
            tools_provider=tools_provider,
            service_config=ProcessingServiceConfig(
                id="event_handler",
                prompts={"system_prompt": "Security event handler"},
                timezone=ZoneInfo("UTC"),
                max_history_messages=1,
                history_max_age_hours=1,
                tools_config=ToolsConfig(),
                delegation_security_level=DelegationSecurityLevel.BLOCKED,
            ),
            app_config=AppConfig(),
            context_providers=[],
            server_url=None,
            attachment_registry=attachment_registry,
        )

        worker, _, _ = task_worker_manager(processing_service, mock_chat_interface)
        worker.register_task_handler("script_execution", handle_script_execution)
        worker.register_task_handler("llm_callback", handle_llm_callback)

        await processor.process_event(
            "home_assistant",
            {
                "entity_id": "binary_sensor.motion_detector",
                "old_state": {"state": "off"},
                "new_state": {"state": "on", "attributes": {"zone": "front_door"}},
            },
        )

        # The script task enqueues the llm_callback before it completes.
        await wait_for_tasks_to_complete(db_engine, task_types={"script_execution"})
        await wait_for_tasks_to_complete(db_engine, task_types={"llm_callback"})

        snapshots = await attachment_registry.list_attachments(
            db_ctx,
            acting_user_id=None,
            conversation_id=CONVERSATION_ID,
            source_type="tool",
        )
        assert [snapshot.source_id for snapshot in snapshots] == [
            "mock_camera_snapshot"
        ]
        assert snapshots[0].mime_type == "image/png"

        assert wake_messages
        for text in wake_messages:
            assert "<attachment_metadata>" in text
            handed_over = text.split("<attachment_metadata>", 1)[1]
            assert snapshots[0].attachment_id in handed_over

        mock_chat_interface.send_message.assert_called_once()
        sent_text = mock_chat_interface.send_message.call_args.kwargs["text"]
        assert "Security Alert" in sent_text
        assert "snapshot captured" in sent_text
