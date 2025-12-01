from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litellm.exceptions import RateLimitError

from family_assistant.llm import LiteLLMClient, UserMessage


@pytest.mark.asyncio
async def test_generate_response_stream_fallback_on_429() -> None:
    """Test that streaming generation falls back to secondary model on 429 error."""
    # Setup
    primary_model = "google/gemini-pro"
    fallback_model = "openai/gpt-3.5-turbo"

    client = LiteLLMClient(model=primary_model, fallback_model_id=fallback_model)

    messages = [UserMessage(content="Hello")]

    # Mocking acompletion
    with patch(
        "family_assistant.llm.acompletion", new_callable=AsyncMock
    ) as mock_acompletion:
        # Define side effect for acompletion
        # First call (primary): raises RateLimitError
        # Second call (fallback): returns a stream

        # We need to simulate the stream iterator for the successful call
        async def async_iter(items: list[Any]) -> Any:  # noqa: ANN401
            for item in items:
                yield item

        # Successful chunk structure
        mock_chunk = MagicMock()
        mock_chunk.choices = [MagicMock()]
        mock_chunk.choices[0].delta.content = "Fallback response"
        mock_chunk.choices[0].delta.tool_calls = None

        # Configure side effect
        def side_effect(**kwargs: Any) -> Any:  # noqa: ANN401
            model = kwargs.get("model")
            if model == primary_model:
                raise RateLimitError(
                    "429 Too Many Requests", llm_provider="google", model=primary_model
                )
            elif model == fallback_model:
                return async_iter([mock_chunk])
            else:
                return async_iter([])

        mock_acompletion.side_effect = side_effect

        # Execute
        events = []
        async for event in client.generate_response_stream(messages):
            events.append(event)

        # Verify
        has_error = any(e.type == "error" for e in events)
        has_content = any(
            e.type == "content" and e.content == "Fallback response" for e in events
        )

        assert not has_error, "Should not have received an error event"
        assert has_content, "Should have received content from fallback model"

        # Verify calls
        assert (
            mock_acompletion.call_count >= 3
        )  # 1 primary, 1 retry primary, 1 fallback
