"""Tests for PostgreSQL text sanitization and replay-safety classification."""

import sqlite3

from sqlalchemy.exc import DBAPIError

from family_assistant.storage.database import (
    _is_retryable,  # noqa: PLC2701 - testing private function behavior
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


class TestIsRetryable:
    """Tests for _is_retryable, which decides whether atomic() replays a closure."""

    @staticmethod
    def _dbapi_error(orig: BaseException) -> DBAPIError:
        return DBAPIError("SELECT 1", {}, orig)

    def test_serialization_failure_is_retryable(self) -> None:
        """40001 rolled back cleanly, so replaying the closure is safe."""

        class MockPostgresError(Exception):
            pgcode = "40001"

        assert _is_retryable(self._dbapi_error(MockPostgresError("conflict"))) is True

    def test_deadlock_is_retryable(self) -> None:
        """40P01 rolled back cleanly, so replaying the closure is safe."""

        class MockPostgresError(Exception):
            pgcode = "40P01"

        assert _is_retryable(self._dbapi_error(MockPostgresError("deadlock"))) is True

    def test_aborted_transaction_is_not_retryable(self) -> None:
        """25P02 means a statement already failed inside this transaction."""

        class MockPostgresError(Exception):
            pgcode = "25P02"

        assert _is_retryable(self._dbapi_error(MockPostgresError("aborted"))) is False

    def test_encoding_error_is_not_retryable(self) -> None:
        """22021 is deterministic: the same bytes fail the same way."""

        class MockPostgresError(Exception):
            pgcode = "22021"

        assert _is_retryable(self._dbapi_error(MockPostgresError("bad bytes"))) is False

    def test_unrecognized_postgres_error_is_not_retryable(self) -> None:
        """An unlisted SQLSTATE is not assumed safe to replay."""

        class MockPostgresError(Exception):
            pgcode = "08006"  # connection_failure

        assert _is_retryable(self._dbapi_error(MockPostgresError("gone"))) is False

    def test_dropped_connection_is_not_retryable(self) -> None:
        """The outcome is unknown -- the commit may have reached the server.

        Replaying would write the closure's rows a second time rather than
        surfacing the uncertainty.
        """
        orig = OSError("connection was closed in the middle of operation")
        assert _is_retryable(self._dbapi_error(orig)) is False

    def test_sqlite_lock_contention_is_retryable(self) -> None:
        """SQLite reports the lock before the transaction does any work."""
        orig = sqlite3.OperationalError("database is locked")
        assert _is_retryable(self._dbapi_error(orig)) is True

    def test_other_sqlite_operational_errors_are_not_retryable(self) -> None:
        """Only lock contention is known to have left nothing behind."""
        orig = sqlite3.OperationalError("disk I/O error")
        assert _is_retryable(self._dbapi_error(orig)) is False
