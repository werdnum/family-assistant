"""Unit tests for RetryingLLMClient."""

from unittest.mock import AsyncMock

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.base import (
    InvalidRequestError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.factories.messages import (
    create_user_message,
)


@pytest.fixture
def mock_primary_client() -> AsyncMock:
    """Create a mock primary LLM client."""
    mock = AsyncMock()
    mock.format_user_message_with_file = AsyncMock(
        return_value=create_user_message("test")
    )
    return mock


@pytest.fixture
def mock_fallback_client() -> AsyncMock:
    """Create a mock fallback LLM client."""
    mock = AsyncMock()
    mock.format_user_message_with_file = AsyncMock(
        return_value=create_user_message("test")
    )
    return mock


@pytest.mark.no_db
async def test_successful_primary_call(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test successful call to primary client."""
    mock_primary_client.generate_response = AsyncMock(
        return_value=LLMOutput(content="Primary success")
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Primary success"
    assert mock_primary_client.generate_response.call_count == 1
    assert mock_fallback_client.generate_response.call_count == 0


@pytest.mark.no_db
async def test_retry_on_connection_error(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test retry on connection error."""
    # Fail once, then succeed
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            ProviderConnectionError("Connection failed", "test", "test-model"),
            LLMOutput(content="Success after retry"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after retry"
    assert mock_primary_client.generate_response.call_count == 2
    assert mock_fallback_client.generate_response.call_count == 0


@pytest.mark.no_db
async def test_retry_on_timeout_error(mock_primary_client: AsyncMock) -> None:
    """Test retry on timeout error."""
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            ProviderTimeoutError("Timeout", "test", "test-model"),
            LLMOutput(content="Success after timeout"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=None,  # No fallback
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after timeout"
    assert mock_primary_client.generate_response.call_count == 2


@pytest.mark.no_db
async def test_retry_on_rate_limit(mock_primary_client: AsyncMock) -> None:
    """Test retry on rate limit error."""
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            RateLimitError("Rate limited", "test", "test-model"),
            LLMOutput(content="Success after rate limit"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after rate limit"
    assert mock_primary_client.generate_response.call_count == 2


@pytest.mark.no_db
async def test_retry_on_service_unavailable(mock_primary_client: AsyncMock) -> None:
    """Test retry on service unavailable error."""
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            ServiceUnavailableError("Service unavailable", "test", "test-model"),
            LLMOutput(content="Success after 503"),
        ]
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Success after 503"
    assert mock_primary_client.generate_response.call_count == 2


@pytest.mark.no_db
async def test_fallback_after_retry_failures(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test fallback after primary retries fail."""
    # Primary fails twice
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            RateLimitError("Rate limited", "test", "test-model"),
            RateLimitError("Still rate limited", "test", "test-model"),
        ]
    )

    # Fallback succeeds
    mock_fallback_client.generate_response = AsyncMock(
        return_value=LLMOutput(content="Fallback success")
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Fallback success"
    assert mock_primary_client.generate_response.call_count == 2
    assert mock_fallback_client.generate_response.call_count == 1


@pytest.mark.no_db
async def test_non_retriable_error_goes_to_fallback(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test non-retriable errors skip retry and go to fallback."""
    # Primary fails with non-retriable error
    mock_primary_client.generate_response = AsyncMock(
        side_effect=InvalidRequestError("Invalid request", "test", "test-model")
    )

    # Fallback succeeds
    mock_fallback_client.generate_response = AsyncMock(
        return_value=LLMOutput(content="Fallback handled it")
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]
    response = await client.generate_response(messages)

    assert response.content == "Fallback handled it"
    # Should only try primary once (no retry for non-retriable)
    assert mock_primary_client.generate_response.call_count == 1
    assert mock_fallback_client.generate_response.call_count == 1


@pytest.mark.no_db
async def test_all_attempts_fail(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test exception raised when all attempts fail."""
    # Primary fails twice
    primary_error = RateLimitError("Rate limited", "test", "test-model")
    mock_primary_client.generate_response = AsyncMock(side_effect=primary_error)

    # Fallback also fails
    mock_fallback_client.generate_response = AsyncMock(
        side_effect=ServiceUnavailableError(
            "Service down", "fallback", "fallback-model"
        )
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError) as exc_info:
        await client.generate_response(messages)

    # Should raise the primary error, not the fallback error
    assert exc_info.value is primary_error
    assert mock_primary_client.generate_response.call_count == 2
    assert mock_fallback_client.generate_response.call_count == 1


@pytest.mark.no_db
async def test_no_fallback_configured(mock_primary_client: AsyncMock) -> None:
    """Test behavior when no fallback is configured."""
    mock_primary_client.generate_response = AsyncMock(
        side_effect=RateLimitError("Rate limited", "test", "test-model")
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=None,  # No fallback
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError):
        await client.generate_response(messages)

    # Should try twice (original + retry)
    assert mock_primary_client.generate_response.call_count == 2


@pytest.mark.no_db
async def test_same_model_fallback_skipped(mock_primary_client: AsyncMock) -> None:
    """Test that fallback is skipped if it's the same model as primary."""
    mock_primary_client.generate_response = AsyncMock(
        side_effect=RateLimitError("Rate limited", "test", "test-model")
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_primary_client,  # Same client
        fallback_model="test-model",  # Same model
    )

    messages = [create_user_message("test")]

    with pytest.raises(RateLimitError):
        await client.generate_response(messages)

    # Should only try primary twice, no fallback attempt
    assert mock_primary_client.generate_response.call_count == 2


@pytest.mark.no_db
async def test_format_user_message_with_file(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that format_user_message_with_file delegates to primary."""
    expected_result = create_user_message("file content")
    mock_primary_client.format_user_message_with_file = AsyncMock(
        return_value=expected_result
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    result = await client.format_user_message_with_file(
        "prompt", "/path/to/file", "text/plain", 1000
    )

    assert result == expected_result
    assert mock_primary_client.format_user_message_with_file.call_count == 1
    assert mock_fallback_client.format_user_message_with_file.call_count == 0
