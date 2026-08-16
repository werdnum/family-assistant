"""Long messages sent through the chat interface reach Telegram.

Telegram refuses any message over 4096 characters. Delegation results,
notifications and anything else delivered through ``ChatInterface.send_message``
go out on this path, and a refused send is reported as a delivery failure --
which for a delegation run means it stays unnotified and is retried, failing
identically, every hour forever. So the interface has to split what it sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from telegram.error import BadRequest

from family_assistant.telegram.chunking import TELEGRAM_SINGLE_MESSAGE_LIMIT
from family_assistant.telegram.interface import TelegramChatInterface

if TYPE_CHECKING:
    from telegram import ForceReply
    from telegram.constants import ParseMode
    from telegram.ext import Application

_CHAT_ID = "109472877"


@dataclass(frozen=True)
class _SentMessage:
    text: str
    parse_mode: ParseMode | None
    reply_to_message_id: int | None
    reply_markup: ForceReply | None


class _FakeBot:
    """A bot that refuses over-long messages, the way Telegram does."""

    def __init__(self, *, rejects_parse_mode: bool = False) -> None:
        self.sent: list[_SentMessage] = []
        self._rejects_parse_mode = rejects_parse_mode

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: ParseMode | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: ForceReply | None = None,
    ) -> SimpleNamespace:
        _ = chat_id
        if len(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT:
            raise BadRequest("Message is too long")
        if self._rejects_parse_mode and parse_mode is not None:
            raise BadRequest("Can't parse entities: can't find end of the entity")
        self.sent.append(
            _SentMessage(
                text=text,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
        )
        return SimpleNamespace(message_id=100 + len(self.sent))


def _interface(bot: _FakeBot) -> TelegramChatInterface:
    return TelegramChatInterface(cast("Application", SimpleNamespace(bot=bot)))


def _long_result() -> str:
    return "\n\n".join(
        f"Finding {index}: " + "the pod restarted zero times. " * 8
        for index in range(20)
    )


@pytest.mark.asyncio
async def test_a_result_over_the_cap_is_delivered_as_several_messages() -> None:
    bot = _FakeBot()
    text = _long_result()
    assert len(text) > TELEGRAM_SINGLE_MESSAGE_LIMIT

    message_id = await _interface(bot).send_message(conversation_id=_CHAT_ID, text=text)

    assert message_id == "101"
    assert len(bot.sent) > 1
    delivered = "\n\n".join(sent.text for sent in bot.sent)
    assert delivered.split() == text.split()


@pytest.mark.asyncio
async def test_only_the_first_message_replies_to_the_prompting_message() -> None:
    bot = _FakeBot()

    await _interface(bot).send_message(
        conversation_id=_CHAT_ID,
        text=_long_result(),
        reply_to_interface_id="42",
    )

    later_messages = bot.sent[1:]
    assert later_messages
    assert bot.sent[0].reply_to_message_id == 42
    assert bot.sent[0].reply_markup is not None
    assert all(sent.reply_to_message_id is None for sent in later_messages)
    assert all(sent.reply_markup is None for sent in later_messages)


@pytest.mark.asyncio
async def test_a_piece_whose_escaping_overflows_the_cap_goes_out_as_plain_text() -> (
    None
):
    # Escaping adds a backslash before each '.', so a full piece of them more
    # than doubles in length once converted - past what Telegram will accept.
    bot = _FakeBot()

    await _interface(bot).send_message(
        conversation_id=_CHAT_ID, text="." * 5000, parse_mode="MarkdownV2"
    )

    assert bot.sent[0].parse_mode is None
    assert bot.sent[0].text == "." * 4000


@pytest.mark.asyncio
async def test_a_piece_telegram_cannot_parse_is_retried_as_plain_text() -> None:
    bot = _FakeBot(rejects_parse_mode=True)
    text = "Here is some text with ||spoiler-like|| content."

    message_id = await _interface(bot).send_message(
        conversation_id=_CHAT_ID, text=text, parse_mode="MarkdownV2"
    )

    assert message_id == "101"
    assert bot.sent[0].parse_mode is None
    assert bot.sent[0].text == text
