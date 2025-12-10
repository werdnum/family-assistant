"""Unit tests for RetryingLLMClient streaming fallback."""

from unittest.mock import AsyncMock, MagicMock

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
    async def primary_stream(*args, **kwargs):
        raise RateLimitError("Rate limited", "test", "test-model")
        yield  # unreachable

    mock_primary_client.generate_response_stream = MagicMock(side_effect=primary_stream)

    # Setup fallback to succeed
    async def fallback_stream(*args, **kwargs):
        yield LLMStreamEvent(type="content", content="Fallback content")
        yield LLMStreamEvent(type="done", metadata={})

    mock_fallback_client.generate_response_stream = MagicMock(side_effect=fallback_stream)

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
    async def primary_stream(*args, **kwargs):
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
        async for event in client.generate_response_stream(messages):
            pass

    # Verify fallback was NOT used
    assert mock_primary_client.generate_response_stream.call_count == 1
    assert mock_fallback_client.generate_response_stream.call_count == 0
