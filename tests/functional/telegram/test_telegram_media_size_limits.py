"""Media sent as a Telegram document is bounded by the multimodal limit.

A video or recording attached as a file arrives as `message.document`, not
`message.video`/`message.audio`, so the branch that handles it has to take its
limit from the document's MIME type rather than from the branch it landed in.
Otherwise the whole payload is downloaded and then refused at registration with
the handler's generic "could not process" reply, which says nothing about size.
"""

from typing import Any

import pytest
from telegram import Update
from telegram.ext import Application, ContextTypes

from .conftest import TelegramHandlerTestFixture
from .helpers import assert_bot_sent_message, wait_for_bot_response

_MEDIA_LIMIT = 1024 * 1024
_OVERSIZED = b"\0" * (2 * _MEDIA_LIMIT)


def create_mock_context(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> ContextTypes.DEFAULT_TYPE:
    return ContextTypes.DEFAULT_TYPE(
        application=application, chat_id=123, user_id=12345
    )


async def _send_document(
    fix: TelegramHandlerTestFixture, content: bytes, filename: str, mime_type: str
) -> None:
    result = await fix.telegram_client.send_document(
        document_content=content,
        filename=filename,
        caption="What is in this?",
        mime_type=mime_type,
    )
    assert result.get("ok") is True, f"Failed to send document: {result}"
    update = Update.de_json(data=result.get("result", {}), bot=fix.bot)
    assert update is not None

    await fix.handler.message_handler(update, create_mock_context(fix.application))


@pytest.mark.asyncio
async def test_oversized_video_sent_as_a_document_is_refused_with_its_size(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    fix = telegram_handler_fixture
    fix.handler.telegram_service.attachment_registry.max_multimodal_size = _MEDIA_LIMIT

    await _send_document(fix, _OVERSIZED, "clip.mp4", "video/mp4")

    await assert_bot_sent_message(
        fix.telegram_client, "File size exceeds the 1MB limit"
    )


@pytest.mark.asyncio
async def test_an_oversized_pdf_document_is_still_accepted(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """The tighter bound follows the MIME type, not the document branch.

    A PDF the same size is text to be extracted, so `max_file_size` governs it
    and nothing is refused.
    """
    fix = telegram_handler_fixture
    fix.handler.telegram_service.attachment_registry.max_multimodal_size = _MEDIA_LIMIT

    await _send_document(fix, _OVERSIZED, "report.pdf", "application/pdf")

    updates = await wait_for_bot_response(fix.telegram_client)
    texts = [u.get("message", {}).get("text", "") for u in updates]
    assert not any("File size exceeds" in text for text in texts), texts
