"""Autospecced python-telegram-bot objects for tests of the outbound interface.

The Telegram Bot API is an external service, so tests that only care which
Bot method the interface chose and with what arguments use an autospecced
``Bot``: it rejects methods and call signatures the real ``Bot`` does not have.
What it returns is a real ``Message``, not a mock standing in for one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from unittest.mock import create_autospec

from telegram import Bot, Chat, Message
from telegram.ext import Application

if TYPE_CHECKING:
    from unittest.mock import NonCallableMagicMock


class AutospeccedTelegram(NamedTuple):
    application: Application[Any, Any, Any, Any, Any, Any]
    bot: NonCallableMagicMock


def sent_message(message_id: int, chat_id: int = 123) -> Message:
    """A real ``Message`` as the Bot API returns it for a delivered send."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=Chat.PRIVATE),
    )


def make_autospecced_telegram() -> AutospeccedTelegram:
    """An autospecced ``Application`` whose ``bot`` is an autospecced ``Bot``."""
    bot = create_autospec(Bot, instance=True, spec_set=True)
    bot.send_message.return_value = sent_message(1)
    application = create_autospec(Application, instance=True, spec_set=True)
    application.bot = bot
    return AutospeccedTelegram(
        application=cast("Application[Any, Any, Any, Any, Any, Any]", application),
        bot=bot,
    )
