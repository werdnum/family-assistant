"""Telegram Bot API 10.1 Rich Messages compatibility shim.

Provides support for Telegram Bot API 10.1 `sendRichMessage` endpoint
through the python-telegram-bot transport layer before PTB officially
exposes typed methods in v23.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from telegram import Bot, Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

if TYPE_CHECKING:
    from telegram import (
        ForceReply,
        InlineKeyboardMarkup,
        ReplyKeyboardMarkup,
        ReplyKeyboardRemove,
    )

logger = logging.getLogger(__name__)

# Matches GitHub Flavored Markdown / standard Markdown table structures:
# A header row followed by a delimiter row of hyphens, pipes, and optional alignment colons.
_MARKDOWN_TABLE_REGEX = re.compile(
    r"^[ \t]*\|?.+\|.+\|?[ \t]*\r?\n[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class InputRichMessage:
    """Describes a rich message payload for Telegram's `sendRichMessage` endpoint.

    Attributes:
        markdown: Content of the rich message using Markdown formatting.
        html: Content of the rich message using HTML formatting.
        is_rtl: Pass True if the rich message must be shown right-to-left.
        skip_entity_detection: Pass True to skip automatic detection of entities.
    """

    markdown: str | None = None
    html: str | None = None
    is_rtl: bool | None = None
    skip_entity_detection: bool | None = None

    def __post_init__(self) -> None:
        if self.markdown is None and self.html is None:
            raise ValueError("InputRichMessage requires either 'markdown' or 'html'.")
        if self.markdown is not None and self.html is not None:
            raise ValueError("InputRichMessage cannot have both 'markdown' and 'html'.")

    def to_dict(self) -> dict[str, object]:
        """Serialize into a dictionary matching Telegram Bot API specifications."""
        data: dict[str, object] = {}
        if self.markdown is not None:
            data["markdown"] = self.markdown
        if self.html is not None:
            data["html"] = self.html
        if self.is_rtl is not None:
            data["is_rtl"] = self.is_rtl
        if self.skip_entity_detection is not None:
            data["skip_entity_detection"] = self.skip_entity_detection
        return data


def has_markdown_table(text: str) -> bool:
    """Check if ``text`` contains a Markdown table."""
    return bool(_MARKDOWN_TABLE_REGEX.search(text))


def should_attempt_rich_message(
    text: str, parse_mode: str | ParseMode | None = None
) -> bool:
    """Determine whether a message should be sent as a Telegram Bot API 10.1 rich message.

    Rich messages use Telegram's native CommonMark/GFM markdown parser and larger payload limits,
    eliminating the entity escaping bugs of legacy MarkdownV2 and natively supporting Markdown tables.
    Returns True for all standard Markdown or unspecified parse modes when text is non-empty.
    """
    if not text:
        return False
    return not (
        parse_mode is not None
        and parse_mode
        not in {
            "MarkdownV2",
            "Markdown",
            ParseMode.MARKDOWN_V2,
            ParseMode.MARKDOWN,
        }
    )


async def send_rich_message(
    bot: Bot,
    chat_id: int | str,
    text: str | InputRichMessage | dict[str, object],
    *,
    reply_to_message_id: int | None = None,
    reply_markup: (
        InlineKeyboardMarkup
        | ReplyKeyboardMarkup
        | ReplyKeyboardRemove
        | ForceReply
        | None
    ) = None,
    message_thread_id: int | None = None,
    disable_notification: bool | None = None,
    protect_content: bool | None = None,
    is_html: bool = False,
    is_rtl: bool | None = None,
    skip_entity_detection: bool | None = None,
    read_timeout: float | None = None,
    write_timeout: float | None = None,
    connect_timeout: float | None = None,
    pool_timeout: float | None = None,
    api_kwargs: dict[str, object] | None = None,
) -> Message:
    """Send a rich message using Telegram Bot API's `sendRichMessage` endpoint.

    Uses the typed `send_rich_message` method if available (future PTB versions),
    or delegates to PTB's internal `_send_message` transport layer.

    Args:
        bot: The Telegram bot instance.
        chat_id: Unique identifier for the target chat.
        text: Rich message content as text, InputRichMessage, or dict.
        reply_to_message_id: Optional ID of the message to reply to.
        reply_markup: Optional reply markup (ForceReply, InlineKeyboardMarkup, etc.).
        message_thread_id: Optional target thread ID in supergroups.
        disable_notification: Optional flag to send silently.
        protect_content: Optional flag to protect content from saving/forwarding.
        is_html: If True and text is a string, treats text as HTML instead of Markdown.
        is_rtl: Optional flag for right-to-left layout.
        skip_entity_detection: Optional flag to skip automatic entity detection.
        read_timeout: Optional read timeout override.
        write_timeout: Optional write timeout override.
        connect_timeout: Optional connect timeout override.
        pool_timeout: Optional pool timeout override.
        api_kwargs: Optional additional API arguments.

    Returns:
        The sent Telegram Message.

    Raises:
        TelegramError: When Telegram rejects the request.
        AttributeError / NotImplementedError: When the bot transport does not support sending.
    """
    # 1. Format payload
    rich_payload: dict[str, object]
    if isinstance(text, InputRichMessage):
        rich_payload = text.to_dict()
    elif isinstance(text, dict):
        rich_payload = text
    else:
        rich_msg = (
            InputRichMessage(
                html=text,
                is_rtl=is_rtl,
                skip_entity_detection=skip_entity_detection,
            )
            if is_html
            else InputRichMessage(
                markdown=text,
                is_rtl=is_rtl,
                skip_entity_detection=skip_entity_detection,
            )
        )
        rich_payload = rich_msg.to_dict()

    # 2. Check if bot has native send_rich_message (e.g. PTB v23+)
    bot_any: Any = bot
    native_send: Any = getattr(bot_any, "send_rich_message", None)
    if callable(native_send):
        res: Any = await cast(
            "Any",
            native_send(
                chat_id=chat_id,
                rich_message=rich_payload,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
                disable_notification=disable_notification,
                protect_content=protect_content,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
                connect_timeout=connect_timeout,
                pool_timeout=pool_timeout,
                api_kwargs=api_kwargs,
            ),
        )
        return cast("Message", res)

    # 3. Call PTB's _send_message transport layer
    transport_send: Any = getattr(bot_any, "_send_message", None)
    if callable(transport_send):
        data: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": rich_payload,
        }
        kwargs: dict[str, object] = {
            "disable_notification": disable_notification,
            "protect_content": protect_content,
            "reply_markup": reply_markup,
            "message_thread_id": message_thread_id,
            "reply_to_message_id": reply_to_message_id,
        }
        if read_timeout is not None:
            kwargs["read_timeout"] = read_timeout
        if write_timeout is not None:
            kwargs["write_timeout"] = write_timeout
        if connect_timeout is not None:
            kwargs["connect_timeout"] = connect_timeout
        if pool_timeout is not None:
            kwargs["pool_timeout"] = pool_timeout
        if api_kwargs is not None:
            kwargs["api_kwargs"] = api_kwargs

        result: Any = await cast(
            "Any",
            transport_send(
                "sendRichMessage",
                data=data,
                **kwargs,
            ),
        )
        if isinstance(result, Message) or hasattr(result, "message_id"):
            return cast("Message", result)
        if isinstance(result, dict):
            return cast("Message", Message.de_json(result, bot))
        raise TelegramError(f"Unexpected response from sendRichMessage: {result!r}")

    raise AttributeError(
        f"Bot instance {type(bot).__name__} does not support sending rich messages."
    )
