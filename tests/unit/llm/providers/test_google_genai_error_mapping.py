"""Tests for Google GenAI error mapping and retry parsing."""

from unittest.mock import MagicMock

import pytest

from family_assistant.llm.base import (
    AuthenticationError,
    ContextLengthError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.mark.no_db
class TestParseRetryAfter:
    def test_parses_retry_delay(self) -> None:
        assert GoogleGenAIClient._parse_retry_after("retryDelay: 36s") == 36.0

    def test_parses_retry_delay_decimal(self) -> None:
        assert GoogleGenAIClient._parse_retry_after("retryDelay: 1.5s") == 1.5

    def test_parses_retry_delay_in_context(self) -> None:
        msg = 'Resource exhausted. retryDelay: "42s"'
        assert GoogleGenAIClient._parse_retry_after(msg) == 42.0

    def test_returns_none_when_no_delay(self) -> None:
        assert GoogleGenAIClient._parse_retry_after("Rate limited") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert GoogleGenAIClient._parse_retry_after("") is None


@pytest.mark.no_db
class TestMapErrorToTypedException:
    @pytest.fixture
    def client(self) -> MagicMock:
        c = MagicMock(spec=GoogleGenAIClient)
        c.model_name = "models/gemini-test"
        c._parse_retry_after = GoogleGenAIClient._parse_retry_after
        c._map_error_to_typed_exception = (
            GoogleGenAIClient._map_error_to_typed_exception.__get__(c)
        )
        return c

    def test_maps_401_to_authentication_error(self, client: MagicMock) -> None:
        exc = Exception("401 Unauthorized: Invalid API key")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, AuthenticationError)

    def test_maps_429_to_rate_limit_error(self, client: MagicMock) -> None:
        exc = Exception("429 Resource exhausted retryDelay: 36s")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, RateLimitError)
        assert result.retry_after == 36.0

    def test_maps_quota_to_rate_limit_error(self, client: MagicMock) -> None:
        exc = Exception("Quota exceeded for model")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, RateLimitError)

    def test_maps_404_to_model_not_found(self, client: MagicMock) -> None:
        exc = Exception("404 Model not found")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, ModelNotFoundError)

    def test_maps_token_limit_to_context_length(self, client: MagicMock) -> None:
        exc = Exception("Token limit exceeded for this model")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, ContextLengthError)

    def test_maps_400_to_invalid_request(self, client: MagicMock) -> None:
        exc = Exception("400 Bad Request: invalid parameter")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, InvalidRequestError)

    def test_maps_connection_to_connection_error(self, client: MagicMock) -> None:
        exc = Exception("Connection refused")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, ProviderConnectionError)

    def test_maps_timeout_to_timeout_error(self, client: MagicMock) -> None:
        exc = Exception("Request timeout after 30s")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, ProviderTimeoutError)

    def test_maps_unknown_to_base_error(self, client: MagicMock) -> None:
        exc = Exception("Something completely unexpected")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, LLMProviderError)
        assert not isinstance(result, RateLimitError)

    def test_maps_network_to_connection_error(self, client: MagicMock) -> None:
        exc = Exception("Network unreachable")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, ProviderConnectionError)

    def test_rate_limit_without_retry_delay(self, client: MagicMock) -> None:
        exc = Exception("429 Too Many Requests")
        result = client._map_error_to_typed_exception(exc)
        assert isinstance(result, RateLimitError)
        assert result.retry_after is None
