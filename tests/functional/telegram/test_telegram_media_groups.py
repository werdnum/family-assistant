"""Tests for Telegram Media Group functionality.

This module tests that the TelegramChatInterface properly groups consecutive
image attachments into Telegram media groups when sending multiple images.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from family_assistant.services.attachment_registry import AttachmentRegistry
from family_assistant.storage.context import DatabaseContext
from family_assistant.telegram_bot import TelegramChatInterface


@pytest.mark.asyncio
async def test_multiple_images_sent_as_media_group(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that multiple consecutive image attachments are sent as a media group."""
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send_media_group to return list of messages
    mock_sent_messages = [
        MagicMock(message_id=201),
        MagicMock(message_id=202),
        MagicMock(message_id=203),
    ]
    mock_bot.send_media_group = AsyncMock(return_value=mock_sent_messages)

    # Create attachment registry with mocked methods

    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments", db_engine=db_engine
    )

    # Create test image data
    test_image_1 = b"\x89PNG\r\n\x1a\n"  # Minimal PNG header
    test_image_2 = b"\x89PNG\r\n\x1a\n"  # Minimal PNG header
    test_image_3 = b"\x89PNG\r\n\x1a\n"  # Minimal PNG header

    async with DatabaseContext(db_engine):
        # Store three image attachments using proper registration
        attachment_1 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_1,
            filename="image1.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 1",
        )

        attachment_2 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_2,
            filename="image2.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 2",
        )

        attachment_3 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_3,
            filename="image3.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 3",
        )

        attachment_ids = [
            attachment_1.attachment_id,
            attachment_2.attachment_id,
            attachment_3.attachment_id,
        ]

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

        # Verify send_media_group was called once with 3 images
        mock_bot.send_media_group.assert_called_once()
        call_args = mock_bot.send_media_group.call_args

        assert call_args[1]["chat_id"] == 123
        assert len(call_args[1]["media"]) == 3

        # Verify that send_photo was NOT called (images sent as group)
        mock_bot.send_photo.assert_not_called()


@pytest.mark.asyncio
async def test_single_image_sent_individually(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that a single image is sent using send_photo, not as a media group."""
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send_photo to return a message
    mock_sent_message = MagicMock(message_id=201)
    mock_bot.send_photo = AsyncMock(return_value=mock_sent_message)

    # Create attachment registry

    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments", db_engine=db_engine
    )

    # Create test image data
    test_image = b"\x89PNG\r\n\x1a\n"  # Minimal PNG header

    async with DatabaseContext(db_engine):
        # Store one image attachment
        attachment = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image,
            filename="single_image.png",
            content_type="image/png",
            tool_name="test",
            description="single_image.png",  # Use filename as description for caption
        )

        attachment_ids = [attachment.attachment_id]

        # Create TelegramChatInterface
        chat_interface = TelegramChatInterface(
            application=mock_app,
            attachment_registry=attachment_registry,
        )

        # Send the attachment
        await chat_interface._send_attachments(
            chat_id=123,
            attachment_ids=attachment_ids,
            reply_to_msg_id=None,
        )

        # Verify send_photo was called once
        mock_bot.send_photo.assert_called_once()
        call_args = mock_bot.send_photo.call_args

        assert call_args[1]["chat_id"] == 123
        assert call_args[1]["caption"] == "single_image.png"

        # Verify that send_media_group was NOT called
        mock_bot.send_media_group.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_attachments_grouped_correctly(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that mixed image and document attachments are handled correctly.

    Images should be grouped together, but separated by documents.
    """
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send methods
    mock_sent_image_group = [MagicMock(message_id=201), MagicMock(message_id=202)]
    mock_bot.send_media_group = AsyncMock(return_value=mock_sent_image_group)
    mock_bot.send_document = AsyncMock(return_value=MagicMock(message_id=203))
    mock_bot.send_photo = AsyncMock(return_value=MagicMock(message_id=204))

    # Create attachment registry

    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments", db_engine=db_engine
    )

    # Create test data
    test_image_1 = b"\x89PNG\r\n\x1a\n"
    test_image_2 = b"\x89PNG\r\n\x1a\n"
    test_doc = b"Test document content"
    test_image_3 = b"\x89PNG\r\n\x1a\n"

    async with DatabaseContext(db_engine):
        # Store attachments in sequence: image, image, document, image
        attachment_1 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_1,
            filename="image1.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 1",
        )
        attachment_2 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_2,
            filename="image2.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 2",
        )
        attachment_3 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_doc,
            filename="document.pdf",
            content_type="application/pdf",
            tool_name="test",
            description="document.pdf",  # Used as filename
        )
        attachment_4 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_3,
            filename="image3.png",
            content_type="image/png",
            tool_name="test",
            description="image3.png",  # Used as caption
        )

        attachment_ids = [
            attachment_1.attachment_id,
            attachment_2.attachment_id,
            attachment_3.attachment_id,
            attachment_4.attachment_id,
        ]

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

        # Verify:
        # 1. First two images sent as media group
        mock_bot.send_media_group.assert_called_once()
        media_group_call = mock_bot.send_media_group.call_args
        assert len(media_group_call[1]["media"]) == 2

        # 2. Document sent separately
        mock_bot.send_document.assert_called_once()
        doc_call = mock_bot.send_document.call_args
        assert doc_call[1]["filename"] == "document.pdf"

        # 3. Last image sent individually (not enough consecutive images for group)
        mock_bot.send_photo.assert_called_once()
        photo_call = mock_bot.send_photo.call_args
        assert photo_call[1]["caption"] == "image3.png"


@pytest.mark.asyncio
async def test_media_group_with_reply_to(
    db_engine: "AsyncEngine",  # type: ignore[name-defined] # noqa: F821
) -> None:
    """Test that media groups properly include reply_to_message_id."""
    # Setup mock application and bot
    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    # Mock send_media_group
    mock_sent_messages = [MagicMock(message_id=201), MagicMock(message_id=202)]
    mock_bot.send_media_group = AsyncMock(return_value=mock_sent_messages)

    # Create attachment registry

    attachment_registry = AttachmentRegistry(
        storage_path="/tmp/test_attachments", db_engine=db_engine
    )

    # Create test image data
    test_image_1 = b"\x89PNG\r\n\x1a\n"
    test_image_2 = b"\x89PNG\r\n\x1a\n"

    async with DatabaseContext(db_engine):
        # Store two image attachments
        attachment_1 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_1,
            filename="image1.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 1",
        )
        attachment_2 = await attachment_registry.store_and_register_tool_attachment(
            file_content=test_image_2,
            filename="image2.png",
            content_type="image/png",
            tool_name="test",
            description="Test image 2",
        )

        attachment_ids = [attachment_1.attachment_id, attachment_2.attachment_id]

        # Create TelegramChatInterface
        chat_interface = TelegramChatInterface(
            application=mock_app,
            attachment_registry=attachment_registry,
        )

        # Send the attachments with reply_to
        await chat_interface._send_attachments(
            chat_id=123,
            attachment_ids=attachment_ids,
            reply_to_msg_id=100,  # Replying to message 100
        )

        # Verify send_media_group was called with reply_to_message_id
        mock_bot.send_media_group.assert_called_once()
        call_args = mock_bot.send_media_group.call_args

        assert call_args[1]["chat_id"] == 123
        assert call_args[1]["reply_to_message_id"] == 100
        assert len(call_args[1]["media"]) == 2
