"""Tests for Telegram video attachment and filename handling.

This module tests that:
1. Video attachments are sent using send_video.
2. Document attachments use the original filename if available, falling back to description/ID.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.telegram.interface import TelegramChatInterface


@pytest.mark.asyncio
async def test_video_attachment_sent_as_video(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that a video attachment is sent using send_video."""
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send_video
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=201))
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=202))

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments_video", db_engine=db_engine
    )

    # Create test video data
    test_video = b"fake video content"

    async with DatabaseContext(db_engine):
        # Store video attachment
        attachment = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_video,
            filename="video.mp4",
            content_type="video/mp4",
            tool_name="test",
            description="Generated video: Test prompt",  # Description is NOT a filename
        )

        attachment_ids = [attachment.attachment_id]

        # Create TelegramChatInterface
        chat_interface = TelegramChatInterface(
            application=mock_app,
            attachment_registry=attachment_registry,
        )

        # Send the attachments
        await chat_interface._send_attachments(
            chat_id=123,
            attachment_ids=attachment_ids,
            reply_to_msg_id=None,
        )

        # Verify send_video was called
        mock_bot.send_video.assert_called_once()
        call_args = mock_bot.send_video.call_args
        assert call_args[1]["chat_id"] == 123
        assert call_args[1]["caption"] == "Generated video: Test prompt"

        # Verify send_document was NOT called
        mock_bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_document_attachment_uses_original_filename(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that a document attachment uses the original filename from metadata."""
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send_document
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=203))

    # Create attachment registry
    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments_doc", db_engine=db_engine
    )

    # Create test document data
    test_doc = b"fake document content"

    async with DatabaseContext(db_engine):
        # Store document attachment
        attachment = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_doc,
            filename="report.pdf",
            content_type="application/pdf",
            tool_name="test",
            description="Generated report",  # Description is NOT a filename
        )

        attachment_ids = [attachment.attachment_id]

        # Create TelegramChatInterface
        chat_interface = TelegramChatInterface(
            application=mock_app,
            attachment_registry=attachment_registry,
        )

        # Send the attachments
        await chat_interface._send_attachments(
            chat_id=123,
            attachment_ids=attachment_ids,
            reply_to_msg_id=None,
        )

        # Verify send_document was called
        mock_bot.send_document.assert_called_once()
        call_args = mock_bot.send_document.call_args
        assert call_args[1]["chat_id"] == 123

        # Verify filename is correct (from metadata, not description)
        assert call_args[1]["filename"] == "report.pdf"

        # Verify caption uses description
        assert call_args[1]["caption"] == "Generated report"
