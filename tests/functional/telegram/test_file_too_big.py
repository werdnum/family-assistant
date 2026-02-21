"""Tests for handling Telegram's 'File is too big' BadRequest error.

When a user sends a file larger than Telegram's 20MB download limit,
the bot should show a helpful message about the size limit instead of
a generic error.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest

from tests.functional.telegram.helpers import assert_bot_sent_message
from tests.functional.telegram.test_telegram_handler import (
    create_context,
    create_mock_update_with_photo,
)

if TYPE_CHECKING:
    from tests.functional.telegram.conftest import TelegramHandlerTestFixture

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_photo_file_too_big_shows_specific_error(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """When get_file raises BadRequest('File is too big'), user sees a helpful size limit message."""
    fix = telegram_handler_fixture

    photo_update = create_mock_update_with_photo(
        message_text="Check out this photo",
        chat_id=123,
        user_id=12345,
        message_id=101,
        bot=fix.bot,
    )
    context = create_context(fix.application)

    with patch(
        "telegram.ext.ExtBot.get_file",
        AsyncMock(side_effect=BadRequest("File is too big")),
    ):
        await fix.handler.message_handler(photo_update, context)

    response = await assert_bot_sent_message(
        fix.telegram_client, "too large", timeout=5.0, partial_match=True
    )
    response_text = response.get("message", {}).get("text", "")
    assert "20MB" in response_text


@pytest.mark.asyncio
async def test_other_bad_request_shows_generic_error(
    telegram_handler_fixture: TelegramHandlerTestFixture,
) -> None:
    """When get_file raises BadRequest with a different message, user sees the generic error."""
    fix = telegram_handler_fixture

    photo_update = create_mock_update_with_photo(
        message_text="Check out this photo",
        chat_id=123,
        user_id=12345,
        message_id=101,
        bot=fix.bot,
    )
    context = create_context(fix.application)

    with patch(
        "telegram.ext.ExtBot.get_file",
        AsyncMock(
            side_effect=BadRequest(
                "Wrong file_id or the file is temporarily unavailable"
            )
        ),
    ):
        await fix.handler.message_handler(photo_update, context)

    response = await assert_bot_sent_message(
        fix.telegram_client,
        "error processing attached media",
        timeout=5.0,
        partial_match=True,
    )
    response_text = response.get("message", {}).get("text", "")
    assert "too large" not in response_text.lower()
