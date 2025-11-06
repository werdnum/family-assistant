"""Utilities for handling Telegram MarkdownV2 formatting."""

from __future__ import annotations


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
    result = []
    i = 0

    while i < len(text):
        char = text[i]

        # Check if this character needs escaping
        if char in "<>":
            # Check if it's already escaped (preceded by backslash)
            if i > 0 and text[i - 1] == "\\":
                # Already escaped, keep as-is
                result.append(char)
            else:
                # Not escaped, add escape
                result.append("\\")
                result.append(char)
        else:
            result.append(char)

        i += 1

    return "".join(result)
