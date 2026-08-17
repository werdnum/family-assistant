"""Which Telegram failures are worth trying again.

The classification decides whether an undelivered delegation result is retried
with the same text or handed back to the model to do something else with, so
getting it wrong either burns turns rewriting fine messages or retries a
refusal forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    TimedOut,
)

from family_assistant.interfaces import ChatDeliveryError
from family_assistant.telegram.interface import TelegramChatInterface

if TYPE_CHECKING:
    from telegram.ext import Application


class _RefusingBot:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def send_message(self, **_: object) -> object:
        raise self._error


def _interface(error: Exception) -> TelegramChatInterface:
    return TelegramChatInterface(
        cast("Application", SimpleNamespace(bot=_RefusingBot(error)))
    )


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (BadRequest("Message is too long"), False),
        (BadRequest("Chat not found"), False),
        (Forbidden("Forbidden: bot was blocked by the user"), False),
        (InvalidToken(), False),
        (ChatMigrated(new_chat_id=-100123), False),
        (NetworkError("connection reset"), True),
        (TimedOut(), True),
    ],
)
@pytest.mark.asyncio
async def test_telegram_failures_are_classified(
    error: Exception, transient: bool
) -> None:
    with pytest.raises(ChatDeliveryError) as raised:
        await _interface(error).send_message(conversation_id="123", text="hello")

    assert raised.value.transient is transient


@pytest.mark.asyncio
async def test_a_conversation_id_that_is_not_a_chat_id_is_permanent() -> None:
    # It will not start being an integer on a later attempt.
    with pytest.raises(ChatDeliveryError) as raised:
        await _interface(NetworkError("unused")).send_message(
            conversation_id="not-a-chat-id", text="hello"
        )

    assert raised.value.transient is False
