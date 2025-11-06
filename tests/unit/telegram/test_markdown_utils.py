"""Tests for Telegram markdown utilities."""

from __future__ import annotations

import pytest

from family_assistant.telegram.markdown_utils import fix_telegramify_markdown_escaping


class TestFixTelegramifyMarkdownEscaping:
    """Tests for fix_telegramify_markdown_escaping function."""

    def test_already_escaped_chars_not_double_escaped(self) -> None:
        """Test that already escaped characters are not double-escaped."""
        text = r"Already \> escaped and \< here"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Already \> escaped and \< here"

    def test_unescaped_gt_at_start(self) -> None:
        """Test that unescaped '>' at start of string is escaped."""
        text = ">Blockquote"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"\>Blockquote"

    def test_unescaped_gt_after_newline(self) -> None:
        """Test that unescaped '>' after newline is escaped."""
        text = "Line1\n>Line2"
        result = fix_telegramify_markdown_escaping(text)
        assert result == "Line1\n\\>Line2"

    def test_unescaped_lt_escaped(self) -> None:
        """Test that unescaped '<' is escaped."""
        text = "Test <html> tags"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Test \<html\> tags"

    def test_mixed_escaped_and_unescaped(self) -> None:
        """Test mixed escaped and unescaped characters."""
        text = r"Already \> escaped but <not> this"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Already \> escaped but \<not\> this"

    def test_normal_text_unchanged(self) -> None:
        """Test that normal text without special chars is unchanged."""
        text = "Normal text without special characters"
        result = fix_telegramify_markdown_escaping(text)
        assert result == "Normal text without special characters"

    def test_comparison_operators(self) -> None:
        """Test that comparison operators are properly escaped."""
        text = "Math: 5 > 3 and 2 < 4"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Math: 5 \> 3 and 2 \< 4"

    def test_empty_string(self) -> None:
        """Test that empty string is handled correctly."""
        text = ""
        result = fix_telegramify_markdown_escaping(text)
        assert not result

    def test_only_special_chars(self) -> None:
        """Test string with only special characters."""
        text = "<>"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"\<\>"

    def test_consecutive_special_chars(self) -> None:
        """Test consecutive special characters."""
        text = "Text >>more<< here"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Text \>\>more\<\< here"

    def test_real_world_example(self) -> None:
        """Test a real-world example that would trigger the error."""
        # Simulate telegramify_markdown output with unescaped '<'
        text = "Task Status Report\nCompleted: 5 > 3\nPending: 2 < 4\n<important> note"
        result = fix_telegramify_markdown_escaping(text)
        assert (
            result
            == "Task Status Report\nCompleted: 5 \\> 3\nPending: 2 \\< 4\n\\<important\\> note"
        )


@pytest.mark.parametrize(
    "input_text,expected",
    [
        ("Normal text", "Normal text"),
        ("Text with > char", r"Text with \> char"),
        (r"Already \> escaped", r"Already \> escaped"),
        ("Text <html> tags", r"Text \<html\> tags"),
        (">Blockquote", r"\>Blockquote"),
        ("Line1\n>Line2", "Line1\n\\>Line2"),
        ("Math: 5 > 3 and 2 < 4", r"Math: 5 \> 3 and 2 \< 4"),
    ],
)
def test_fix_telegramify_markdown_escaping_parametrized(
    input_text: str, expected: str
) -> None:
    """Parametrized test for various input scenarios."""
    result = fix_telegramify_markdown_escaping(input_text)
    assert result == expected
