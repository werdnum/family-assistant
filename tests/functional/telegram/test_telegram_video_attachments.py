"""Tests for Telegram video attachment and filename handling.

This module tests that:
1. Video attachments are sent using send_video.
2. Document attachments use the original filename if available, falling back to description/ID.
"""

import pytest

from family_assistant.security.taint import TurnTaintState
from family_assistant.telegram.interface import TelegramChatInterface
from tests.functional.telegram.conftest import TelegramHandlerTestFixture
from tests.functional.telegram.helpers import wait_for_bot_response


@pytest.mark.asyncio
async def test_video_attachment_sent_as_video(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """Test that a video attachment is sent using send_video."""
    fixture = telegram_handler_fixture
    assert fixture.assistant.attachment_registry is not None

    attachment = (
        await fixture.assistant.attachment_registry.store_and_register_tool_attachment(
            file_content=b"fake video content",
            filename="video.mp4",
            content_type="video/mp4",
            tool_name="test",
            description="Generated video: Test prompt",  # Description is NOT a filename
            taint_state=TurnTaintState.empty(),
        )
    )

    assert fixture.assistant.telegram_service is not None
    chat_interface = fixture.assistant.telegram_service.chat_interface
    assert isinstance(chat_interface, TelegramChatInterface)
    await chat_interface.send_message(
        conversation_id="123",
        text="here you go",
        attachment_ids=[attachment.attachment_id],
    )

    updates = await wait_for_bot_response(fixture.telegram_client)
    messages = [u.get("message", {}) for u in updates]
    message = next((m for m in messages if m.get("video")), {})
    assert message.get("video") is not None, (
        f"Expected a video message, got: {messages}"
    )
    assert message.get("text") == "Generated video: Test prompt"
    assert message.get("document") is None


@pytest.mark.asyncio
async def test_document_attachment_uses_original_filename(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """Test that a document attachment uses the original filename from metadata."""
    fixture = telegram_handler_fixture
    assert fixture.assistant.attachment_registry is not None

    attachment = (
        await fixture.assistant.attachment_registry.store_and_register_tool_attachment(
            file_content=b"fake document content",
            filename="report.pdf",
            content_type="application/pdf",
            tool_name="test",
            description="Generated report",  # Description is NOT a filename
            taint_state=TurnTaintState.empty(),
        )
    )

    assert fixture.assistant.telegram_service is not None
    chat_interface = fixture.assistant.telegram_service.chat_interface
    assert isinstance(chat_interface, TelegramChatInterface)
    await chat_interface.send_message(
        conversation_id="123",
        text="here you go",
        attachment_ids=[attachment.attachment_id],
    )

    updates = await wait_for_bot_response(fixture.telegram_client)
    messages = [u.get("message", {}) for u in updates]
    message = next((m for m in messages if m.get("document")), {})
    document = message.get("document")
    assert document is not None, f"Expected a document message, got: {messages}"

    # Verify filename is correct (from metadata, not description)
    assert document.get("file_name") == "report.pdf"

    # Verify caption uses description
    assert message.get("text") == "Generated report"
