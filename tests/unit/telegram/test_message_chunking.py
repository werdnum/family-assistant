"""Splitting outbound text into pieces Telegram will accept.

The splitter has to preserve what it splits: text that arrives as several
messages should read the same as text that fits in one, with nothing dropped,
nothing repeated, and no line structure flattened.
"""

from __future__ import annotations

import pytest

from family_assistant.telegram.chunking import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_message_text,
)


def _words(text: str) -> list[str]:
    return text.split()


def _long_report() -> str:
    return "\n\n".join(
        f"Finding {index}: " + "the pod restarted zero times. " * 8
        for index in range(20)
    )


def test_text_within_the_limit_is_returned_untouched() -> None:
    assert split_message_text("Delegated task finished.") == [
        "Delegated task finished."
    ]


def test_empty_text_produces_no_pieces() -> None:
    assert split_message_text("") == []


def test_whitespace_only_text_produces_no_pieces() -> None:
    assert split_message_text("   \n\n  \t ") == []


def test_a_non_positive_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        split_message_text("some text", limit=0)


def test_every_piece_fits_the_limit() -> None:
    chunks = split_message_text(_long_report())

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH


def test_the_pieces_carry_the_whole_message_exactly_once() -> None:
    text = _long_report()

    chunks = split_message_text(text)

    assert _words("\n\n".join(chunks)) == _words(text)


def test_lines_are_kept_whole_rather_than_flattened() -> None:
    text = "\n".join(f"line {index} " + "x" * 100 for index in range(80))

    chunks = split_message_text(text)

    assert len(chunks) > 1
    assert [line for chunk in chunks for line in chunk.split("\n")] == text.split("\n")


def test_a_paragraph_boundary_is_preferred_over_a_word_boundary() -> None:
    text = "a" * 30 + "\n\n" + "b " * 30

    chunks = split_message_text(text, limit=50)

    assert chunks[0] == "a" * 30


def test_a_boundary_too_early_in_the_piece_is_passed_over() -> None:
    # The only paragraph break sits 2 characters in; splitting there would send
    # a 2-character message and leave the rest just as oversized.
    text = "hi\n\n" + "word " * 100

    chunks = split_message_text(text, limit=50)

    assert len(chunks[0]) > 25


def test_a_run_with_no_boundary_is_cut_at_the_limit() -> None:
    text = "x" * 9000

    assert split_message_text(text) == ["x" * 4000, "x" * 4000, "x" * 1000]
