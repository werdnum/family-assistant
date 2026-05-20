"""Property-based and exhaustive tests for ``text_normalization``.

The non-streaming and streaming paths must agree byte-for-byte for any input:
``"".join(normalizer.feed(c) for c in chunks) + normalizer.flush()`` must
equal ``normalize_latex_to_unicode("".join(chunks))`` regardless of how the
input is split. This file exercises that invariant with Hypothesis-generated
realistic LLM-style inputs and chunkings, plus an explicit table of
construct combinations the reviewers have surfaced and adjacent edge cases.

The Hypothesis strategy intentionally separates constructs with whitespace
so it doesn't generate adversarial chains (``$\\alpha$$\\beta$``-style)
that no incremental algorithm could resolve without buffering the entire
message; the explicit regression table below covers the patterns that
actually appeared in code review.
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from family_assistant.utils.text_normalization import (
    StreamingLatexNormalizer,
    normalize_latex_to_unicode,
)

_COMMANDS: Final = [
    "alpha",
    "beta",
    "gamma",
    "rightarrow",
    "to",
    "le",
    "ge",
    "ne",
    "approx",
    "infty",
    "pi",
    "Delta",
    "frac",
    "unknown",  # not in dict; must pass through
]


def _stream(chunks: list[str]) -> str:
    normalizer = StreamingLatexNormalizer()
    parts = [normalizer.feed(chunk) for chunk in chunks]
    parts.append(normalizer.flush())
    return "".join(parts)


def _assert_consistent(text: str, chunks: list[str]) -> None:
    assert "".join(chunks) == text, "chunking dropped or duplicated input"
    streamed = _stream(chunks)
    persisted = normalize_latex_to_unicode(text)
    assert streamed == persisted, (
        f"divergence for {text!r} chunked as {chunks!r}\n"
        f"  streamed:  {streamed!r}\n"
        f"  persisted: {persisted!r}"
    )


@st.composite
def _realistic_message(draw: st.DrawFn) -> str:
    """Build a realistic LLM-style message.

    Constructs (math spans, code spans, commands, currency, prose) are
    separated by whitespace so we don't accidentally generate adversarial
    overlapping delimiters. This matches how an LLM actually emits text.
    """
    pieces: list[str] = []
    fragment_count = draw(st.integers(min_value=0, max_value=8))
    for index in range(fragment_count):
        kind = draw(
            st.sampled_from([
                "prose",
                "prose",
                "prose",
                "command",
                "currency",
                "math",
                "code",
            ])
        )
        if kind == "prose":
            count = draw(st.integers(min_value=1, max_value=10))
            chars = draw(
                st.lists(
                    st.sampled_from("abcdefghijklmnopqrstuvwxyz .,;:!?"),
                    min_size=count,
                    max_size=count,
                )
            )
            pieces.append("".join(chars))
        elif kind == "command":
            pieces.append("\\" + draw(st.sampled_from(_COMMANDS)))
        elif kind == "currency":
            amount = draw(st.integers(min_value=1, max_value=9999))
            pieces.append(f"${amount}")
        elif kind == "math":
            cmd = draw(st.sampled_from(_COMMANDS))
            opener = draw(st.sampled_from(["$", "$$", "\\(", "\\["]))
            closer = {"$": "$", "$$": "$$", "\\(": "\\)", "\\[": "\\]"}[opener]
            pad = draw(st.sampled_from(["", " ", "  "]))
            pieces.append(f"{opener}{pad}\\{cmd}{pad}{closer}")
        else:  # code
            length = draw(st.integers(min_value=1, max_value=3))
            cmd = draw(st.sampled_from(_COMMANDS))
            fence = "`" * length
            pieces.append(f"{fence}\\{cmd}{fence}")
        if index < fragment_count - 1:
            pieces.append(draw(st.sampled_from([" ", "  ", " \n", "\n"])))
    return "".join(pieces)


@st.composite
def _chunkings(draw: st.DrawFn, text: str) -> list[str]:
    """Split ``text`` into 1..N non-empty contiguous chunks."""
    if len(text) < 2:
        return [text] if text else [""]
    n = len(text)
    max_splits = min(n - 1, 6)
    num_splits = draw(st.integers(min_value=0, max_value=max_splits))
    if num_splits == 0:
        return [text]
    cuts = sorted(
        draw(
            st.lists(
                st.integers(min_value=1, max_value=n - 1),
                min_size=num_splits,
                max_size=num_splits,
                unique=True,
            )
        )
    )
    chunks: list[str] = []
    prev = 0
    for cut in cuts:
        chunks.append(text[prev:cut])
        prev = cut
    chunks.append(text[prev:])
    return chunks


class TestStreamingMatchesPersisted:
    """The core invariant: streamed output must equal persisted normalization
    for any chunking of realistic LLM-style input.
    """

    @given(_realistic_message())
    @settings(
        max_examples=400,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_single_chunk_round_trip(self, text: str) -> None:
        _assert_consistent(text, [text] if text else [""])

    @given(st.data())
    @settings(
        max_examples=600,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_arbitrary_chunking(self, data: st.DataObject) -> None:
        text = data.draw(_realistic_message())
        chunks = data.draw(_chunkings(text))
        _assert_consistent(text, chunks)

    @given(_realistic_message())
    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_char_by_char_streaming(self, text: str) -> None:
        # Worst case: every character is its own chunk. Stresses the
        # construct-deferral logic the hardest.
        chunks = list(text) if text else [""]
        _assert_consistent(text, chunks)


# Hand-picked inputs covering the reviewer's case set plus adjacent shapes.
# Each is tested under three chunkings: whole, mid-construct, char-by-char.
_REGRESSION_INPUTS: Final = [
    # Currency and math interaction
    "Price $5 and \\alpha cost $10.",
    "Saved $50 thanks to \\alpha-testing!",
    "Spent $5 and saved $\\rightarrow$ all good",
    "cost $5 and $\\alpha$ end",
    # Inline math
    "A $\\rightarrow$ B",
    "$\\alpha + \\beta$",
    "$ \\alpha$",
    "$ \\alpha + \\beta $",
    "$\\foo \\alpha$",  # unknown command inside math
    # Display math
    "$$\\int f \\, dx$$",
    "$$\\alpha + \\beta$$",
    # \( \) and \[ \]
    "Use \\(\\alpha\\) here.",
    "Display: \\[\\sum x_i\\] end.",
    # Code spans
    "Use `\\alpha` for greek.",
    "``\\alpha``",
    "```\\beta```",
    "Block:\n```\nalpha = \\frac{1}{2}\n``` end",
    # Code span containing math closer (must not be paired)
    "Use \\[`\\alpha\\]` notation.",
    "Code: `\\]` standalone.",
    # Math opener and would-be closer separated by a code span -- the
    # math regex would only see each side as a separate segment, so the
    # streaming scan must too.
    "$\\alpha`x`$\\beta$",
    "\\[\\alpha`x`\\]\\beta\\)",
    # Currency
    "Just $100 here.",
    "Paid $50 today, $25 yesterday.",
    "price is \\$100 plus \\$50",
    # Multiple constructs
    "$\\alpha$ then `\\beta` plus \\gamma",
    "`\\alpha`, `\\beta`, plain \\gamma",
    # Multiline
    "Line1: $\\alpha$\nLine2: $5\nLine3: $\\beta$",
    # Empty / boundary
    "",
    " ",
    "\\",
    "$",
    "`",
    "$\\",
    "``",
    "```",
    # Adjacent backticks
    "``a``",
    "`a` `b` `c`",
    # Embedded
    "Outer ``has `inner` backticks`` more",
    # Currency near $$ display
    "I owe $$\\alpha$$ to you and $5 in cash",
    # Pathological backslashes
    "\\\\alpha",  # escaped backslash then alpha
    "\\$\\alpha\\$",  # escaped dollars
]


@pytest.mark.parametrize("text", _REGRESSION_INPUTS)
class TestRegressionInputs:
    def test_whole_chunk(self, text: str) -> None:
        _assert_consistent(text, [text] if text else [""])

    def test_char_by_char(self, text: str) -> None:
        chunks = list(text) if text else [""]
        _assert_consistent(text, chunks)

    def test_split_after_every_other_char(self, text: str) -> None:
        if len(text) < 2:
            chunks = [text] if text else [""]
        else:
            chunks = [text[i : i + 2] for i in range(0, len(text), 2)]
        _assert_consistent(text, chunks)


class TestNormalizeProperties:
    """Properties of the non-streaming function itself."""

    @given(st.text(alphabet="abcde ", max_size=40))
    def test_plain_text_unchanged(self, text: str) -> None:
        assert normalize_latex_to_unicode(text) == text

    @given(st.text(alphabet="abcde$ 1234567890.", max_size=40))
    def test_text_without_backslash_unchanged(self, text: str) -> None:
        # No backslash means no LaTeX -- short-circuit must preserve input.
        assert normalize_latex_to_unicode(text) == text

    @given(_realistic_message())
    @settings(max_examples=200, deadline=None)
    def test_idempotent(self, text: str) -> None:
        once = normalize_latex_to_unicode(text)
        twice = normalize_latex_to_unicode(once)
        assert once == twice


class TestStreamingProperties:
    """Properties of the streaming normalizer itself."""

    @given(_realistic_message())
    @settings(max_examples=200, deadline=None)
    def test_feed_returns_persisted_after_flush(self, text: str) -> None:
        # The streaming output (after flush) must equal the persisted output.
        streamed = _stream([text])
        persisted = normalize_latex_to_unicode(text)
        assert streamed == persisted

    def test_flush_after_no_feed_is_empty(self) -> None:
        normalizer = StreamingLatexNormalizer()
        assert not normalizer.flush()

    def test_flush_is_idempotent(self) -> None:
        normalizer = StreamingLatexNormalizer()
        normalizer.feed("hello")
        normalizer.flush()
        assert not normalizer.flush()

    def test_empty_feed_returns_empty(self) -> None:
        normalizer = StreamingLatexNormalizer()
        assert not normalizer.feed("")
