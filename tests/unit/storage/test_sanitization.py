"""Tests for PostgreSQL text sanitization utilities."""

from family_assistant.storage.context import (
    _is_non_retryable_postgres_error,  # noqa: PLC2701 - testing private function behavior
    sanitize_text_for_postgres,
)


class TestSanitizeTextForPostgres:
    """Tests for sanitize_text_for_postgres function."""

    def test_none_input_returns_none(self) -> None:
        """None input should return None."""
        assert sanitize_text_for_postgres(None) is None

    def test_empty_string_returns_empty(self) -> None:
        """Empty string should return empty string."""
        assert sanitize_text_for_postgres("") == ""  # noqa: PLC1901 - explicitly testing empty string return

    def test_normal_text_unchanged(self) -> None:
        """Normal text should pass through unchanged."""
        text = "Hello, World! This is normal text."
        assert sanitize_text_for_postgres(text) == text

    def test_removes_null_bytes(self) -> None:
        """Null bytes should be removed."""
        text = "Hello\x00World"
        assert sanitize_text_for_postgres(text) == "HelloWorld"

    def test_removes_multiple_null_bytes(self) -> None:
        """Multiple null bytes should be removed."""
        text = "\x00Hello\x00\x00World\x00"
        assert sanitize_text_for_postgres(text) == "HelloWorld"

    def test_preserves_newlines_and_tabs(self) -> None:
        """Newlines and tabs should be preserved."""
        text = "Line1\nLine2\tTabbed"
        assert sanitize_text_for_postgres(text) == text

    def test_preserves_unicode(self) -> None:
        """Unicode characters should be preserved."""
        text = "Hello 世界 🌍 émoji"
        assert sanitize_text_for_postgres(text) == text

    def test_handles_surrogate_characters(self) -> None:
        """Surrogate characters should be replaced."""
        # Create a string with a lone surrogate (invalid UTF-8)
        text = "Hello\ud800World"
        result = sanitize_text_for_postgres(text)
        assert result is not None
        # Should replace the surrogate with replacement character
        assert "\ud800" not in result
        assert "Hello" in result
        assert "World" in result

    def test_handles_ansi_escape_sequences(self) -> None:
        """ANSI escape sequences should be preserved."""
        text = "\x1b[31mRed text\x1b[0m"
        assert sanitize_text_for_postgres(text) == text

    def test_mixed_problematic_content(self) -> None:
        """Should handle mixed problematic content."""
        # Null byte with normal text
        text = "Browser output:\x00console.log('test')"
        result = sanitize_text_for_postgres(text)
        assert result is not None
        assert "\x00" not in result
        assert result == "Browser output:console.log('test')"


class TestIsNonRetryablePostgresError:
    """Tests for _is_non_retryable_postgres_error function."""

    def test_none_returns_false(self) -> None:
        """None should return (False, '')."""
        assert _is_non_retryable_postgres_error(None) == (False, "")

    def test_generic_exception_returns_false(self) -> None:
        """Generic exceptions should return (False, '')."""
        exc = Exception("Some error")
        assert _is_non_retryable_postgres_error(exc) == (False, "")

    def test_detects_transaction_error_by_pgcode(self) -> None:
        """Should detect transaction errors by SQLSTATE code (25P02)."""

        class MockPostgresError(Exception):
            pgcode = "25P02"  # in_failed_sql_transaction

        exc = MockPostgresError("Transaction aborted")
        is_non_retryable, error_type = _is_non_retryable_postgres_error(exc)
        assert is_non_retryable is True
        assert error_type == "transaction_aborted"

    def test_detects_encoding_error_by_pgcode(self) -> None:
        """Should detect encoding errors by SQLSTATE code (22021)."""

        class MockPostgresError(Exception):
            pgcode = "22021"  # character_not_in_repertoire

        exc = MockPostgresError("Invalid byte sequence")
        is_non_retryable, error_type = _is_non_retryable_postgres_error(exc)
        assert is_non_retryable is True
        assert error_type == "encoding_error"

    def test_retryable_pgcode_returns_false(self) -> None:
        """SQLSTATE codes for retryable errors should return (False, '')."""

        class MockPostgresError(Exception):
            pgcode = "40001"  # serialization_failure - retryable

        exc = MockPostgresError("Serialization failure")
        is_non_retryable, error_type = _is_non_retryable_postgres_error(exc)
        assert is_non_retryable is False
        assert error_type == ""  # noqa: PLC1901 - explicitly testing empty string return

    def test_exception_without_pgcode_returns_false(self) -> None:
        """Exceptions without pgcode attribute should return (False, '')."""

        class SomeOtherError(Exception):
            pass

        exc = SomeOtherError("Some error")
        is_non_retryable, error_type = _is_non_retryable_postgres_error(exc)
        assert is_non_retryable is False
        assert error_type == ""  # noqa: PLC1901 - explicitly testing empty string return
