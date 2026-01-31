"""Unit tests for get_attachment_info tool."""

import json
import tempfile

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.tools.communication import get_attachment_info_tool
from family_assistant.tools.types import ToolExecutionContext


class TestGetAttachmentInfoTool:
    """Test suite for get_attachment_info tool."""

    @pytest.mark.asyncio
    async def test_get_attachment_info_success(self, db_engine: AsyncEngine) -> None:
        """Test successful attachment info retrieval."""
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_id = "test_conversation"
            test_content = b"test content for attachment info"
            filename = "test_file.txt"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register an attachment
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=test_content,
                    mime_type="text/plain",
                    filename=filename,
                    conversation_id=conversation_id,
                    user_id="test_user",
                    description="Test attachment for info retrieval",
                )

                attachment_id = attachment_record.attachment_id

                # Create execution context
                exec_context = ToolExecutionContext(
                    conversation_id=conversation_id,
                    interface_type="test",
                    turn_id="turn_123",
                    user_name="test_user",
                    db_context=db_context,
                    processing_service=None,
                    clock=None,
                    home_assistant_client=None,
                    event_sources=None,
                    attachment_registry=attachment_registry,
                    camera_backend=None,
                )

                # Execute the tool
                result = await get_attachment_info_tool(
                    exec_context=exec_context,
                    attachment_id=attachment_id,
                )

                # Parse the JSON result
                attachment_info = json.loads(result)

                # Verify all expected fields are present
                assert attachment_info["attachment_id"] == attachment_id
                assert attachment_info["mime_type"] == "text/plain"
                assert (
                    attachment_info["description"]
                    == "Test attachment for info retrieval"
                )
                assert attachment_info["size"] == len(test_content)
                assert attachment_info["source_type"] == "user"
                assert attachment_info["source_id"] == "test_user"
                assert attachment_info["conversation_id"] == conversation_id
                assert "created_at" in attachment_info
                assert attachment_info["metadata"]["original_filename"] == filename

    @pytest.mark.asyncio
    async def test_get_attachment_info_not_found(self, db_engine: AsyncEngine) -> None:
        """Test attachment info retrieval for non-existent attachment."""
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            async with DatabaseContext(engine=db_engine) as db_context:
                exec_context = ToolExecutionContext(
                    conversation_id="test_conversation",
                    interface_type="test",
                    turn_id="turn_123",
                    user_name="test_user",
                    db_context=db_context,
                    processing_service=None,
                    clock=None,
                    home_assistant_client=None,
                    event_sources=None,
                    attachment_registry=attachment_registry,
                    camera_backend=None,
                )

                # Try to get info for non-existent attachment
                result = await get_attachment_info_tool(
                    exec_context=exec_context,
                    attachment_id="non-existent-id",
                )

                assert "Error: Attachment with ID non-existent-id not found." in result

    @pytest.mark.asyncio
    async def test_get_attachment_info_cross_conversation_access_allowed(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test that cross-conversation access is allowed if ID is known."""
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_a = "conversation_a"
            conversation_b = "conversation_b"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment in conversation A
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=b"test content",
                    mime_type="text/plain",
                    filename="test.txt",
                    conversation_id=conversation_a,
                    user_id="test_user",
                    description="Attachment in conversation A",
                )

                attachment_id = attachment_record.attachment_id

                # Access from conversation B
                exec_context = ToolExecutionContext(
                    conversation_id=conversation_b,  # Different conversation
                    interface_type="test",
                    turn_id="turn_123",
                    user_name="test_user",
                    db_context=db_context,
                    processing_service=None,
                    clock=None,
                    home_assistant_client=None,
                    event_sources=None,
                    attachment_registry=attachment_registry,
                    camera_backend=None,
                )

                result = await get_attachment_info_tool(
                    exec_context=exec_context,
                    attachment_id=attachment_id,
                )

                # Should succeed
                attachment_info = json.loads(result)
                assert attachment_info["attachment_id"] == attachment_id
                assert attachment_info["conversation_id"] == conversation_a

    @pytest.mark.asyncio
    async def test_get_attachment_info_no_attachment_registry(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test error handling when attachment registry is not available."""
        async with DatabaseContext(engine=db_engine) as db_context:
            exec_context = ToolExecutionContext(
                conversation_id="test_conversation",
                interface_type="test",
                turn_id="turn_123",
                user_name="test_user",
                db_context=db_context,
                processing_service=None,
                clock=None,
                home_assistant_client=None,
                event_sources=None,
                attachment_registry=None,  # No attachment registry
                camera_backend=None,
            )

            result = await get_attachment_info_tool(
                exec_context=exec_context,
                attachment_id="some-attachment-id",
            )

            assert "Error: Attachment registry not available." in result

    @pytest.mark.asyncio
    async def test_get_attachment_info_same_conversation_access_allowed(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test that access within the same conversation is allowed."""
        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_id = "same_conversation"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=b"test content",
                    mime_type="text/plain",
                    filename="test.txt",
                    conversation_id=conversation_id,
                    user_id="test_user",
                    description="Test attachment",
                )

                attachment_id = attachment_record.attachment_id

                # Access from same conversation
                exec_context = ToolExecutionContext(
                    conversation_id=conversation_id,  # Same conversation
                    interface_type="test",
                    turn_id="turn_123",
                    user_name="test_user",
                    db_context=db_context,
                    processing_service=None,
                    clock=None,
                    home_assistant_client=None,
                    event_sources=None,
                    attachment_registry=attachment_registry,
                    camera_backend=None,
                )

                result = await get_attachment_info_tool(
                    exec_context=exec_context,
                    attachment_id=attachment_id,
                )

                # Should succeed and return valid JSON
                attachment_info = json.loads(result)
                assert attachment_info["attachment_id"] == attachment_id
                assert attachment_info["conversation_id"] == conversation_id
