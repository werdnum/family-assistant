"""Unit tests for RetryingLLMClient streaming fallback."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.base import (
    RateLimitError,
)
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.factories.messages import (
    create_user_message,
)


@pytest.fixture
def mock_primary_client() -> AsyncMock:
    """Create a mock primary LLM client."""
    mock = AsyncMock()
    return mock


@pytest.fixture
def mock_fallback_client() -> AsyncMock:
    """Create a mock fallback LLM client."""
    mock = AsyncMock()
    return mock


@pytest.mark.no_db
async def test_streaming_fallback_on_immediate_error(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that streaming falls back to secondary model if primary fails immediately."""

    # Setup primary to fail immediately
    async def primary_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise RateLimitError("Rate limited", "test", "test-model")
        yield  # unreachable

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    # Setup fallback to succeed
    async def fallback_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        yield LLMStreamEvent(type="content", content="Fallback content")
        yield LLMStreamEvent(type="done", metadata={})

    mock_fallback_client.generate_response_stream = MagicMock(
        side_effect=fallback_stream
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    # Collect events
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    # Verify fallback was used
    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"

    # Verify call counts
    assert mock_primary_client.generate_response_stream.call_count == 1
    assert mock_fallback_client.generate_response_stream.call_count == 1


@pytest.mark.no_db
async def test_streaming_no_fallback_if_content_yielded(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that streaming does NOT fall back if primary yielded content before failing."""

    # Setup primary to yield content then fail
    async def primary_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        yield LLMStreamEvent(type="content", content="Primary content")
        raise RateLimitError("Rate limited mid-stream", "test", "test-model")

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    # Expect exception to propagate
    with pytest.raises(RateLimitError):
        async for _event in client.generate_response_stream(messages):
            pass

    # Verify fallback was NOT used
    assert mock_primary_client.generate_response_stream.call_count == 1
    assert mock_fallback_client.generate_response_stream.call_count == 0


@pytest.mark.no_db
async def test_streaming_rate_limit_retry_with_delay(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that rate limit with retry_after <= 60s triggers a delayed retry on primary."""
    call_count = 0

    async def primary_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimitError("Rate limited", "test", "test-model", retry_after=5)
            yield  # unreachable, makes this an async generator
        yield LLMStreamEvent(type="content", content="Retry succeeded")
        yield LLMStreamEvent(type="done", metadata={})

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        events = []
        async for event in client.generate_response_stream(messages):
            events.append(event)

        mock_sleep.assert_awaited_once_with(5)

    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Retry succeeded"
    assert mock_primary_client.generate_response_stream.call_count == 2
    assert mock_fallback_client.generate_response_stream.call_count == 0


@pytest.mark.no_db
async def test_streaming_rate_limit_retry_fails_then_fallback(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that if delayed retry also fails, falls through to fallback."""

    async def primary_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise RateLimitError("Rate limited", "test", "test-model", retry_after=5)
        yield  # unreachable

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    async def fallback_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        yield LLMStreamEvent(type="content", content="Fallback content")
        yield LLMStreamEvent(type="done", metadata={})

    mock_fallback_client.generate_response_stream = MagicMock(
        side_effect=fallback_stream
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    with patch("asyncio.sleep", new_callable=AsyncMock):
        events = []
        async for event in client.generate_response_stream(messages):
            events.append(event)

    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"
    assert mock_primary_client.generate_response_stream.call_count == 2
    assert mock_fallback_client.generate_response_stream.call_count == 1


@pytest.mark.no_db
async def test_streaming_rate_limit_no_retry_if_retry_after_too_long(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test that rate limit with retry_after > 60s skips delay retry and goes to fallback."""

    async def primary_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        raise RateLimitError("Rate limited", "test", "test-model", retry_after=120)
        yield  # unreachable

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    async def fallback_stream(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        yield LLMStreamEvent(type="content", content="Fallback content")
        yield LLMStreamEvent(type="done", metadata={})

    mock_fallback_client.generate_response_stream = MagicMock(
        side_effect=fallback_stream
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
        fallback_client=mock_fallback_client,
        fallback_model="fallback-model",
    )

    messages = [create_user_message("test")]

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        events = []
        async for event in client.generate_response_stream(messages):
            events.append(event)

        mock_sleep.assert_not_awaited()

    assert len(events) == 2
    assert events[0].type == "content"
    assert events[0].content == "Fallback content"
    assert mock_primary_client.generate_response_stream.call_count == 1
    assert mock_fallback_client.generate_response_stream.call_count == 1
