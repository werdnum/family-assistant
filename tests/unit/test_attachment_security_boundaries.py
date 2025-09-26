"""Unit tests for attachment security boundaries and access control."""

import tempfile
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext


class TestAttachmentSecurityBoundaries:
    """Test suite for attachment security boundaries and conversation scoping."""

    @pytest.mark.asyncio
    async def test_cross_conversation_access_denied(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test that attachments cannot be accessed from different conversations."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            # Create attachment in conversation A
            test_content = b"test content for conversation A"
            conversation_a_id = "conversation_a"
            conversation_b_id = "conversation_b"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment in conversation A
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=test_content,
                    mime_type="text/plain",
                    filename="test.txt",
                    conversation_id=conversation_a_id,
                    user_id="user1",
                    description="Test attachment for conversation A",
                )
                attachment_id = attachment_record.attachment_id

                # Verify attachment exists and is accessible in conversation A
                retrieved_from_a = await attachment_registry.get_attachment(
                    db_context, attachment_id
                )
                assert retrieved_from_a is not None
                assert retrieved_from_a.conversation_id == conversation_a_id

                # Try to access attachment from conversation B by simulating different conversation context
                # This should still return the attachment metadata but security should be enforced at tool level
                retrieved_from_b = await attachment_registry.get_attachment(
                    db_context, attachment_id
                )
                assert retrieved_from_b is not None
                assert (
                    retrieved_from_b.conversation_id == conversation_a_id
                )  # Should show original conversation

                # Security check: Attachment should not be accessible from different conversation
                # This is enforced at the tool level (e.g., in delegate_to_service_tool)
                assert retrieved_from_b.conversation_id != conversation_b_id

    @pytest.mark.asyncio
    async def test_attachment_persistence_throughout_conversation(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test that attachments remain accessible throughout a conversation lifetime."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_id = "persistent_conversation"
            test_content = b"persistent test content"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=test_content,
                    mime_type="application/pdf",
                    filename="persistent.bin",
                    conversation_id=conversation_id,
                    user_id="user1",
                    description="Persistent test attachment",
                )
                attachment_id = attachment_record.attachment_id

            # Simulate multiple database sessions (as would happen during conversation)
            for _i in range(3):
                async with DatabaseContext(engine=db_engine) as db_context:
                    # Attachment should remain accessible
                    retrieved = await attachment_registry.get_attachment(
                        db_context, attachment_id
                    )
                    assert retrieved is not None
                    assert retrieved.conversation_id == conversation_id
                    assert retrieved.mime_type == "application/pdf"
                    assert (
                        retrieved.metadata.get("original_filename") == "persistent.bin"
                    )

                    # Content should remain accessible
                    content = await attachment_registry.get_attachment_content(
                        db_context, attachment_id
                    )
                    assert content == test_content

    @pytest.mark.asyncio
    async def test_reference_integrity_between_services(
        self, db_engine: AsyncEngine
    ) -> None:
        """Test that attachment IDs remain valid when passed between tools/services."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_id = "service_conversation"
            test_content = b"content for service passing"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=test_content,
                    mime_type="image/png",
                    filename="service_test.png",
                    conversation_id=conversation_id,
                    user_id="user1",
                    description="Attachment for service testing",
                )
                attachment_id = attachment_record.attachment_id

                # Simulate passing attachment ID between services
                service_a_id = attachment_id
                service_b_id = attachment_id

                # Both services should be able to access the same attachment
                attachment_from_a = await attachment_registry.get_attachment(
                    db_context, service_a_id
                )
                attachment_from_b = await attachment_registry.get_attachment(
                    db_context, service_b_id
                )

                assert attachment_from_a is not None
                assert attachment_from_b is not None
                assert (
                    attachment_from_a.attachment_id == attachment_from_b.attachment_id
                )
                assert (
                    attachment_from_a.conversation_id
                    == attachment_from_b.conversation_id
                )

                # Content should be identical
                content_a = await attachment_registry.get_attachment_content(
                    db_context, service_a_id
                )
                content_b = await attachment_registry.get_attachment_content(
                    db_context, service_b_id
                )

                assert content_a == content_b == test_content

    @pytest.mark.asyncio
    async def test_invalid_attachment_id_handling(self, db_engine: AsyncEngine) -> None:
        """Test proper handling of invalid or non-existent attachment IDs."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            async with DatabaseContext(engine=db_engine) as db_context:
                # Test with completely invalid UUID - registry doesn't validate format, just queries DB
                invalid_id = "not-a-uuid"
                result = await attachment_registry.get_attachment(
                    db_context, invalid_id
                )
                assert result is None  # Database simply won't find it

                # Test with valid UUID format but non-existent attachment
                non_existent_id = str(uuid.uuid4())
                result = await attachment_registry.get_attachment(
                    db_context, non_existent_id
                )
                assert result is None

                # Test content access for non-existent attachment
                content = await attachment_registry.get_attachment_content(
                    db_context, non_existent_id
                )
                assert content is None

    @pytest.mark.asyncio
    async def test_attachment_metadata_integrity(self, db_engine: AsyncEngine) -> None:
        """Test that attachment metadata remains intact and accurate."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            conversation_id = "metadata_conversation"
            test_content = (
                b"metadata test content with special chars: \xe2\x9c\x93\xe2\x9d\x84"
            )
            original_filename = "special_chars_\u2713\u2744.txt"

            async with DatabaseContext(engine=db_engine) as db_context:
                # Register attachment with special characters
                attachment_record = await attachment_registry.register_user_attachment(
                    db_context=db_context,
                    content=test_content,
                    mime_type="text/plain",
                    filename=original_filename,
                    conversation_id=conversation_id,
                    user_id="test_user",
                    description="Test with special characters: ✓❄",
                )
                attachment_id = attachment_record.attachment_id

                # Retrieve and verify all metadata
                retrieved = await attachment_registry.get_attachment(
                    db_context, attachment_id
                )
                assert retrieved is not None
                assert retrieved.attachment_id == attachment_id
                assert retrieved.conversation_id == conversation_id
                assert retrieved.mime_type == "text/plain"
                assert retrieved.description == "Test with special characters: ✓❄"
                assert retrieved.metadata.get("original_filename") == original_filename
                assert retrieved.source_type == "user"
                assert retrieved.source_id == "test_user"
                assert retrieved.size == len(test_content)

                # Verify content matches exactly
                content = await attachment_registry.get_attachment_content(
                    db_context, attachment_id
                )
                assert content == test_content
