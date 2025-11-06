"""Utilities for handling Telegram MarkdownV2 formatting."""

from __future__ import annotations

import logging
import re

import telegramify_markdown

logger = logging.getLogger(__name__)


def fix_telegramify_markdown_escaping(text: str) -> str:
    """
    Fix escaping bugs in telegramify_markdown output.

    The telegramify_markdown library has bugs where it doesn't properly escape
    '<' and '>' characters in certain contexts:
    1. '<' characters are never escaped
    2. '>' at the start of lines (blockquotes) are not escaped

    This function post-processes the output from telegramify_markdown.markdownify()
    to ensure these characters are properly escaped for Telegram's MarkdownV2 format.

    Args:
        text: The text output from telegramify_markdown.markdownify()

    Returns:
        Text with '<' and '>' characters properly escaped
    """
    # Use regex with negative lookbehind to avoid double-escaping
    # Matches '<' or '>' that are NOT already escaped (not preceded by '\')
    text = re.sub(r"(?<!\\)([<>])", r"\\\1", text)
    return text


def convert_to_telegram_markdown(text: str) -> tuple[str, str | None]:
    """
    Convert text to Telegram MarkdownV2 format with error handling.

    This is a shim function that encapsulates the telegramify_markdown conversion
    and applies our bug fixes. It provides a single point of control for markdown
    conversion across the codebase.

    Args:
        text: Plain text or markdown to convert

    Returns:
        Tuple of (converted_text, parse_mode):
        - converted_text: The text to send (either MarkdownV2 or plain text)
        - parse_mode: "MarkdownV2" if conversion succeeded, None for plain text

    Examples:
        >>> text, mode = convert_to_telegram_markdown("Hello *world*")
        >>> # text is MarkdownV2 formatted, mode is "MarkdownV2"

        >>> text, mode = convert_to_telegram_markdown("Text with < and >")
        >>> # text has < and > properly escaped, mode is "MarkdownV2"
    """
    try:
        converted = telegramify_markdown.markdownify(text)
        # Fix escaping bugs in telegramify_markdown
        converted = fix_telegramify_markdown_escaping(converted)
        return converted, "MarkdownV2"
    except Exception as e:
        logger.warning(
            f"Failed to convert text to MarkdownV2: {e}. Will use plain text.",
            exc_info=True,
        )
        return text, None
