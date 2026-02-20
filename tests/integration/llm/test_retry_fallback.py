"""Integration tests for LLM retry and fallback behavior."""

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from family_assistant.llm import LLMInterface, LLMOutput
from family_assistant.llm.base import (
    InvalidRequestError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.factory import LLMClientFactory
from tests.factories.messages import (
    create_user_message,
)

from .vcr_helpers import sanitize_response


@pytest_asyncio.fixture
async def retry_client_factory() -> Any:  # noqa: ANN401 # Factory returns different client types
    """Factory fixture for creating retrying LLM clients."""

    async def _create_client(
        primary_provider: str,
        primary_model: str,
        fallback_provider: str | None = None,
        fallback_model: str | None = None,
    ) -> LLMInterface:
        """Create a retrying LLM client for testing."""
        config = {
            "retry_config": {
                "primary": {
                    "provider": primary_provider,
                    "model": primary_model,
                    "api_key": "test-api-key",
                }
            }
        }

        if fallback_provider and fallback_model:
            config["retry_config"]["fallback"] = {
                "provider": fallback_provider,
                "model": fallback_model,
                "api_key": "test-fallback-key",
            }

        return LLMClientFactory.create_client(config)

    return _create_client


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_successful_primary_response(
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test that successful primary requests work without retry."""
    # Create a mock primary client that succeeds
    mock_primary = AsyncMock()
    mock_primary.generate_response = AsyncMock(
        return_value=LLMOutput(content="Primary response")
    )

    with patch(
        "family_assistant.llm.factory.LLMClientFactory._create_single_client",
        return_value=mock_primary,
    ):
        client = await retry_client_factory("test", "test-model")

        messages = [create_user_message("Test message")]
        response = await client.generate_response(messages)

        assert response.content == "Primary response"
        # Should only be called once - no retry
        assert mock_primary.generate_response.call_count == 1


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_retry_on_retriable_error(
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test that retriable errors trigger a retry on the primary model."""
    # Create a mock that fails once then succeeds
    mock_primary = AsyncMock()
    mock_primary.generate_response = AsyncMock(
        side_effect=[
            RateLimitError("Rate limit hit", provider="test", model="test-model"),
            LLMOutput(content="Success after retry"),
        ]
    )

    with patch(
        "family_assistant.llm.factory.LLMClientFactory._create_single_client",
        return_value=mock_primary,
    ):
        client = await retry_client_factory("test", "test-model")

        messages = [create_user_message("Test message")]
        response = await client.generate_response(messages)

        assert response.content == "Success after retry"
        # Should be called twice - original + retry
        assert mock_primary.generate_response.call_count == 2


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_fallback_after_primary_failures(
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test fallback to secondary model after primary failures."""
    # Create mocks for primary and fallback
    mock_primary = AsyncMock()
    mock_primary.generate_response = AsyncMock(
        side_effect=[
            ServiceUnavailableError("503 error", provider="test", model="test-model"),
            ServiceUnavailableError("503 error", provider="test", model="test-model"),
        ]
    )

    mock_fallback = AsyncMock()
    mock_fallback.generate_response = AsyncMock(
        return_value=LLMOutput(content="Fallback response")
    )

    # Mock the factory to return our mocks in order
    call_count = 0

    def mock_create_single(*args: Any, **kwargs: Any) -> LLMInterface:  # noqa: ANN401 # Mock function for testing
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_primary
        else:
            return mock_fallback

    with patch(
        "family_assistant.llm.factory.LLMClientFactory._create_single_client",
        side_effect=mock_create_single,
    ):
        client = await retry_client_factory(
            "test", "test-model", "fallback", "fallback-model"
        )

        messages = [create_user_message("Test message")]
        response = await client.generate_response(messages)

        assert response.content == "Fallback response"
        # Primary should be called twice (original + retry)
        assert mock_primary.generate_response.call_count == 2
        # Fallback should be called once
        assert mock_fallback.generate_response.call_count == 1


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_all_retries_exhausted(
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test that exceptions are raised when all retries are exhausted."""
    # Create a mock that always fails
    mock_primary = AsyncMock()
    mock_primary.generate_response = AsyncMock(
        side_effect=ProviderTimeoutError("Timeout", provider="test", model="test-model")
    )

    with patch(
        "family_assistant.llm.factory.LLMClientFactory._create_single_client",
        return_value=mock_primary,
    ):
        client = await retry_client_factory("test", "test-model")

        messages = [create_user_message("Test message")]

        with pytest.raises(ProviderTimeoutError) as exc_info:
            await client.generate_response(messages)

        assert "Timeout" in str(exc_info.value)
        # Should be called twice - original + retry
        assert mock_primary.generate_response.call_count == 2


@pytest.mark.no_db
@pytest.mark.llm_integration
async def test_non_retriable_error_goes_to_fallback(
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test that non-retriable errors skip retry and go to fallback."""

    # Create mocks
    mock_primary = AsyncMock()
    mock_primary.generate_response = AsyncMock(
        side_effect=InvalidRequestError(
            "Invalid request", provider="test", model="test-model"
        )
    )

    mock_fallback = AsyncMock()
    mock_fallback.generate_response = AsyncMock(
        return_value=LLMOutput(content="Fallback handled it")
    )

    call_count = 0

    def mock_create_single(*args: Any, **kwargs: Any) -> LLMInterface:  # noqa: ANN401 # Mock function for testing
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return mock_primary
        else:
            return mock_fallback

    with patch(
        "family_assistant.llm.factory.LLMClientFactory._create_single_client",
        side_effect=mock_create_single,
    ):
        client = await retry_client_factory(
            "test", "test-model", "fallback", "fallback-model"
        )

        messages = [create_user_message("Test message")]
        response = await client.generate_response(messages)

        assert response.content == "Fallback handled it"
        # Primary should only be called once - no retry for non-retriable errors
        assert mock_primary.generate_response.call_count == 1
        # Fallback should be called once
        assert mock_fallback.generate_response.call_count == 1


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.parametrize(
    "primary_provider,primary_model,fallback_provider,fallback_model",
    [
        ("openai", "gpt-4.1-nano", "google", "gemini-2.5-flash-lite"),
        ("google", "gemini-2.5-flash-lite", "openai", "gpt-4.1-nano"),
        ("google", "gemini-2.5-flash-lite", "openai", "gpt-5.2"),
    ],
)
async def test_real_provider_fallback(
    primary_provider: str,
    primary_model: str,
    fallback_provider: str,
    fallback_model: str,
    retry_client_factory: Any,  # noqa: ANN401 # Factory returns different client types
) -> None:
    """Test real provider fallback using VCR recordings."""
    # Skip if running in CI without API keys
    if os.getenv("CI") and (
        not os.getenv(f"{primary_provider.upper()}_API_KEY")
        or not os.getenv(f"{fallback_provider.upper()}_API_KEY")
    ):
        pytest.skip("Skipping test in CI without API keys")

    # For this test, we need real API keys
    primary_key = os.getenv(f"{primary_provider.upper()}_API_KEY", "test-key")
    fallback_key = os.getenv(f"{fallback_provider.upper()}_API_KEY", "test-key")

    config = {
        "retry_config": {
            "primary": {
                "provider": primary_provider,
                "model": primary_model,
                "api_key": primary_key,
            },
            "fallback": {
                "provider": fallback_provider,
                "model": fallback_model,
                "api_key": fallback_key,
            },
        }
    }

    # Add provider-specific config
    if primary_provider == "google":
        config["retry_config"]["primary"]["api_base"] = (
            "https://generativelanguage.googleapis.com/v1beta"
        )
    if fallback_provider == "google":
        config["retry_config"]["fallback"]["api_base"] = (
            "https://generativelanguage.googleapis.com/v1beta"
        )

    client = LLMClientFactory.create_client(config)

    messages = [create_user_message("Reply with: 'Primary response received'")]

    response = await client.generate_response(messages)

    # Should get a response (from either primary or fallback)
    assert response.content is not None
    assert len(response.content) > 0
