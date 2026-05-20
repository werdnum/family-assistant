"""Tests for ``family_assistant.utils.text_normalization``."""

from __future__ import annotations

import pytest

from family_assistant.utils.text_normalization import (
    StreamingLatexNormalizer,
    normalize_latex_to_unicode,
)


class TestNoOpCases:
    """The function should be a no-op when there's nothing LaTeX-ish to do."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Plain text with no markup.",
            "Hello, world!",
            "Numbers like 1, 2, 3 and 4.5 should pass through.",
            "Multi\nline\ntext.",
        ],
    )
    def test_returns_input_unchanged(self, text: str) -> None:
        assert normalize_latex_to_unicode(text) == text

    def test_currency_amount_preserved(self) -> None:
        assert (
            normalize_latex_to_unicode("It costs $100 and saves $50 monthly.")
            == "It costs $100 and saves $50 monthly."
        )

    def test_two_dollar_amounts_preserved(self) -> None:
        # Two stand-alone amounts shouldn't be treated as math delimiters,
        # because there's no \command between them.
        assert (
            normalize_latex_to_unicode("Paid $50 today, $25 yesterday.")
            == "Paid $50 today, $25 yesterday."
        )

    def test_backslash_without_letters_unchanged(self) -> None:
        # Markdown-style backslash escapes shouldn't get touched.
        assert (
            normalize_latex_to_unicode(r"escaped \* asterisk") == r"escaped \* asterisk"
        )

    def test_unknown_command_unchanged(self) -> None:
        assert (
            normalize_latex_to_unicode(r"some \madeupcommand here")
            == r"some \madeupcommand here"
        )


class TestArrowConversions:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (r"A $\rightarrow$ B", "A → B"),
            (r"A $\to$ B", "A → B"),
            (r"A $\Rightarrow$ B", "A ⇒ B"),
            (r"A $\implies$ B", "A ⇒ B"),
            (r"A $\leftarrow$ B", "A ← B"),
            (r"A $\leftrightarrow$ B", "A ↔ B"),
            (r"A $\Leftrightarrow$ B", "A ⇔ B"),
            (r"A $\mapsto$ B", "A ↦ B"),
        ],
    )
    def test_inline_arrows(self, source: str, expected: str) -> None:
        assert normalize_latex_to_unicode(source) == expected


class TestGreekLetters:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (r"$\alpha$ is a constant", "α is a constant"),
            (r"angle $\theta$", "angle θ"),
            (r"sum of $\pi$ and $\phi$", "sum of π and φ"),
            (r"$\Delta$ T", "Δ T"),
            (r"$\Omega$ resistance", "Ω resistance"),
        ],
    )
    def test_greek_inline_math(self, source: str, expected: str) -> None:
        assert normalize_latex_to_unicode(source) == expected


class TestMathDelimiterStripping:
    def test_paren_delimiters_stripped(self) -> None:
        assert (
            normalize_latex_to_unicode(r"State \(\alpha\) means start")
            == "State α means start"
        )

    def test_bracket_delimiters_stripped(self) -> None:
        assert (
            normalize_latex_to_unicode(r"Display: \[\sum x_i\] end")
            == "Display: ∑ x_i end"
        )

    def test_double_dollar_delimiters_stripped(self) -> None:
        assert (
            normalize_latex_to_unicode(r"Block: $$\int f$$ done") == "Block: ∫ f done"
        )

    def test_inline_dollars_without_command_preserved(self) -> None:
        # No LaTeX command inside, so the dollars stay.
        assert (
            normalize_latex_to_unicode("Spent $5 then $7 then $3.")
            == "Spent $5 then $7 then $3."
        )


class TestBareCommands:
    def test_bare_command_without_delimiters(self) -> None:
        assert (
            normalize_latex_to_unicode(r"Use \alpha and \beta freely.")
            == "Use α and β freely."
        )

    def test_command_inside_sentence(self) -> None:
        assert normalize_latex_to_unicode(r"so 2 \times 3 = 6") == "so 2 × 3 = 6"

    def test_multiple_commands_in_one_line(self) -> None:
        result = normalize_latex_to_unicode(r"$\alpha \rightarrow \beta$ then $\gamma$")
        assert result == "α → β then γ"


class TestRelationsAndOperators:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (r"x $\le$ y", "x ≤ y"),
            (r"x $\geq$ 0", "x ≥ 0"),
            (r"a $\ne$ b", "a ≠ b"),
            (r"a $\approx$ b", "a ≈ b"),
            (r"$\infty$", "∞"),
            (r"$\pm$", "±"),
            (r"$\cdot$", "·"),
        ],
    )
    def test_relations(self, source: str, expected: str) -> None:
        assert normalize_latex_to_unicode(source) == expected


class TestEdgeCases:
    def test_empty_string(self) -> None:
        assert not normalize_latex_to_unicode("")

    def test_text_with_no_backslash_short_circuits(self) -> None:
        # This is a behaviour assertion: even with dollars present, if there's
        # no backslash we return the input unchanged.
        s = "Just $100 here."
        assert normalize_latex_to_unicode(s) is s

    def test_multiline_text_with_mixed_content(self) -> None:
        source = (
            "Recipe:\n"
            r"- mix $\alpha$ with $\beta$" + "\n"
            r"- heat to 100\degree" + "\n"
            "- cost: $5"
        )
        expected = "Recipe:\n- mix α with β\n- heat to 100°\n- cost: $5"
        assert normalize_latex_to_unicode(source) == expected

    def test_inline_math_must_be_on_one_line(self) -> None:
        # A stray $ on one line and another on a different line shouldn't be
        # treated as math delimiters.
        source = "First line has $5.\nSecond line has \\alpha cost: $10."
        # \alpha is converted as a bare command; the dollars stay put.
        assert (
            normalize_latex_to_unicode(source)
            == "First line has $5.\nSecond line has α cost: $10."
        )

    def test_unknown_command_inside_math_preserved(self) -> None:
        # Inside delimiters, unknown commands stay as-is and dollars are
        # stripped because the inner content contains a command.
        assert normalize_latex_to_unicode(r"$\foo \alpha$") == r"\foo α"

    def test_no_double_processing_of_escaped_dollar(self) -> None:
        # An escaped dollar in markdown shouldn't act as a math delimiter.
        assert (
            normalize_latex_to_unicode(r"price is \$100 plus \$50")
            == r"price is \$100 plus \$50"
        )


class TestCurrencyNotCorrupted:
    """Regression: ``$...$`` must not be stripped across prose.

    The original regex matched whenever any ``\\command`` appeared between
    two ``$`` characters on the same line, which corrupted currency mentions.
    The fix requires the math content to start with a backslash command.
    """

    def test_currency_with_bare_command_between(self) -> None:
        assert (
            normalize_latex_to_unicode(r"Price $5 and \alpha cost $10.")
            == r"Price $5 and α cost $10."
        )

    def test_dollar_amounts_with_command_first(self) -> None:
        # The first $ opens what looks like math, but content starts with
        # "5 ..." not "\command", so the dollars stay put.
        assert (
            normalize_latex_to_unicode(r"Saved $50 thanks to \alpha-testing!")
            == r"Saved $50 thanks to α-testing!"
        )

    def test_dollar_followed_by_text_then_command(self) -> None:
        # `$abc\alpha$` -- content doesn't start with a backslash command,
        # so we treat the dollars as literal and only convert the command.
        assert normalize_latex_to_unicode(r"$abc\alpha$") == r"$abcα$"

    def test_proper_math_still_converts(self) -> None:
        # Sanity check that valid `$\command$` math still works after the fix.
        assert (
            normalize_latex_to_unicode(r"Spent $5 and saved $\rightarrow$ all good")
            == r"Spent $5 and saved → all good"
        )


class TestCodeSpansPreserved:
    """LaTeX inside markdown code spans must be left verbatim."""

    def test_inline_code_preserved(self) -> None:
        assert (
            normalize_latex_to_unicode(r"Use `\alpha` for greek alpha")
            == r"Use `\alpha` for greek alpha"
        )

    def test_inline_code_alongside_normal_math(self) -> None:
        # Code keeps `\alpha`; surrounding `$\beta$` is converted.
        assert (
            normalize_latex_to_unicode(r"Type `\alpha` to get $\beta$")
            == r"Type `\alpha` to get β"
        )

    def test_fenced_code_block_preserved(self) -> None:
        source = "Block:\n```\n\\alpha + \\beta\n```\nafter"
        assert normalize_latex_to_unicode(source) == source

    def test_multiple_code_spans_preserved(self) -> None:
        assert (
            normalize_latex_to_unicode(r"`\alpha`, `\beta`, plain \gamma")
            == r"`\alpha`, `\beta`, plain γ"
        )


class TestStreamingLatexNormalizer:
    """Buffered normalization for chunked / streamed text."""

    def _drain(self, normalizer: StreamingLatexNormalizer, chunks: list[str]) -> str:
        emitted = "".join(normalizer.feed(chunk) for chunk in chunks)
        return emitted + normalizer.flush()

    def test_single_complete_chunk(self) -> None:
        n = StreamingLatexNormalizer()
        assert self._drain(n, [r"A $\rightarrow$ B"]) == "A → B"

    def test_command_split_across_chunks(self) -> None:
        # The reviewer's example: `"$\alp"` then `"ha$"` must produce `α`.
        n = StreamingLatexNormalizer()
        assert self._drain(n, [r"$\alp", r"ha$"]) == "α"

    def test_dollar_open_across_chunks(self) -> None:
        # Math opens in one chunk and closes in the next.
        n = StreamingLatexNormalizer()
        assert self._drain(n, [r"start $\rig", r"htarrow$ end"]) == "start → end"

    def test_code_span_inside_stream_preserved(self) -> None:
        n = StreamingLatexNormalizer()
        assert (
            self._drain(n, [r"use `\al", r"pha` for ", r"$\beta$"])
            == r"use `\alpha` for β"
        )

    def test_trailing_backslash_held_until_complete(self) -> None:
        n = StreamingLatexNormalizer()
        # First feed ends with a bare backslash -- must hold back.
        assert n.feed("hi \\") == "hi "
        # Next feed completes the command.
        assert n.feed("rightarrow done") == "→ done"
        assert not n.flush()

    def test_currency_dollars_in_stream_preserved(self) -> None:
        n = StreamingLatexNormalizer()
        # Even with a backslash later in the buffer, the currency $ shouldn't
        # pair with it because the math content wouldn't start with `\command`.
        out = self._drain(n, [r"Price $5 and ", r"\alpha cost $10."])
        assert out == r"Price $5 and α cost $10."

    def test_currency_followed_by_split_math_span(self) -> None:
        # Regression: the safe-split logic must not pair a currency `$` with
        # a later math opener. Streaming "cost $5 and $\alp" then "ha$ end"
        # should produce "cost $5 and α end", matching the persisted message.
        n = StreamingLatexNormalizer()
        out = self._drain(n, [r"cost $5 and $\alp", r"ha$ end"])
        assert out == r"cost $5 and α end"

    def test_backtick_inside_bracket_math_does_not_split(self) -> None:
        # Regression: a backtick inside a \[...\] math span must not be
        # treated as an unclosed inline-code opener -- it's consumed by the
        # bracket-math substitution. Streamed output must match persisted.
        n = StreamingLatexNormalizer()
        chunks = [r"\[\alpha `", r"\]"]
        out = self._drain(n, chunks)
        assert out == normalize_latex_to_unicode("".join(chunks))

    def test_display_dollar_math_split_across_chunks(self) -> None:
        # Regression: ``$$...$$`` display math must be atomic in the stream
        # scan. If we treated the opener as two single-``$`` literals, the
        # first ``$`` would be emitted early and the rest of the buffer would
        # be normalized as inline math, leaving ``$α$`` instead of ``α``.
        n = StreamingLatexNormalizer()
        chunks = [r"$$\alp", r"ha$$"]
        out = self._drain(n, chunks)
        assert out == normalize_latex_to_unicode("".join(chunks))
        assert out == "α"

    def test_empty_chunks_are_noop(self) -> None:
        n = StreamingLatexNormalizer()
        assert not n.feed("")
        assert not n.flush()

    def test_plain_text_passes_through(self) -> None:
        n = StreamingLatexNormalizer()
        assert self._drain(n, ["Hello, ", "world!"]) == "Hello, world!"
