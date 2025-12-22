"""Test Google Deep Research Agent integration."""

from unittest.mock import MagicMock, patch

import pytest

from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import AssistantMessage, SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient


@pytest.fixture
def mock_genai_client() -> MagicMock:
    with patch(
        "family_assistant.llm.providers.google_genai_client.genai.Client"
    ) as mock_client:
        mock_instance = mock_client.return_value
        # Mock async client
        mock_instance.aio = MagicMock()
        mock_instance.aio.interactions = MagicMock()
        yield mock_instance


@pytest.mark.asyncio
async def test_deep_research_stream_initiation(mock_genai_client: MagicMock) -> None:
    """Test that deep research interaction is correctly initiated."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    # Mock stream response
    async def mock_stream_generator() -> MagicMock:
        # Yield start event
        mock_start = MagicMock()
        mock_start.event_type = "interaction.start"
        mock_start.interaction.id = "inter_123"
        mock_start.event_id = None
        yield mock_start

        # Yield content event
        mock_content = MagicMock()
        mock_content.event_type = "content.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Researching..."
        mock_content.event_id = "evt_1"
        yield mock_content

        # Yield complete event
        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.complete"
        mock_complete.event_id = "evt_2"
        yield mock_complete

    mock_genai_client.aio.interactions.create.return_value = mock_stream_generator()

    messages = [
        SystemMessage(content="You are a helpful researcher."),
        UserMessage(content="Research quantum computing."),
    ]

    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    # Verify interactions.create call
    mock_genai_client.aio.interactions.create.assert_called_once()
    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs

    # Check inputs
    assert "System: You are a helpful researcher." in call_kwargs["input"]
    assert "Research quantum computing." in call_kwargs["input"]
    assert call_kwargs["agent"] == "deep-research-pro-preview-12-2025"
    assert call_kwargs["background"] is True
    assert call_kwargs["stream"] is True
    assert call_kwargs["previous_interaction_id"] is None

    # Verify events
    # 1. Content event
    assert events[0].type == "content"
    assert events[0].content == "Researching..."

    # 2. Done event
    assert events[1].type == "done"
    assert events[1].metadata["provider_metadata"].interaction_id == "inter_123"


@pytest.mark.asyncio
async def test_deep_research_continuation(mock_genai_client: MagicMock) -> None:
    """Test that deep research uses previous_interaction_id from history."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    # Mock stream response (minimal)
    async def mock_stream_generator() -> MagicMock:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.start"
        mock_start.interaction.id = "inter_456"
        yield mock_start

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.complete"
        yield mock_complete

    mock_genai_client.aio.interactions.create.return_value = mock_stream_generator()

    # Setup history with previous interaction ID
    prev_metadata = GeminiProviderMetadata(interaction_id="inter_123")
    messages = [
        UserMessage(content="Start research."),
        AssistantMessage(content="Done.", provider_metadata=prev_metadata),
        UserMessage(content="Tell me more."),
    ]

    async for _ in client.generate_response_stream(messages):
        pass

    # Verify previous_interaction_id passed
    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["previous_interaction_id"] == "inter_123"
    assert "Tell me more." in call_kwargs["input"]


@pytest.mark.asyncio
async def test_deep_research_thought_summaries(mock_genai_client: MagicMock) -> None:
    """Test thought summaries are yielded as content."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream_generator() -> MagicMock:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.start"
        mock_start.interaction.id = "inter_123"
        yield mock_start

        # Yield thought
        mock_thought = MagicMock()
        mock_thought.event_type = "content.delta"
        mock_thought.delta.type = "thought_summary"
        mock_thought.delta.content.text = "Thinking about query..."
        yield mock_thought

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.complete"
        yield mock_complete

    mock_genai_client.aio.interactions.create.return_value = mock_stream_generator()

    messages = [UserMessage(content="Test")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    # Verify thought event
    assert events[0].type == "content"
    assert "*Thinking: Thinking about query...*" in events[0].content

    # Verify done event metadata has thoughts
    assert events[1].type == "done"
    assert (
        events[1].metadata["reasoning_info"]["thought_summaries"][0]["summary"]
        == "Thinking about query..."
    )
