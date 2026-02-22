"""Tests for user-friendly error messages and stream error mapping."""

import pytest

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.base import (
    ContextLengthError,
    LLMProviderError,
    RateLimitError,
)
from family_assistant.processing import (
    _map_stream_error_to_exception,  # noqa: PLC2701 - Testing internal function
    _user_friendly_error_message,  # noqa: PLC2701 - Testing internal function
)


@pytest.mark.no_db
class TestUserFriendlyErrorMessage:
    def test_rate_limit_with_retry_after(self) -> None:
        exc = RateLimitError("Rate limited", "google", "gemini", retry_after=36)
        msg = _user_friendly_error_message(exc)
        assert "36 seconds" in msg
        assert "rate-limited" in msg

    def test_rate_limit_without_retry_after(self) -> None:
        exc = RateLimitError("Rate limited", "google", "gemini")
        msg = _user_friendly_error_message(exc)
        assert "rate-limited" in msg
        assert "minute or two" in msg

    def test_context_length_error(self) -> None:
        exc = ContextLengthError("Too long", "google", "gemini")
        msg = _user_friendly_error_message(exc)
        assert "too long" in msg
        assert "new conversation" in msg

    def test_generic_exception(self) -> None:
        exc = RuntimeError("Something broke")
        msg = _user_friendly_error_message(exc)
        assert "error" in msg.lower()
        assert "try again" in msg.lower()

    def test_generic_llm_provider_error(self) -> None:
        exc = LLMProviderError("Provider error", "google", "gemini")
        msg = _user_friendly_error_message(exc)
        assert "error" in msg.lower()


@pytest.mark.no_db
class TestMapStreamErrorToException:
    def test_rate_limit_error_event(self) -> None:
        event = LLMStreamEvent(
            type="error",
            error="429 Too Many Requests",
            metadata={
                "error_type": "RateLimitError",
                "provider": "google",
                "model": "gemini",
            },
        )
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, RateLimitError)
        assert exc.provider == "google"
        assert exc.model == "gemini"

    def test_context_length_error_event(self) -> None:
        event = LLMStreamEvent(
            type="error",
            error="Token limit exceeded",
            metadata={
                "error_type": "ContextLengthError",
                "provider": "google",
                "model": "gemini",
            },
        )
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, ContextLengthError)

    def test_unknown_error_type_returns_runtime_error(self) -> None:
        event = LLMStreamEvent(
            type="error",
            error="Something weird",
            metadata={
                "error_type": "UnknownError",
                "provider": "google",
                "model": "gemini",
            },
        )
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, RuntimeError)
        assert "Something weird" in str(exc)

    def test_no_metadata_returns_runtime_error(self) -> None:
        event = LLMStreamEvent(type="error", error="Oops")
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, RuntimeError)

    def test_no_error_message_uses_default(self) -> None:
        event = LLMStreamEvent(type="error")
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, RuntimeError)
        assert "Unknown streaming error" in str(exc)

    def test_empty_error_type_returns_runtime_error(self) -> None:
        event = LLMStreamEvent(
            type="error",
            error="Some error",
            metadata={"error_type": "", "provider": "google", "model": "gemini"},
        )
        exc = _map_stream_error_to_exception(event)
        assert isinstance(exc, RuntimeError)
