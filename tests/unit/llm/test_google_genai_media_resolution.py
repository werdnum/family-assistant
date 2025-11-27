from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient

class TestMediaResolution:
    """Tests for media resolution configuration."""

    @pytest.fixture
    def google_client(self) -> GoogleGenAIClient:
        """Create a GoogleGenAIClient instance for testing."""
        return GoogleGenAIClient(
            api_key="test_key_for_unit_tests", model="gemini-2.5-pro"
        )

    async def test_generate_response_uses_high_resolution(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that generate_response sets MEDIA_RESOLUTION_HIGH."""
        # Mock the API call
        mock_response = MagicMock()
        mock_response.text = "Response"

        # Add candidates to avoid UnboundLocalError in the client code
        mock_candidate = MagicMock()
        # Mock content.parts to be empty or contain text so it doesn't crash on iteration
        mock_candidate.content.parts = []
        mock_response.candidates = [mock_candidate]

        mock_response.usage_metadata = None

        google_client.client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        messages = [UserMessage(role="user", content="Hello")]

        await google_client.generate_response(messages)

        # Check the config passed to the API
        call_args = google_client.client.aio.models.generate_content.call_args
        assert call_args is not None

        kwargs = call_args.kwargs
        config = kwargs.get("config")

        assert config is not None
        assert isinstance(config, types.GenerateContentConfig)
        assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH

    async def test_generate_response_stream_uses_high_resolution(
        self, google_client: GoogleGenAIClient
    ) -> None:
        """Test that generate_response_stream sets MEDIA_RESOLUTION_HIGH."""
        # Mock the API call
        async def mock_stream():
            chunk = MagicMock()
            chunk.text = "Response"
            chunk.candidates = []
            yield chunk

        google_client.client.aio.models.generate_content_stream = AsyncMock(
            return_value=mock_stream()
        )

        messages = [UserMessage(role="user", content="Hello")]

        async for _ in google_client.generate_response_stream(messages):
            pass

        # Check the config passed to the API
        call_args = google_client.client.aio.models.generate_content_stream.call_args
        assert call_args is not None

        kwargs = call_args.kwargs
        config = kwargs.get("config")

        assert config is not None
        assert isinstance(config, types.GenerateContentConfig)
        assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
