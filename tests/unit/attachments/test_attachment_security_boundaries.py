"""Unit tests for attachment security boundaries and access control."""

import tempfile
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.database import Database


class TestAttachmentSecurityBoundaries:
    """Test suite for attachment security boundaries and conversation scoping."""

    @pytest.mark.asyncio
    async def test_attachment_survives_new_database_contexts(
        self, db_engine: AsyncEngine
    ) -> None:
        """An uploaded attachment remains readable by a later turn."""
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )
            record = await registry.register_user_attachment(
                db_context=Database(engine=db_engine),
                content=b"persistent test content",
                mime_type="application/pdf",
                filename="persistent.bin",
                conversation_id="persistent_conversation",
                user_id="user1",
                description="Persistent test attachment",
            )

            later_context = Database(engine=db_engine)
            retrieved = await registry.get_attachment(
                later_context, record.attachment_id, acting_user_id=None
            )
            content = await registry.get_attachment_content(
                later_context, record.attachment_id, acting_user_id=None
            )

            assert retrieved is not None
            assert retrieved.conversation_id == "persistent_conversation"
            assert retrieved.mime_type == "application/pdf"
            assert retrieved.metadata["original_filename"] == "persistent.bin"
            assert content == b"persistent test content"

    @pytest.mark.asyncio
    async def test_invalid_attachment_id_handling(self, db_engine: AsyncEngine) -> None:
        """Test proper handling of invalid or non-existent attachment IDs."""

        with tempfile.TemporaryDirectory() as temp_dir:
            attachment_registry = AttachmentRegistry(
                storage_path=temp_dir, db_engine=db_engine, config=None
            )

            db_context = Database(engine=db_engine)
            # Test with completely invalid UUID - registry doesn't validate format, just queries DB
            invalid_id = "not-a-uuid"
            result = await attachment_registry.get_attachment(
                db_context, invalid_id, acting_user_id=None
            )
            assert result is None  # Database simply won't find it

            # Test with valid UUID format but non-existent attachment
            non_existent_id = str(uuid.uuid4())
            result = await attachment_registry.get_attachment(
                db_context, non_existent_id, acting_user_id=None
            )
            assert result is None

            # Test content access for non-existent attachment
            content = await attachment_registry.get_attachment_content(
                db_context, non_existent_id, acting_user_id=None
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

            db_context = Database(engine=db_engine)
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
                db_context, attachment_id, acting_user_id=None
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
                db_context, attachment_id, acting_user_id=None
            )
            assert content == test_content
