"""Splitting outbound messages that Telegram would refuse for being too long."""

from __future__ import annotations

TELEGRAM_SINGLE_MESSAGE_LIMIT = 4096
"""Hard cap Telegram enforces on a single message's text."""

TELEGRAM_MAX_MESSAGE_LENGTH = 4000
"""Size we split outbound text at, leaving headroom under the hard cap."""

CHUNK_SEND_DELAY_SECONDS = 0.2
"""Pacing between the pieces of a split message, to keep them in order."""

_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ")

_MINIMUM_FILL_RATIO = 0.5
"""How full a chunk must be for a boundary to be worth splitting on."""


def split_message_text(
    text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split ``text`` into pieces Telegram will accept.

    Splits land on the largest structural boundary available near the limit --
    paragraph, then line, then sentence, then word -- so the pieces read as
    written. A run with no boundary in reach (a long URL, an encoded blob) is
    cut at the limit. Whitespace at a split point is dropped; everything else
    is preserved exactly, in order, with no overlap between pieces.

    Returns an empty list for text that is empty or entirely whitespace.
    """
    if limit < 1:
        raise ValueError(f"Chunk limit must be positive, got {limit}.")

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = _split_point(remaining, limit)
        chunk = remaining[:split_at].rstrip()
        remaining = remaining[split_at:]
        if chunk:
            chunks.append(chunk)

    if remaining.strip():
        chunks.append(remaining)
    return chunks


def _split_point(text: str, limit: int) -> int:
    """Return the index to cut ``text`` at, at most ``limit`` characters in."""
    window = text[:limit]
    minimum_fill = int(limit * _MINIMUM_FILL_RATIO)
    for separator in _SEPARATORS:
        boundary = window.rfind(separator)
        if boundary >= minimum_fill:
            return boundary + len(separator)
    return limit
