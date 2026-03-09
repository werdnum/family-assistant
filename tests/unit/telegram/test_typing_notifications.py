from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.telegram.handler import TelegramUpdateHandler

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


@pytest.mark.asyncio
async def test_typing_notifications_shutdown_timeout_is_suppressed() -> None:
    """Typing loop shutdown should not fail message handling on timeout."""
    handler = cast("TelegramUpdateHandler", object.__new__(TelegramUpdateHandler))
    never_finishes_event = asyncio.Event()

    async def send_chat_action(*_: object, **__: object) -> None:
        # Simulate a hung network call so typing_task cannot finish within 1s.
        await never_finishes_event.wait()

    context = cast(
        "ContextTypes.DEFAULT_TYPE",
        SimpleNamespace(bot=SimpleNamespace(send_chat_action=send_chat_action)),
    )

    async with handler._typing_notifications(context, chat_id=123):
        pass
