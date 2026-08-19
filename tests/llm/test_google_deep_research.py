"""Test Google Deep Research Agent integration."""

import importlib
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import TypeAdapter

from family_assistant.llm.base import (
    LLMProviderError,
    ModelNotFoundError,
    RateLimitError,
    ServiceUnavailableError,
)
from family_assistant.llm.google_types import GeminiProviderMetadata
from family_assistant.llm.messages import AssistantMessage, SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_antigravity_model,
    is_deep_research_model,
    is_interactions_agent_model,
)
from family_assistant.processing.protocol import (
    DelegationPermanentError,
    DelegationTaskNotFoundError,
    DelegationTransientError,
)

_SDK_INTERACTIONS_MODULE = importlib.import_module(
    "google.genai._gaos.resources.interactions"
)
_INTERACTION_SSE_EVENT_ADAPTER = TypeAdapter(
    _SDK_INTERACTIONS_MODULE.InteractionSSEEvent
)


def _sdk_interaction_event(payload: dict[str, object]) -> object:
    """Parse replay-style Interactions SSE JSON through the installed SDK."""
    return _INTERACTION_SSE_EVENT_ADAPTER.validate_python(payload)


@pytest.fixture
def mock_genai_client() -> Generator[MagicMock]:
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
    async def mock_stream_generator() -> AsyncGenerator[MagicMock]:
        # Yield start event
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_123"
        mock_start.event_id = None
        yield mock_start

        # Yield content event
        mock_content = MagicMock()
        mock_content.event_type = "step.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Researching..."
        mock_content.event_id = "evt_1"
        yield mock_content

        # Yield complete event
        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_2"
        yield mock_complete

    # create must be an awaitable that returns the generator
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_generator()
    )

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
    # previous_interaction_id should be absent if not provided
    assert "previous_interaction_id" not in call_kwargs
    # Deep Research has no sandbox, so it sends no environment -- the field is
    # required only by Antigravity, which always states its own.
    assert "environment" not in call_kwargs

    # Verify events
    # 1. Content event
    assert events[0].type == "content"
    assert events[0].content == "Researching..."

    # 2. Done event
    assert events[1].type == "done"
    assert events[1].metadata["provider_metadata"].interaction_id == "inter_123"


@pytest.mark.asyncio
async def test_deep_research_replays_interactions_2x_stream_schema(
    mock_genai_client: MagicMock,
) -> None:
    """Replay current Interactions SSE events through SDK parsing before client parsing."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    async def mock_sdk_replay_stream() -> AsyncGenerator[object]:
        for payload in [
            {
                "event_type": "interaction.created",
                "event_id": "evt_0",
                "interaction": {"id": "inter_2x", "status": "in_progress"},
            },
            {
                "event_type": "step.delta",
                "event_id": "evt_1",
                "index": 0,
                "delta": {"type": "text", "text": "Researching with 2.x"},
            },
            {
                "event_type": "step.delta",
                "event_id": "evt_2",
                "index": 1,
                "delta": {
                    "type": "thought_summary",
                    "content": {"type": "text", "text": "Checking sources"},
                },
            },
            {
                "event_type": "step.delta",
                "event_id": "evt_3",
                "index": 2,
                "delta": {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "redacted",
                },
            },
            {
                "event_type": "interaction.completed",
                "event_id": "evt_4",
                "interaction": {"id": "inter_2x", "status": "completed"},
            },
        ]:
            yield _sdk_interaction_event(payload)

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_sdk_replay_stream()
    )

    events = [
        event
        async for event in client.generate_response_stream([
            UserMessage(content="Replay Deep Research stream")
        ])
    ]

    assert [event.type for event in events] == ["content", "content", "done"]
    assert events[0].content == "Researching with 2.x"
    thinking_content = events[1].content
    assert thinking_content is not None
    assert "*Thinking: Checking sources*" in thinking_content
    done_event = events[2]
    metadata = done_event.metadata
    assert metadata is not None
    provider_metadata = metadata.get("provider_metadata")
    assert isinstance(provider_metadata, GeminiProviderMetadata)
    assert provider_metadata.interaction_id == "inter_2x"
    assert metadata.get("last_event_id") == "evt_4"
    reasoning_info = metadata.get("reasoning_info")
    assert reasoning_info is not None
    assert reasoning_info.get("thought_summaries") == [{"summary": "Checking sources"}]


@pytest.mark.asyncio
async def test_deep_research_continuation(mock_genai_client: MagicMock) -> None:
    """Test that deep research uses previous_interaction_id from history."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    # Mock stream response (minimal)
    async def mock_stream_generator() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_456"
        yield mock_start

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_generator()
    )

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

    async def mock_stream_generator() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_123"
        yield mock_start

        # Yield thought
        mock_thought = MagicMock()
        mock_thought.event_type = "step.delta"
        mock_thought.delta.type = "thought_summary"
        mock_thought.delta.content.text = "Thinking about query..."
        yield mock_thought

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_generator()
    )

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


@pytest.mark.asyncio
async def test_deep_research_pre_content_error_raises(
    mock_genai_client: MagicMock,
) -> None:
    """Pre-content errors should raise typed exceptions (enabling retry/fallback)."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    mock_genai_client.aio.interactions.create = AsyncMock(
        side_effect=Exception("429 Resource has been exhausted")
    )

    messages = [UserMessage(content="Research something")]

    with pytest.raises(RateLimitError):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_post_content_error_yields_error_and_done(
    mock_genai_client: MagicMock,
) -> None:
    """Post-content errors should yield error event with error_type, followed by done."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream_with_error() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_789"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_content = MagicMock()
        mock_content.event_type = "step.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Some research output"
        mock_content.event_id = "evt_1"
        yield mock_content

        err = Exception("Internal server error")
        err.status_code = 500  # type: ignore[attr-defined]  # Simulating SDK APIStatusError duck type
        raise err

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_with_error()
    )

    messages = [UserMessage(content="Research something")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    # Should get: content, error, done
    assert events[0].type == "content"
    assert events[0].content == "Some research output"

    assert events[1].type == "error"
    assert events[1].metadata["error_type"] == "ServiceUnavailableError"

    assert events[2].type == "done"
    assert events[2].metadata["provider_metadata"].interaction_id == "inter_789"


@pytest.mark.asyncio
async def test_deep_research_stream_error_event_maps_code(
    mock_genai_client: MagicMock,
) -> None:
    """ErrorEvent with error.code should be mapped to the correct typed exception."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream_with_error_event() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_abc"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_error = MagicMock()
        mock_error.event_type = "error"
        mock_error.event_id = "evt_1"
        mock_error.error.code = "resource_exhausted"
        mock_error.error.message = "Quota exceeded"
        yield mock_error

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_with_error_event()
    )

    messages = [UserMessage(content="Research something")]

    # No content yielded before error, so it should raise
    with pytest.raises(RateLimitError, match="Quota exceeded"):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_stream_error_event_post_content(
    mock_genai_client: MagicMock,
) -> None:
    """ErrorEvent after content should yield error with error_type metadata."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_abc"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_content = MagicMock()
        mock_content.event_type = "step.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Partial result"
        mock_content.event_id = "evt_1"
        yield mock_content

        mock_error = MagicMock()
        mock_error.event_type = "error"
        mock_error.event_id = "evt_2"
        mock_error.error.code = "deadline_exceeded"
        mock_error.error.message = "Request timed out"
        yield mock_error

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_3"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_stream())

    messages = [UserMessage(content="Research something")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    content_events = [e for e in events if e.type == "content"]
    error_events = [e for e in events if e.type == "error"]
    assert len(content_events) == 1
    assert len(error_events) == 1
    assert error_events[0].metadata["error_type"] == "ProviderTimeoutError"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["failed", "cancelled", "incomplete", "budget_exceeded"]
)
async def test_deep_research_terminal_status_update_raises_before_content(
    mock_genai_client: MagicMock,
    status: str,
) -> None:
    """Terminal non-success status updates before content should raise."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream_failed() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_fail"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_status = MagicMock()
        mock_status.event_type = "interaction.status_update"
        mock_status.event_id = "evt_1"
        mock_status.status = status
        mock_status.interaction = None
        yield mock_status

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_failed()
    )

    messages = [UserMessage(content="Research something")]

    with pytest.raises(ServiceUnavailableError, match=status):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "incomplete", "budget_exceeded"])
async def test_deep_research_terminal_status_update_yields_error_after_content(
    mock_genai_client: MagicMock,
    status: str,
) -> None:
    """Terminal non-success status updates after content should yield an error event."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_fail2"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_content = MagicMock()
        mock_content.event_type = "step.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Partial output"
        mock_content.event_id = "evt_1"
        yield mock_content

        mock_status = MagicMock()
        mock_status.event_type = "interaction.status_update"
        mock_status.event_id = "evt_2"
        mock_status.status = status
        mock_status.interaction = None
        yield mock_status

    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_stream())

    messages = [UserMessage(content="Research something")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    assert error_events[0].metadata["error_type"] == "ServiceUnavailableError"
    assert status in error_events[0].error


@pytest.mark.asyncio
async def test_deep_research_sdk_status_code_mapping(
    mock_genai_client: MagicMock,
) -> None:
    """SDK exceptions with status_code should be mapped to typed errors."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    # Create an exception that looks like an SDK APIStatusError (has status_code)
    sdk_error = Exception("The model is not found")
    sdk_error.status_code = 404  # type: ignore[attr-defined]  # Simulating SDK APIStatusError duck type

    mock_genai_client.aio.interactions.create = AsyncMock(side_effect=sdk_error)

    messages = [UserMessage(content="Research something")]

    with pytest.raises(ModelNotFoundError):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_done_event_has_last_event_id(
    mock_genai_client: MagicMock,
) -> None:
    """Done event should include last_event_id for stream reconnection."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_123"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_content = MagicMock()
        mock_content.event_type = "step.delta"
        mock_content.delta.type = "text"
        mock_content.delta.text = "Result"
        mock_content.event_id = "evt_42"
        yield mock_content

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_43"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_stream())

    messages = [UserMessage(content="Test")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    done_event = [e for e in events if e.type == "done"][0]
    assert done_event.metadata["last_event_id"] == "evt_43"


@pytest.mark.asyncio
async def test_deep_research_unknown_error_code_yields_generic(
    mock_genai_client: MagicMock,
) -> None:
    """Unknown error codes should produce LLMProviderError."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-pro-preview-12-2025"
    )

    async def mock_stream() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_abc"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_error = MagicMock()
        mock_error.event_type = "error"
        mock_error.event_id = "evt_1"
        mock_error.error.code = "some_unknown_code"
        mock_error.error.message = "Something went wrong"
        yield mock_error

    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_stream())

    messages = [UserMessage(content="Research something")]

    with pytest.raises(LLMProviderError, match="Something went wrong"):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.parametrize(
    "model_id",
    [
        "deep-research-pro-preview-12-2025",
        "deep-research-preview-04-2026",
        "deep-research-max-preview-04-2026",
    ],
)
def test_is_deep_research_model_matches_all_tiers(model_id: str) -> None:
    """Both the legacy preview and the 04-2026 regular/max tiers should route to deep research."""
    assert is_deep_research_model(model_id) is True
    assert is_interactions_agent_model(model_id) is True
    assert is_antigravity_model(model_id) is False


@pytest.mark.asyncio
async def test_deep_research_agent_config_includes_visualization(
    mock_genai_client: MagicMock,
) -> None:
    """agent_config should request visualizations alongside thought summaries."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-max-preview-04-2026"
    )

    async def mock_stream_generator() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_viz"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_1"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_generator()
    )

    async for _ in client.generate_response_stream([UserMessage(content="Test")]):
        pass

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["agent"] == "deep-research-max-preview-04-2026"
    assert call_kwargs["agent_config"] == {
        "type": "deep-research",
        "thinking_summaries": "auto",
        "visualization": "auto",
    }


@pytest.mark.asyncio
async def test_deep_research_image_delta_is_skipped(
    mock_genai_client: MagicMock,
) -> None:
    """Image deltas should be ignored cleanly without breaking the stream."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    async def mock_stream_generator() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = "inter_img"
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_image = MagicMock()
        mock_image.event_type = "step.delta"
        mock_image.delta.type = "image"
        mock_image.event_id = "evt_1"
        yield mock_image

        mock_text = MagicMock()
        mock_text.event_type = "step.delta"
        mock_text.delta.type = "text"
        mock_text.delta.text = "After image"
        mock_text.event_id = "evt_2"
        yield mock_text

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_3"
        yield mock_complete

    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=mock_stream_generator()
    )

    events = []
    async for event in client.generate_response_stream([UserMessage(content="Test")]):
        events.append(event)

    content_events = [e for e in events if e.type == "content"]
    assert len(content_events) == 1
    assert content_events[0].content == "After image"
    done_events = [e for e in events if e.type == "done"]
    assert len(done_events) == 1


# --- Pollable-delegation primitives (submit-then-poll, no streaming) ---


@pytest.mark.asyncio
async def test_start_agent_interaction_does_not_stream(
    mock_genai_client: MagicMock,
) -> None:
    """The non-blocking submit calls create with stream=False and returns immediately."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    mock_interaction = MagicMock()
    mock_interaction.id = "inter_submit_1"
    mock_interaction.status = "in_progress"
    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_interaction)

    result = await client.start_agent_interaction([
        UserMessage(content="Research quantum computing.")
    ])

    assert result is mock_interaction
    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["stream"] is False
    assert call_kwargs["background"] is True
    assert "Research quantum computing." in call_kwargs["input"]
    assert "previous_interaction_id" not in call_kwargs


@pytest.mark.asyncio
async def test_start_agent_interaction_passes_previous_interaction_id(
    mock_genai_client: MagicMock,
) -> None:
    """An explicit previous_interaction_id overrides history scanning."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    mock_interaction = MagicMock()
    mock_interaction.id = "inter_submit_2"
    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_interaction)

    await client.start_agent_interaction(
        [UserMessage(content="Follow-up question.")],
        previous_interaction_id="inter_chain_from",
    )

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["previous_interaction_id"] == "inter_chain_from"


@pytest.mark.asyncio
async def test_get_agent_interaction_polls_without_streaming(
    mock_genai_client: MagicMock,
) -> None:
    """A single poll calls interactions.get with stream=False."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    mock_interaction = MagicMock()
    mock_interaction.status = "completed"
    mock_interaction.output_text = "The final report."
    mock_genai_client.aio.interactions.get = AsyncMock(return_value=mock_interaction)

    result = await client.get_agent_interaction("inter_poll_1")

    assert result is mock_interaction
    mock_genai_client.aio.interactions.get.assert_called_once_with(
        "inter_poll_1", stream=False
    )


@pytest.mark.asyncio
async def test_cancel_agent_interaction_calls_cancel(
    mock_genai_client: MagicMock,
) -> None:
    """Cancellation delegates directly to the SDK's cancel endpoint."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    mock_genai_client.aio.interactions.cancel = AsyncMock(return_value=None)

    await client.cancel_agent_interaction("inter_cancel_1")

    mock_genai_client.aio.interactions.cancel.assert_called_once_with("inter_cancel_1")


@pytest.mark.asyncio
async def test_get_agent_interaction_maps_404_to_task_not_found(
    mock_genai_client: MagicMock,
) -> None:
    """A 404 on poll is a cue to re-submit, not a permanent failure."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk_error = Exception("no such interaction")
    sdk_error.status_code = 404  # type: ignore[attr-defined]
    mock_genai_client.aio.interactions.get = AsyncMock(side_effect=sdk_error)

    with pytest.raises(DelegationTaskNotFoundError):
        await client.get_agent_interaction("inter_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403])
async def test_start_agent_interaction_maps_4xx_to_permanent(
    mock_genai_client: MagicMock,
    status_code: int,
) -> None:
    """Bad-auth/bad-request submit failures fail fast rather than retry."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk_error = Exception("bad request")
    sdk_error.status_code = status_code  # type: ignore[attr-defined]
    mock_genai_client.aio.interactions.create = AsyncMock(side_effect=sdk_error)

    with pytest.raises(DelegationPermanentError):
        await client.start_agent_interaction([UserMessage(content="Test")])


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_get_agent_interaction_maps_429_5xx_to_transient(
    mock_genai_client: MagicMock,
    status_code: int,
) -> None:
    """Rate-limit/server errors on poll are retried, not failed."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk_error = Exception("try again")
    sdk_error.status_code = status_code  # type: ignore[attr-defined]
    mock_genai_client.aio.interactions.get = AsyncMock(side_effect=sdk_error)

    with pytest.raises(DelegationTransientError) as exc_info:
        await client.get_agent_interaction("inter_flaky")
    # A 4xx transient must not also satisfy the permanent subclass check.
    assert not isinstance(exc_info.value, DelegationPermanentError)


@pytest.mark.asyncio
async def test_get_agent_interaction_unrecognized_error_propagates_unwrapped(
    mock_genai_client: MagicMock,
) -> None:
    """An error with no recognizable status_code is not wrapped (fails fast by default)."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    original = ValueError("some unrelated bug")
    mock_genai_client.aio.interactions.get = AsyncMock(side_effect=original)

    with pytest.raises(ValueError, match="some unrelated bug"):
        await client.get_agent_interaction("inter_bug")
