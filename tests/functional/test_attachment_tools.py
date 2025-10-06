"""Functional tests for attachment tools."""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import aiofiles
import pytest
from PIL import Image

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.attachments import attach_to_response_tool
from family_assistant.tools.communication import send_message_to_user_tool
from family_assistant.tools.image_tools import highlight_image_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class MockAttachmentMetadata:
    """Mock attachment metadata for testing."""

    id: str
    conversation_id: str
    original_filename: str
    mime_type: str
    file_size_bytes: int
    description: str
    storage_path: str


@pytest.fixture
def mock_attachment_registry() -> Mock:
    """Mock attachment registry."""
    registry = Mock()
    registry.get_attachment_metadata = AsyncMock()
    return registry


@pytest.fixture
def mock_attachment_metadata() -> MockAttachmentMetadata:
    """Create mock attachment metadata."""
    return MockAttachmentMetadata(
        id=str(uuid.uuid4()),
        conversation_id="test_conversation",
        original_filename="test.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        description="Test PDF attachment",
        storage_path="/tmp/test.pdf",
    )


class TestAttachToResponseTool:
    """Test the attach_to_response tool functionality."""

    async def test_attach_to_response_success(
        self,
        db_engine: AsyncEngine,  # noqa: ANN001
        mock_attachment_registry: Mock,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """Test successful attachment bundling."""
        # Setup attachment service mock
        mock_attachment_registry.get_attachment_metadata.return_value = (
            mock_attachment_metadata
        )

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry and register the attachment in the database
            attachment_registry = AttachmentRegistry(
                storage_path="/tmp/test_attachments", db_engine=db_engine, config=None
            )
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=mock_attachment_metadata.id,
                source_type="user",
                source_id="test_user",
                mime_type=mock_attachment_metadata.mime_type,
                description=mock_attachment_metadata.description,
                size=mock_attachment_metadata.file_size_bytes,
                storage_path=mock_attachment_metadata.storage_path,
                conversation_id=mock_attachment_metadata.conversation_id,
            )

            # Create execution context
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            # Create ScriptAttachment object for the tool
            # Get the attachment metadata from registry
            attachment_metadata = await attachment_registry.get_attachment(
                db_context, mock_attachment_metadata.id
            )
            assert attachment_metadata is not None

            # Create ScriptAttachment object
            script_attachment = ScriptAttachment(
                metadata=attachment_metadata,
                registry=attachment_registry,
                db_context_getter=lambda: DatabaseContext(db_engine),
            )

            # Execute tool
            result = await attach_to_response_tool(
                exec_context=exec_context,
                attachment_ids=[script_attachment],
            )

            # Verify result
            result_data = json.loads(result)
            assert result_data["status"] == "attachments_queued"
            assert result_data["attachment_ids"] == [mock_attachment_metadata.id]
            assert result_data["count"] == 1
            assert "Successfully attached 1 attachment" in result_data["message"]
            assert "No further action needed" in result_data["message"]

    async def test_attach_to_response_invalid_attachment(
        self,
        db_engine: AsyncEngine,
        mock_attachment_registry: Mock,
    ) -> None:
        """Test attach_to_response with invalid attachment ID."""

        # Setup attachment service mock to return None (not found)
        mock_attachment_registry.get_attachment_metadata.return_value = None

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry for this test
            attachment_registry = AttachmentRegistry(
                storage_path="/tmp/test_attachments", db_engine=db_engine, config=None
            )

            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            # Create a mock ScriptAttachment that will fail validation
            invalid_attachment = Mock(spec=ScriptAttachment)
            invalid_attachment.get_id.side_effect = Exception("Invalid attachment")

            result = await attach_to_response_tool(
                exec_context=exec_context,
                attachment_ids=[invalid_attachment],
            )

            result_data = json.loads(result)
            assert result_data["status"] == "error"
            assert "No valid attachments found" in result_data["message"]

    async def test_attach_to_response_no_attachment_registry(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test attach_to_response without attachment registry."""

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=None,  # No attachment registry
            )

            # Create a mock ScriptAttachment for this test
            mock_attachment = Mock(spec=ScriptAttachment)
            mock_attachment.get_id.return_value = "some_id"

            result = await attach_to_response_tool(
                exec_context=exec_context,
                attachment_ids=[mock_attachment],
            )

            result_data = json.loads(result)
            assert result_data["status"] == "error"
            assert "AttachmentRegistry not available" in result_data["message"]


class TestSendMessageToUserWithAttachments:
    """Test send_message_to_user tool with attachment support."""

    async def test_send_message_with_valid_attachments(
        self,
        db_engine: AsyncEngine,
        mock_attachment_registry: Mock,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """Test send_message_to_user with valid attachments."""
        # Setup attachment service mock
        mock_attachment_registry.get_attachment_metadata.return_value = (
            mock_attachment_metadata
        )

        # Create mock chat interface
        mock_chat_interface = Mock()
        mock_chat_interface.send_message = AsyncMock(return_value="message_123")

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry and register the attachment in the database
            attachment_registry = AttachmentRegistry(
                storage_path="/tmp/test_attachments", db_engine=db_engine, config=None
            )
            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=mock_attachment_metadata.id,
                source_type="user",
                source_id="test_user",
                mime_type=mock_attachment_metadata.mime_type,
                description=mock_attachment_metadata.description,
                size=mock_attachment_metadata.file_size_bytes,
                storage_path=mock_attachment_metadata.storage_path,
                conversation_id=mock_attachment_metadata.conversation_id,
            )

            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=mock_chat_interface,
                attachment_registry=attachment_registry,
            )

            result = await send_message_to_user_tool(
                exec_context=exec_context,
                target_chat_id=456789,
                message_content="Here's your document",
                attachment_ids=[mock_attachment_metadata.id],
            )

            # Verify message was sent with attachments
            mock_chat_interface.send_message.assert_called_once_with(
                conversation_id="456789",
                text="Here's your document",
                attachment_ids=[mock_attachment_metadata.id],
            )

            assert "Message sent successfully" in result
            assert "with 1 attachment(s)" in result

    async def test_send_message_without_attachments(
        self,
        db_engine: AsyncEngine,
        mock_attachment_registry: Mock,
    ) -> None:
        """Test send_message_to_user without attachments."""

        # Create mock chat interface
        mock_chat_interface = Mock()
        mock_chat_interface.send_message = AsyncMock(return_value="message_123")

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=mock_chat_interface,
                attachment_registry=None,
            )

            result = await send_message_to_user_tool(
                exec_context=exec_context,
                target_chat_id=456789,
                message_content="Just a message",
            )

            # Verify message was sent without attachments
            mock_chat_interface.send_message.assert_called_once_with(
                conversation_id="456789",
                text="Just a message",
                attachment_ids=None,
            )

            assert "Message sent successfully" in result
            assert "attachment" not in result


class TestToolRegistration:
    """Test that attachment tools are properly registered."""

    def test_attach_to_response_tool_registered(self) -> None:
        """Test that attach_to_response tool is registered."""

        # Check function is registered
        assert "attach_to_response" in AVAILABLE_FUNCTIONS

        # Check tool definition is included
        tool_names = [
            tool.get("function", {}).get("name")
            for tool in TOOLS_DEFINITION
            if tool.get("type") == "function"
        ]
        assert "attach_to_response" in tool_names

    def test_attach_to_response_tool_definition(self) -> None:
        """Test that attach_to_response tool has correct definition."""

        # Find the tool definition
        attach_tool = None
        for tool in TOOLS_DEFINITION:
            if (
                tool.get("type") == "function"
                and tool.get("function", {}).get("name") == "attach_to_response"
            ):
                attach_tool = tool
                break

        assert attach_tool is not None
        function_def = attach_tool["function"]

        # Verify key aspects of the definition
        assert function_def["name"] == "attach_to_response"
        assert (
            "attach files/images to your current response"
            in function_def["description"].lower()
        )

        # Verify parameters
        params = function_def["parameters"]
        assert params["type"] == "object"
        assert "attachment_ids" in params["properties"]
        assert params["properties"]["attachment_ids"]["type"] == "array"
        assert "attachment_ids" in params["required"]


def create_test_image(
    width: int = 800, height: int = 600, color: str = "white"
) -> bytes:
    """Create a simple test image with specified dimensions and color.

    Args:
        width: Image width in pixels
        height: Image height in pixels
        color: Background color (PIL color name or hex)

    Returns:
        PNG image bytes
    """
    img = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


class TestHighlightImageTool:
    """Test the highlight_image tool functionality."""

    async def test_highlight_image_success(
        self,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Test successful image highlighting with bounding boxes."""
        # Create test image
        test_image_bytes = create_test_image(800, 600, "white")

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry
            attachment_registry = AttachmentRegistry(
                storage_path=str(tmp_path), db_engine=db_engine, config=None
            )

            # Register the test image
            image_id = str(uuid.uuid4())
            # AttachmentRegistry uses hash-prefixed directories
            hash_prefix = image_id[:2]
            storage_dir = tmp_path / hash_prefix
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = str(storage_dir / f"{image_id}.png")
            async with aiofiles.open(storage_path, "wb") as f:
                await f.write(test_image_bytes)

            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=image_id,
                source_type="user",
                source_id="test_user",
                mime_type="image/png",
                description="Test image",
                size=len(test_image_bytes),
                storage_path=storage_path,
                conversation_id="test_conversation",
            )

            # Create execution context
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="web",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            # Get attachment metadata
            attachment_metadata = await attachment_registry.get_attachment(
                db_context, image_id
            )
            assert attachment_metadata is not None

            # Create ScriptAttachment
            script_attachment = ScriptAttachment(
                metadata=attachment_metadata,
                registry=attachment_registry,
                db_context_getter=lambda: db_context,
            )

            # Define regions with bounding box format (normalized [0, 1000] coordinates)
            # For an 800x600 image, these will be scaled to pixel coordinates
            # Bounding box format is: y_min, x_min, y_max, x_max
            regions = [
                {
                    "box": [200, 100, 600, 400],
                    "label": "test_region_1",
                    "color": "red",
                },
                {
                    "box": [300, 500, 700, 800],
                    "label": "test_region_2",
                    "color": "blue",
                },
            ]

            # Execute tool
            result = await highlight_image_tool(
                exec_context=exec_context,
                image_attachment_id=script_attachment,
                regions=regions,
            )

            # Verify result
            assert result.text is not None
            assert "Successfully highlighted" in result.text
            assert "2 regions" in result.text
            assert "test_region_1" in result.text
            assert "test_region_2" in result.text

            # Verify attachment was created
            assert result.attachments and len(result.attachments) > 0
            assert result.attachments[0].mime_type == "image/png"
            assert result.attachments[0].content is not None
            assert len(result.attachments[0].content) > 0

            # Verify the highlighted image is different from the original
            assert result.attachments[0].content != test_image_bytes

            # Verify we can load the highlighted image
            highlighted_img = Image.open(io.BytesIO(result.attachments[0].content))
            assert highlighted_img.size == (800, 600)

    async def test_highlight_image_invalid_attachment(
        self,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Test highlight_image with non-image attachment."""
        # Create a text file instead of an image
        test_text = b"This is not an image"

        async with DatabaseContext(db_engine) as db_context:
            attachment_registry = AttachmentRegistry(
                storage_path=str(tmp_path), db_engine=db_engine, config=None
            )

            # Register a text file
            file_id = str(uuid.uuid4())
            # AttachmentRegistry uses hash-prefixed directories
            hash_prefix = file_id[:2]
            storage_dir = tmp_path / hash_prefix
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = str(storage_dir / f"{file_id}.txt")
            async with aiofiles.open(storage_path, "wb") as f:
                await f.write(test_text)

            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=file_id,
                source_type="user",
                source_id="test_user",
                mime_type="text/plain",
                description="Test text file",
                size=len(test_text),
                storage_path=storage_path,
                conversation_id="test_conversation",
            )

            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="web",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            attachment_metadata = await attachment_registry.get_attachment(
                db_context, file_id
            )
            assert attachment_metadata is not None

            script_attachment = ScriptAttachment(
                metadata=attachment_metadata,
                registry=attachment_registry,
                db_context_getter=lambda: db_context,
            )

            regions = [
                {
                    "box": [100, 100, 200, 200],
                    "label": "test",
                },
            ]

            # Execute tool - should fail gracefully
            result = await highlight_image_tool(
                exec_context=exec_context,
                image_attachment_id=script_attachment,
                regions=regions,
            )

            # Verify error result
            assert result.text is not None
            assert "Error" in result.text
            assert "not an image" in result.text
            assert not result.attachments or len(result.attachments) == 0

    async def test_highlight_image_invalid_regions(
        self,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Test highlight_image with invalid region data."""
        test_image_bytes = create_test_image(800, 600, "white")

        async with DatabaseContext(db_engine) as db_context:
            attachment_registry = AttachmentRegistry(
                storage_path=str(tmp_path), db_engine=db_engine, config=None
            )

            image_id = str(uuid.uuid4())
            # AttachmentRegistry uses hash-prefixed directories
            hash_prefix = image_id[:2]
            storage_dir = tmp_path / hash_prefix
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = str(storage_dir / f"{image_id}.png")
            async with aiofiles.open(storage_path, "wb") as f:
                await f.write(test_image_bytes)

            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=image_id,
                source_type="user",
                source_id="test_user",
                mime_type="image/png",
                description="Test image",
                size=len(test_image_bytes),
                storage_path=storage_path,
                conversation_id="test_conversation",
            )

            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="web",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            attachment_metadata = await attachment_registry.get_attachment(
                db_context, image_id
            )
            assert attachment_metadata is not None

            script_attachment = ScriptAttachment(
                metadata=attachment_metadata,
                registry=attachment_registry,
                db_context_getter=lambda: db_context,
            )

            # Region with missing required field (box)
            regions = [
                {
                    "box": [100, 100, 200, 200],
                    "label": "valid_region",
                },
                {
                    # Missing box entirely - should trigger validation error
                    "label": "invalid_region",
                },
            ]

            # Execute tool - should fail fast on invalid region
            result = await highlight_image_tool(
                exec_context=exec_context,
                image_attachment_id=script_attachment,
                regions=regions,
            )

            # Should return error for invalid region
            assert result.text is not None
            assert result.text.startswith("Error:")
            assert "Invalid region 1" in result.text
            assert "missing required field" in result.text
            assert not result.attachments or len(result.attachments) == 0

    async def test_highlight_image_invalid_shape(
        self,
        db_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """Test highlight_image with invalid shape."""
        test_image_bytes = create_test_image(800, 600, "white")

        async with DatabaseContext(db_engine) as db_context:
            attachment_registry = AttachmentRegistry(
                storage_path=str(tmp_path), db_engine=db_engine, config=None
            )

            image_id = str(uuid.uuid4())
            # AttachmentRegistry uses hash-prefixed directories
            hash_prefix = image_id[:2]
            storage_dir = tmp_path / hash_prefix
            storage_dir.mkdir(parents=True, exist_ok=True)
            storage_path = str(storage_dir / f"{image_id}.png")
            async with aiofiles.open(storage_path, "wb") as f:
                await f.write(test_image_bytes)

            await attachment_registry.register_attachment(
                db_context=db_context,
                attachment_id=image_id,
                source_type="user",
                source_id="test_user",
                mime_type="image/png",
                description="Test image",
                size=len(test_image_bytes),
                storage_path=storage_path,
                conversation_id="test_conversation",
            )

            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="web",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                chat_interface=None,
                attachment_registry=attachment_registry,
            )

            attachment_metadata = await attachment_registry.get_attachment(
                db_context, image_id
            )
            assert attachment_metadata is not None

            script_attachment = ScriptAttachment(
                metadata=attachment_metadata,
                registry=attachment_registry,
                db_context_getter=lambda: db_context,
            )

            # Region with invalid shape
            regions = [
                {
                    "box": [100, 100, 200, 200],
                    "label": "test",
                    "shape": "triangle",  # Invalid shape
                },
            ]

            # Execute tool - should fail fast on invalid shape
            result = await highlight_image_tool(
                exec_context=exec_context,
                image_attachment_id=script_attachment,
                regions=regions,
            )

            # Should return error for invalid shape
            assert result.text is not None
            assert result.text.startswith("Error:")
            assert "Invalid shape 'triangle'" in result.text
            assert "Must be 'rectangle' or 'circle'" in result.text
            assert not result.attachments or len(result.attachments) == 0
