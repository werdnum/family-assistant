"""Tests for Telegram markdown utilities."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from family_assistant.telegram.markdown_utils import (
    convert_to_telegram_markdown,
    fix_telegramify_markdown_escaping,
)


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

    def test_backslash_not_consumed(self) -> None:
        """Test that backslashes not before special chars are preserved."""
        text = r"Path: C:\Users\file.txt with > and <"
        result = fix_telegramify_markdown_escaping(text)
        assert result == r"Path: C:\Users\file.txt with \> and \<"

    def test_multiple_backslashes(self) -> None:
        r"""Test handling of multiple backslashes.

        Note: The regex uses negative lookbehind (?<!\\) which cannot detect
        escaped backslashes. In \\>, the > appears to be escaped (preceded by \),
        so it won't be escaped again. This is an acceptable limitation for our
        use case of post-processing telegramify_markdown output.
        """
        text = r"Test \\> and \\< with double backslashes"
        result = fix_telegramify_markdown_escaping(text)
        # The regex sees \ before > and <, so they don't get escaped
        assert result == r"Test \\> and \\< with double backslashes"

    def test_escaped_backslash_before_special_char(self) -> None:
        r"""Test handling of escaped backslash before special char.

        Note: The regex cannot distinguish between \> (escaped >) and \\>
        (escaped backslash followed by >). This is an acceptable trade-off
        for the simplicity and performance of the regex approach.
        """
        # If we have \\> (escaped backslash followed by >), the regex sees \ before >
        text = r"\\>"
        result = fix_telegramify_markdown_escaping(text)
        # Result is unchanged - regex sees backslash before >
        assert result == r"\\>"


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


class TestConvertToTelegramMarkdown:
    """Tests for convert_to_telegram_markdown shim function."""

    def test_successful_conversion(self) -> None:
        """Test successful markdown conversion."""
        text = "Hello *world*"
        result, parse_mode = convert_to_telegram_markdown(text)
        assert parse_mode == "MarkdownV2"
        assert result  # Should have some converted content

    def test_escaping_applied(self) -> None:
        """Test that escaping bug fixes are applied."""
        text = "Text with < and >"
        result, parse_mode = convert_to_telegram_markdown(text)
        assert parse_mode == "MarkdownV2"
        # The result should have escaped < and >
        assert r"\<" in result
        assert r"\>" in result

    def test_conversion_error_fallback(self) -> None:
        """Test fallback to plain text on conversion error."""
        with patch(
            "family_assistant.telegram.markdown_utils.telegramify_markdown.markdownify",
            side_effect=Exception("Test error"),
        ):
            text = "Test text"
            result, parse_mode = convert_to_telegram_markdown(text)
            assert parse_mode is None  # Should fall back to plain text
            assert result == text  # Should return original text

    def test_empty_string(self) -> None:
        """Test conversion of empty string."""
        text = ""
        result, parse_mode = convert_to_telegram_markdown(text)
        # Should succeed but return empty (with newline from markdownify)
        assert parse_mode == "MarkdownV2"

    def test_complex_markdown(self) -> None:
        """Test conversion of complex markdown with multiple elements."""
        text = "# Header\n\n**Bold** and *italic* with > and < symbols"
        result, parse_mode = convert_to_telegram_markdown(text)
        assert parse_mode == "MarkdownV2"
        # Should have escaped the angle brackets
        assert r"\>" in result
        assert r"\<" in result
