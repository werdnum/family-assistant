"""Unit tests for RetryingLLMClient handling empty responses."""

from unittest.mock import AsyncMock

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.base import EmptyLLMResponseError
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.factories.messages import create_user_message

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
async def test_retry_on_empty_response(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test retry on empty response."""
    # First attempt returns empty response, second attempt succeeds
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            LLMOutput(content=None, tool_calls=None),
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
async def test_fallback_on_persistent_empty_response(
    mock_primary_client: AsyncMock, mock_fallback_client: AsyncMock
) -> None:
    """Test fallback when primary consistently returns empty response."""
    # Primary returns empty response twice
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            LLMOutput(content=None, tool_calls=None),
            LLMOutput(content=None, tool_calls=None),
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
