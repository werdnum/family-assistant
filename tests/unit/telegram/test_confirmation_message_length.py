"""Tests that Telegram confirmation messages stay within the send limit.

MarkdownV2 escaping expands the prompt (a backslash before each reserved
character), so a prompt that fits raw can overflow Telegram's single-message
limit once converted. When that happens the message must fall back to the
already length-bounded plain text rather than be sent over-limit and fail —
otherwise the user can never approve the confirmation.
"""

from __future__ import annotations

from telegram.constants import ParseMode

from family_assistant.telegram.ui import (
    TELEGRAM_CONFIRMATION_MESSAGE_LIMIT,
    confirmation_text_and_parse_mode,
)


def _select(prompt_text: str) -> tuple[str, ParseMode | None]:
    return confirmation_text_and_parse_mode(prompt_text)


def test_markdown_is_used_when_converted_text_fits() -> None:
    # A short prompt with a reserved character converts to MarkdownV2 and fits.
    text, parse_mode = _select("a_b reserved")
    assert parse_mode is ParseMode.MARKDOWN_V2
    assert "\\_" in text


def test_falls_back_to_plain_text_when_markdown_overflows_limit() -> None:
    # Raw length fits, but escaping each reserved '.' to '\.' doubles it, pushing
    # the converted text past the limit (mimicking a log/source snippet).
    reserved_heavy = "." * 3000
    assert len(reserved_heavy) <= TELEGRAM_CONFIRMATION_MESSAGE_LIMIT

    text, parse_mode = _select(reserved_heavy)

    assert parse_mode is None
    assert text == reserved_heavy
    assert len(text) <= TELEGRAM_CONFIRMATION_MESSAGE_LIMIT


def test_selected_text_never_exceeds_the_limit() -> None:
    for prompt in (
        "short",
        "a_b*c[d]e",
        "." * 3000,
        "(arg) " * 900,
        "x" * (TELEGRAM_CONFIRMATION_MESSAGE_LIMIT * 2),
    ):
        text, _ = _select(prompt)
        assert len(text) <= TELEGRAM_CONFIRMATION_MESSAGE_LIMIT, prompt
