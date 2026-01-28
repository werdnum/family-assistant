"""Unit tests for RetryingLLMClient handling of empty responses."""

from unittest.mock import AsyncMock

import pytest

from family_assistant.llm import LLMOutput
from family_assistant.llm.messages import UserMessage
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


@pytest.mark.no_db
async def test_empty_response_triggers_reprompt(
    mock_primary_client: AsyncMock,
) -> None:
    """Test that an empty response triggers a re-prompt with additional message."""
    # First response is empty, second response is valid
    mock_primary_client.generate_response = AsyncMock(
        side_effect=[
            LLMOutput(content=None, tool_calls=None),  # Empty response
            LLMOutput(content="Success after re-prompt"),  # Valid response
        ]
    )

    client = RetryingLLMClient(
        primary_client=mock_primary_client,
        primary_model="test-model",
    )

    messages = [create_user_message("test")]

    response = await client.generate_response(messages)

    # Expected behavior after fix:
    assert response.content == "Success after re-prompt"
    assert mock_primary_client.generate_response.call_count == 2

    # Verify the second call had the re-prompt
    second_call_args = mock_primary_client.generate_response.call_args_list[1]
    # Check args/kwargs. generate_response(messages=..., ...)
    # If called as positional args, it's args[0]
    # If called as kwargs, it's kwargs['messages']
    # The client calls: return await self.primary_client.generate_response(messages=messages, ...)
    # So it should be in kwargs.
    call_messages = second_call_args.kwargs["messages"]

    assert len(call_messages) == len(messages) + 1
    assert isinstance(call_messages[-1], UserMessage)
    assert "empty response" in call_messages[-1].content.lower()
