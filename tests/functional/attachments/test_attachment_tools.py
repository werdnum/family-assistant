"""Functional tests for attachment tools."""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import aiofiles
import pytest
from PIL import Image

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.security.taint import TurnTaintState
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database
from family_assistant.tools import LOCAL_TOOL_REGISTRATIONS, LocalToolsProvider
from family_assistant.tools.attachments import attach_to_response_tool
from family_assistant.tools.communication import send_message_to_user_tool
from family_assistant.tools.image_tools import highlight_image_tool
from family_assistant.tools.types import ToolExecutionContext
from tests.helpers import seed_known_conversation

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
        db_engine: AsyncEngine,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """A registered attachment ID is resolved and queued for the response."""
        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await LocalToolsProvider(
            registrations=LOCAL_TOOL_REGISTRATIONS
        ).execute_tool(
            "attach_to_response",
            {"attachment_ids": [mock_attachment_metadata.id]},
            context=exec_context,
        )

        assert isinstance(result, str)
        result_data = json.loads(result)
        assert result_data["status"] == "attachments_queued"
        assert result_data["attachment_ids"] == [mock_attachment_metadata.id]
        assert result_data["count"] == 1
        assert "Successfully attached 1 attachment" in result_data["message"]
        assert "No further action needed" in result_data["message"]

    async def test_attach_to_response_rejects_unknown_attachment_id(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """An ID the registry does not know is rejected rather than queued."""
        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )
        unknown_id = str(uuid.uuid4())

        result = await LocalToolsProvider(
            registrations=LOCAL_TOOL_REGISTRATIONS
        ).execute_tool(
            "attach_to_response",
            {"attachment_ids": [unknown_id]},
            context=exec_context,
        )

        assert isinstance(result, str)
        assert result.startswith("Error:")
        assert f"Attachment '{unknown_id}' not found or access denied" in result

    async def test_attach_to_response_no_attachment_registry(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Without a registry the tool reports an error instead of queueing IDs."""
        db_context = Database(db_engine)
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
            attachment_registry=None,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await attach_to_response_tool(
            exec_context=exec_context,
            attachment_ids=[str(uuid.uuid4())],
        )

        result_data = json.loads(result)
        assert result_data["status"] == "error"
        assert "AttachmentRegistry not available" in result_data["message"]


class TestSendMessageToUserWithAttachments:
    """Test send_message_to_user tool with attachment support."""

    async def test_send_message_with_valid_attachments(
        self,
        db_engine: AsyncEngine,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """Test send_message_to_user with valid attachments."""
        # Create mock chat interface
        mock_chat_interface = Mock()
        mock_chat_interface.send_message = AsyncMock(return_value="message_123")

        db_context = Database(db_engine)
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
        await seed_known_conversation(db_engine, "456789")

        exec_context = ToolExecutionContext(
            conversation_id="test_conversation",
            interface_type="telegram",
            turn_id="turn_123",
            user_name="test_user",
            user_id="test_user",
            db_context=db_context,
            processing_service=None,
            clock=None,
            home_assistant_client=None,
            event_sources=None,
            chat_interface=mock_chat_interface,
            attachment_registry=attachment_registry,
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await send_message_to_user_tool(
            exec_context=exec_context,
            target_chat_id="456789",
            message_content="Here's your document",
            attachment_ids=[mock_attachment_metadata.id],
        )

        # Verify message was sent with attachments, delivered on behalf of
        # the acting user so owner-scoped attachments survive delivery.
        mock_chat_interface.send_message.assert_called_once_with(
            conversation_id="456789",
            text="Here's your document",
            attachment_ids=[mock_attachment_metadata.id],
            on_behalf_of_user_id="test_user",
            taint_metadata=TurnTaintState.empty().to_metadata(),
        )

        assert "Message sent successfully" in result
        assert "with 1 attachment(s)" in result

    async def test_send_message_without_attachments(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test send_message_to_user without attachments."""

        # Create mock chat interface
        mock_chat_interface = Mock()
        mock_chat_interface.send_message = AsyncMock(return_value="message_123")

        db_context = Database(db_engine)
        await seed_known_conversation(db_engine, "456789")
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        result = await send_message_to_user_tool(
            exec_context=exec_context,
            target_chat_id="456789",
            message_content="Just a message",
        )

        # Verify message was sent without attachments
        mock_chat_interface.send_message.assert_called_once_with(
            conversation_id="456789",
            text="Just a message",
            attachment_ids=None,
            on_behalf_of_user_id=None,
            taint_metadata=TurnTaintState.empty().to_metadata(),
        )

        assert "Message sent successfully" in result
        assert "attachment" not in result


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

        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        # Get attachment metadata
        attachment_metadata = await attachment_registry.get_attachment(
            db_context, image_id, acting_user_id=None
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

        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        attachment_metadata = await attachment_registry.get_attachment(
            db_context, file_id, acting_user_id=None
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

        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        attachment_metadata = await attachment_registry.get_attachment(
            db_context, image_id, acting_user_id=None
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

        db_context = Database(db_engine)
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
            camera_backend=None,
            timezone=ZoneInfo("UTC"),
            credential_resolvers=None,
            api_backend=None,
        )

        attachment_metadata = await attachment_registry.get_attachment(
            db_context, image_id, acting_user_id=None
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


class TestScriptAttachmentSyncContent:
    """The synchronous content API that scripts call."""

    def test_get_content_uses_the_handle_without_entering_it(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """The getter's return value is used directly, not entered.

        Database is deliberately not an async context manager, so entering it
        here fails every script that reads an attachment through the
        synchronous API -- whatever the registry would have returned.

        The registry is a stub because that is the whole point: the failure
        this pins happens before any content lookup, and the real registry
        would need a database reachable from the fresh event loop that
        ``get_content`` spins up.
        """
        seen: list[object] = []

        class _StubRegistry:
            async def get_attachment_content(
                self,
                db_context: object,
                attachment_id: str,
                *,
                acting_user_id: str | None,
            ) -> bytes:
                _ = attachment_id, acting_user_id
                seen.append(db_context)
                return b"attachment bytes"

        metadata = Mock()
        metadata.attachment_id = str(uuid.uuid4())

        script_attachment = ScriptAttachment(
            metadata=metadata,
            registry=cast("AttachmentRegistry", _StubRegistry()),
            db_context_getter=lambda: Database(db_engine),
        )

        assert script_attachment.get_content() == b"attachment bytes"
        assert isinstance(seen[0], Database)
