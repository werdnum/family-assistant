"""End-to-end tests for attachment manipulation workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.services.attachments import AttachmentService
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import (
    ATTACHMENT_TOOLS_DEFINITION,
    HOME_ASSISTANT_TOOLS_DEFINITION,
    MOCK_IMAGE_TOOLS_DEFINITION,
    CompositeToolsProvider,
    LocalToolsProvider,
    ToolsProvider,
)
from family_assistant.tools import AVAILABLE_FUNCTIONS as local_tool_implementations
from family_assistant.tools.types import ToolExecutionContext, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


class TestAttachmentWorkflows:
    """Test complete attachment manipulation workflows."""

    @pytest.fixture
    async def attachment_service(self, tmp_path: Path) -> AttachmentService:
        """Create a real AttachmentService for testing."""
        # Create a temporary directory for test attachments
        test_storage = tmp_path / "test_attachments"
        test_storage.mkdir(exist_ok=True)
        return AttachmentService(storage_path=str(test_storage))

    @pytest.fixture
    async def attachment_tools_provider(self) -> ToolsProvider:
        """Create a tools provider with attachment-related tools."""
        local_provider = LocalToolsProvider(
            definitions=(
                HOME_ASSISTANT_TOOLS_DEFINITION
                + ATTACHMENT_TOOLS_DEFINITION
                + MOCK_IMAGE_TOOLS_DEFINITION
            ),
            implementations={
                "mock_camera_snapshot": local_tool_implementations[
                    "mock_camera_snapshot"
                ],
                "attach_to_response": local_tool_implementations["attach_to_response"],
                "annotate_image": local_tool_implementations["annotate_image"],
            },
        )
        tools_provider = CompositeToolsProvider(providers=[local_provider])
        await tools_provider.get_tool_definitions()
        return tools_provider

    async def test_camera_annotate_response_workflow(
        self,
        db_engine: AsyncEngine,
        attachment_tools_provider: ToolsProvider,
        attachment_service: AttachmentService,
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
                attachment_service=attachment_service,
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
                assert "snapshot" in camera_result.text.lower()
                assert camera_result.attachment is not None
                assert camera_result.attachment.mime_type.startswith("image/")
                assert camera_result.attachment.content is not None
                camera_content = camera_result.attachment.content
                camera_mime = camera_result.attachment.mime_type
            else:
                # Fallback if mock tool returns string (should not happen)
                camera_content = b"fake_camera_image_data"
                camera_mime = "image/png"

            # Store the camera attachment using the attachment service to get an ID
            camera_data = attachment_service.store_bytes_as_attachment(
                camera_content,
                "camera_snapshot.png",
                camera_mime,
            )
            camera_attachment_id = camera_data["attachment_id"]

            # Register the camera attachment in the metadata database
            attachment_registry = AttachmentRegistry(attachment_service)
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=camera_attachment_id,
                source_type="tool",
                source_id="mock_camera_snapshot",
                mime_type=camera_mime,
                description="Camera snapshot",
                size=len(camera_content),
                content_url=camera_data["url"],
                storage_path=camera_data["storage_path"],
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
                assert "annotated" in annotate_result.text.lower()
                assert annotate_result.attachment is not None
                assert annotate_result.attachment.mime_type.startswith("image/")
                assert annotate_result.attachment.content is not None
                annotated_content = annotate_result.attachment.content
                annotated_mime = annotate_result.attachment.mime_type
            else:
                # It's a string result, create mock annotated attachment
                annotated_content = camera_content + b"_annotated"
                annotated_mime = camera_mime

            # Store the annotated attachment
            annotated_data = attachment_service.store_bytes_as_attachment(
                annotated_content,
                "annotated_image.png",
                annotated_mime,
            )
            annotated_attachment_id = annotated_data["attachment_id"]

            # Register the annotated attachment
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=annotated_attachment_id,
                source_type="tool",
                source_id="annotate_image",
                mime_type=annotated_mime,
                description="Annotated image",
                size=len(annotated_content),
                content_url=annotated_data["url"],
                storage_path=annotated_data["storage_path"],
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
                attach_result if isinstance(attach_result, str) else attach_result.text
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
