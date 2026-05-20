"""Tests for ``family_assistant.utils.text_normalization``."""

from __future__ import annotations

import pytest

from family_assistant.utils.text_normalization import normalize_latex_to_unicode


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
