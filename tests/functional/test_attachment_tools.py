"""Functional tests for attachment tools."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from family_assistant.scripting.apis.attachments import ScriptAttachment
from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools import AVAILABLE_FUNCTIONS, TOOLS_DEFINITION
from family_assistant.tools.attachments import attach_to_response_tool
from family_assistant.tools.communication import send_message_to_user_tool
from family_assistant.tools.types import ToolExecutionContext

if TYPE_CHECKING:
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
def mock_attachment_service() -> Mock:
    """Mock attachment service."""
    service = Mock()
    service.get_attachment_metadata = AsyncMock()
    return service


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
        mock_attachment_service: Mock,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """Test successful attachment bundling."""
        # Setup attachment service mock
        mock_attachment_service.get_attachment_metadata.return_value = (
            mock_attachment_metadata
        )

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry and register the attachment in the database
            attachment_registry = AttachmentRegistry(mock_attachment_service)
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
                chat_interface=None,
                attachment_service=mock_attachment_service,
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

    async def test_attach_to_response_invalid_attachment(
        self,
        db_engine: AsyncEngine,
        mock_attachment_service: Mock,
    ) -> None:
        """Test attach_to_response with invalid attachment ID."""

        # Setup attachment service mock to return None (not found)
        mock_attachment_service.get_attachment_metadata.return_value = None

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                chat_interface=None,
                attachment_service=mock_attachment_service,
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

    async def test_attach_to_response_no_attachment_service(
        self,
        db_engine: AsyncEngine,
    ) -> None:
        """Test attach_to_response without attachment service."""

        async with DatabaseContext(db_engine) as db_context:
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="telegram",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                chat_interface=None,
                attachment_service=None,  # No attachment service
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
            assert "AttachmentService not available" in result_data["message"]


class TestSendMessageToUserWithAttachments:
    """Test send_message_to_user tool with attachment support."""

    async def test_send_message_with_valid_attachments(
        self,
        db_engine: AsyncEngine,
        mock_attachment_service: Mock,
        mock_attachment_metadata: MockAttachmentMetadata,
    ) -> None:
        """Test send_message_to_user with valid attachments."""
        # Setup attachment service mock
        mock_attachment_service.get_attachment_metadata.return_value = (
            mock_attachment_metadata
        )

        # Create mock chat interface
        mock_chat_interface = Mock()
        mock_chat_interface.send_message = AsyncMock(return_value="message_123")

        async with DatabaseContext(db_engine) as db_context:
            # Create attachment registry and register the attachment in the database
            attachment_registry = AttachmentRegistry(mock_attachment_service)
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
                chat_interface=mock_chat_interface,
                attachment_service=mock_attachment_service,
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
        mock_attachment_service: Mock,
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
                chat_interface=mock_chat_interface,
                attachment_service=mock_attachment_service,
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
