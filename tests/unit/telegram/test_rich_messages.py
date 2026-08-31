"""Tests for Telegram Bot API 10.1 rich message support and compatibility shim."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter

from family_assistant.telegram.chunking import TELEGRAM_MAX_MESSAGE_LENGTH
from family_assistant.telegram.interface import TelegramChatInterface
from family_assistant.telegram.rich_messages import (
    InputRichMessage,
    has_markdown_table,
    send_rich_message,
    should_attempt_rich_message,
)

if TYPE_CHECKING:
    from telegram.ext import Application


@dataclass(frozen=True)
class _SentRichMessageCall:
    endpoint: str
    data: dict[str, object]
    kwargs: dict[str, object]


class _FakeRichBot:
    """Mock bot tracking _send_message and send_message calls."""

    def __init__(
        self,
        *,
        reject_rich: bool = False,
        flood_control_on_rich: bool = False,
    ) -> None:
        self.calls: list[_SentRichMessageCall] = []
        self.standard_sent: list[dict[str, object]] = []
        self._reject_rich = reject_rich
        self._flood_control_on_rich = flood_control_on_rich
        self.rich_attempts = 0

    async def _send_message(
        self,
        endpoint: str,
        data: dict[str, object],
        **kwargs: object,
    ) -> SimpleNamespace:
        self.rich_attempts += 1
        if self._flood_control_on_rich and self.rich_attempts == 1:
            raise RetryAfter(0)
        if self._reject_rich:
            raise BadRequest("Unknown method: sendRichMessage")
        self.calls.append(
            _SentRichMessageCall(endpoint=endpoint, data=data, kwargs=kwargs)
        )
        return SimpleNamespace(message_id=200 + len(self.calls))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: ParseMode | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: ForceReply | None = None,
    ) -> SimpleNamespace:
        self.standard_sent.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
        })
        return SimpleNamespace(message_id=300 + len(self.standard_sent))


def test_input_rich_message_validation() -> None:
    msg_md = InputRichMessage(markdown="# Title\n| A | B |\n|---|---|\n| 1 | 2 |")
    assert msg_md.to_dict() == {"markdown": "# Title\n| A | B |\n|---|---|\n| 1 | 2 |"}

    msg_html = InputRichMessage(
        html="<b>Title</b>", is_rtl=True, skip_entity_detection=True
    )
    assert msg_html.to_dict() == {
        "html": "<b>Title</b>",
        "is_rtl": True,
        "skip_entity_detection": True,
    }

    with pytest.raises(ValueError, match="requires either 'markdown' or 'html'"):
        InputRichMessage()

    with pytest.raises(ValueError, match="cannot have both 'markdown' and 'html'"):
        InputRichMessage(markdown="md", html="html")


def test_has_markdown_table_detection() -> None:
    table_sample = """Here is the schedule:
| Day | Activity |
| --- | --- |
| Mon | Math |
| Tue | Science |
"""
    assert has_markdown_table(table_sample) is True

    compact_table = "Col1 | Col2\n---|---\nA | B"
    assert has_markdown_table(compact_table) is True

    aligned_table = "| Col1 | Col2 |\n|:---|---:|\n| A | B |"
    assert has_markdown_table(aligned_table) is True

    prose_with_pipe = "This is a sentence | with a pipe symbol."
    assert has_markdown_table(prose_with_pipe) is False

    plain_prose = "Simple text without any formatting."
    assert has_markdown_table(plain_prose) is False


def test_should_attempt_rich_message() -> None:
    table_text = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert should_attempt_rich_message(table_text) is True
    assert (
        should_attempt_rich_message(table_text, parse_mode=ParseMode.MARKDOWN_V2)
        is True
    )
    assert should_attempt_rich_message(table_text, parse_mode="MarkdownV2") is True
    assert should_attempt_rich_message(table_text, parse_mode=ParseMode.HTML) is False

    long_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH + 50)
    assert should_attempt_rich_message(long_text) is True

    short_prose = "Hello, world!"
    assert should_attempt_rich_message(short_prose) is True
    assert should_attempt_rich_message(short_prose, parse_mode="MarkdownV2") is True

    assert should_attempt_rich_message("") is False


@pytest.mark.asyncio
async def test_send_rich_message_via_internal_transport() -> None:
    bot = _FakeRichBot()
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("OK", callback_data="ok")]])

    result = await send_rich_message(
        bot=bot,
        chat_id=12345,
        text="# Report\n| A | B |\n|---|---|\n| 1 | 2 |",
        reply_to_message_id=99,
        reply_markup=markup,
        is_rtl=True,
    )

    assert result.message_id == 201
    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert call.endpoint == "sendRichMessage"
    assert call.data["chat_id"] == 12345
    assert (
        cast("dict[str, object]", call.data["rich_message"])["markdown"]
        == "# Report\n| A | B |\n|---|---|\n| 1 | 2 |"
    )
    assert cast("dict[str, object]", call.data["rich_message"])["is_rtl"] is True
    assert call.kwargs["reply_to_message_id"] == 99
    assert call.kwargs["reply_markup"] == markup


@pytest.mark.asyncio
async def test_send_rich_message_via_native_bot_method() -> None:
    bot = SimpleNamespace(
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=777))
    )

    result = await send_rich_message(
        bot=bot,
        chat_id=54321,
        text="<b>Hello</b>",
        is_html=True,
    )

    assert result.message_id == 777
    bot.send_rich_message.assert_awaited_once_with(
        chat_id=54321,
        rich_message={"html": "<b>Hello</b>"},
        reply_to_message_id=None,
        reply_markup=None,
        message_thread_id=None,
        disable_notification=None,
        protect_content=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
        api_kwargs=None,
    )


@pytest.mark.asyncio
async def test_chat_interface_sends_rich_message_for_tables() -> None:
    bot = _FakeRichBot()
    interface = TelegramChatInterface(cast("Application", SimpleNamespace(bot=bot)))

    table_text = "| Col1 | Col2 |\n| --- | --- |\n| Val1 | Val2 |"
    msg_id = await interface.send_message(
        conversation_id="123456",
        text=table_text,
        parse_mode="MarkdownV2",
    )

    assert msg_id == "201"
    assert len(bot.calls) == 1
    assert bot.calls[0].endpoint == "sendRichMessage"
    assert len(bot.standard_sent) == 0


@pytest.mark.asyncio
async def test_chat_interface_sends_rich_message_for_prose_by_default() -> None:
    bot = _FakeRichBot()
    interface = TelegramChatInterface(cast("Application", SimpleNamespace(bot=bot)))

    prose_text = "Hello! Here is your daily update: everything looks good."
    msg_id = await interface.send_message(
        conversation_id="123456",
        text=prose_text,
    )

    assert msg_id == "201"
    assert len(bot.calls) == 1
    assert bot.calls[0].endpoint == "sendRichMessage"
    assert len(bot.standard_sent) == 0


@pytest.mark.asyncio
async def test_chat_interface_falls_back_when_rich_message_fails() -> None:
    bot = _FakeRichBot(reject_rich=True)
    interface = TelegramChatInterface(cast("Application", SimpleNamespace(bot=bot)))

    table_text = "| Col1 | Col2 |\n| --- | --- |\n| Val1 | Val2 |"
    msg_id = await interface.send_message(
        conversation_id="123456",
        text=table_text,
        parse_mode="MarkdownV2",
    )

    assert msg_id == "301"
    assert len(bot.calls) == 0
    assert len(bot.standard_sent) == 1


@pytest.mark.asyncio
async def test_chat_interface_retries_flood_control_on_rich_message() -> None:
    bot = _FakeRichBot(flood_control_on_rich=True)
    interface = TelegramChatInterface(cast("Application", SimpleNamespace(bot=bot)))

    table_text = "| Col1 | Col2 |\n| --- | --- |\n| Val1 | Val2 |"
    msg_id = await interface.send_message(
        conversation_id="123456",
        text=table_text,
        parse_mode="MarkdownV2",
    )

    assert msg_id == "201"
    assert bot.rich_attempts == 2
    assert len(bot.calls) == 1
    assert len(bot.standard_sent) == 0
