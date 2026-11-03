"""Test Google Deep Research Agent integration."""

import importlib
import json
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from google import genai
from google.genai import types
from google.genai.interactions import Interaction
from pydantic import TypeAdapter, ValidationError

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
_SDK_ASYNC_INTERACTIONS = type(genai.Client(api_key="test").aio.interactions)
# The client-facing environments resource forwards to this base, which
# declares the real keywords.
_SDK_ASYNC_ENVIRONMENTS_BASE = importlib.import_module(
    "google.genai._gaos.environments"
).AsyncEnvironments
_SDK_ENVIRONMENT_TYPES = importlib.import_module(
    "google.genai._gaos.types.environments.environment"
)


def _sdk_interaction_event(payload: dict[str, object]) -> object:
    """Parse replay-style Interactions SSE JSON through the installed SDK."""
    return _INTERACTION_SSE_EVENT_ADAPTER.validate_python(payload)


def _created(interaction_id: str, event_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "interaction.created",
        "interaction": {"id": interaction_id, "status": "in_progress"},
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


def _text_delta(text: str, event_id: str) -> dict[str, object]:
    return {
        "event_type": "step.delta",
        "event_id": event_id,
        "index": 0,
        "delta": {"type": "text", "text": text},
    }


def _completed(interaction_id: str, event_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_type": "interaction.completed",
        "interaction": {"id": interaction_id, "status": "completed"},
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


def _error(code: str, message: str, event_id: str) -> dict[str, object]:
    return {
        "event_type": "error",
        "event_id": event_id,
        "error": {"code": code, "message": message},
    }


def _status_update(
    interaction_id: str, status: str, event_id: str
) -> dict[str, object]:
    return {
        "event_type": "interaction.status_update",
        "interaction_id": interaction_id,
        "event_id": event_id,
        "status": status,
    }


def _sdk_stream(
    *payloads: dict[str, object], then_raise: Exception | None = None
) -> AsyncGenerator[object]:
    """An SSE stream of SDK-parsed events, optionally failing after the last one."""

    async def generator() -> AsyncGenerator[object]:
        for payload in payloads:
            yield _sdk_interaction_event(payload)
        if then_raise is not None:
            raise then_raise

    return generator()


def _sdk_status_error(message: str, status_code: int) -> Exception:
    error = Exception(message)
    error.status_code = status_code  # type: ignore[attr-defined]  # Simulating SDK APIStatusError duck type
    return error


@dataclass(frozen=True)
class _SdkInteractions:
    """The installed SDK's async interactions methods, autospecced on their class.

    A call records the resource instance as its first positional argument, and
    a keyword this SDK version does not take fails here rather than in
    production.
    """

    create: AsyncMock
    get: AsyncMock
    cancel: AsyncMock


@pytest.fixture
def sdk() -> Generator[_SdkInteractions]:
    with (
        patch.object(_SDK_ASYNC_INTERACTIONS, "create", autospec=True) as create,
        patch.object(_SDK_ASYNC_INTERACTIONS, "get", autospec=True) as get,
        patch.object(_SDK_ASYNC_INTERACTIONS, "cancel", autospec=True) as cancel,
    ):
        yield _SdkInteractions(create=create, get=get, cancel=cancel)


@pytest.mark.asyncio
async def test_deep_research_stream_initiation(sdk: _SdkInteractions) -> None:
    """Test that deep research interaction is correctly initiated."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_123"),
        _text_delta("Researching...", "evt_1"),
        _completed("inter_123", "evt_2"),
    )

    messages = [
        SystemMessage(content="You are a helpful researcher."),
        UserMessage(content="Research quantum computing."),
    ]

    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    sdk.create.assert_called_once()
    assert sdk.create.call_args.args == (client.client.aio.interactions,)
    call_kwargs = sdk.create.call_args.kwargs

    assert "System: You are a helpful researcher." in call_kwargs["input"]
    assert "Research quantum computing." in call_kwargs["input"]
    assert call_kwargs["agent"] == "deep-research-preview-04-2026"
    assert call_kwargs["background"] is True
    assert call_kwargs["stream"] is True
    assert "previous_interaction_id" not in call_kwargs
    # Deep Research has no sandbox, so it sends no environment -- the field is
    # required only by Antigravity, which always states its own.
    assert "environment" not in call_kwargs

    assert events[0].type == "content"
    assert events[0].content == "Researching..."

    assert events[1].type == "done"
    assert events[1].metadata["provider_metadata"].interaction_id == "inter_123"


@pytest.mark.asyncio
async def test_deep_research_replays_interactions_2x_stream_schema(
    sdk: _SdkInteractions,
) -> None:
    """Replay current Interactions SSE events through SDK parsing before client parsing."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
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
    # The agent read the web server-side, so the response declares it for taint.
    external_read = metadata.get("provider_external_read")
    assert external_read is not None
    assert external_read["source_id"] == "deep_research:inter_2x"


@pytest.mark.asyncio
async def test_deep_research_continuation(sdk: _SdkInteractions) -> None:
    """Test that deep research uses previous_interaction_id from history."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_456"), _completed("inter_456")
    )

    prev_metadata = GeminiProviderMetadata(interaction_id="inter_123")
    messages = [
        UserMessage(content="Start research."),
        AssistantMessage(content="Done.", provider_metadata=prev_metadata),
        UserMessage(content="Tell me more."),
    ]

    async for _ in client.generate_response_stream(messages):
        pass

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["previous_interaction_id"] == "inter_123"
    assert "Tell me more." in call_kwargs["input"]


@pytest.mark.asyncio
async def test_deep_research_pre_content_error_raises(
    sdk: _SdkInteractions,
) -> None:
    """Pre-content errors should raise typed exceptions (enabling retry/fallback)."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.side_effect = Exception("429 Resource has been exhausted")

    messages = [UserMessage(content="Research something")]

    with pytest.raises(RateLimitError):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_post_content_error_yields_error_and_done(
    sdk: _SdkInteractions,
) -> None:
    """Post-content errors should yield error event with error_type, followed by done."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_789", "evt_0"),
        _text_delta("Some research output", "evt_1"),
        then_raise=_sdk_status_error("Internal server error", 500),
    )

    messages = [UserMessage(content="Research something")]
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)

    assert events[0].type == "content"
    assert events[0].content == "Some research output"

    assert events[1].type == "error"
    assert events[1].metadata["error_type"] == "ServiceUnavailableError"

    assert events[2].type == "done"
    assert events[2].metadata["provider_metadata"].interaction_id == "inter_789"
    assert events[2].metadata["last_event_id"] == "evt_1"


@pytest.mark.asyncio
async def test_deep_research_stream_error_event_maps_code(
    sdk: _SdkInteractions,
) -> None:
    """ErrorEvent with error.code should be mapped to the correct typed exception."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_abc", "evt_0"),
        _error("resource_exhausted", "Quota exceeded", "evt_1"),
    )

    messages = [UserMessage(content="Research something")]

    # No content yielded before error, so it should raise
    with pytest.raises(RateLimitError, match="Quota exceeded"):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_stream_error_event_post_content(
    sdk: _SdkInteractions,
) -> None:
    """ErrorEvent after content should yield error with error_type metadata."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_abc", "evt_0"),
        _text_delta("Partial result", "evt_1"),
        _error("deadline_exceeded", "Request timed out", "evt_2"),
        _completed("inter_abc", "evt_3"),
    )

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
    sdk: _SdkInteractions,
    status: str,
) -> None:
    """Terminal non-success status updates before content should raise."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_fail", "evt_0"),
        _status_update("inter_fail", status, "evt_1"),
    )

    messages = [UserMessage(content="Research something")]

    with pytest.raises(ServiceUnavailableError, match=status):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "incomplete", "budget_exceeded"])
async def test_deep_research_terminal_status_update_yields_error_after_content(
    sdk: _SdkInteractions,
    status: str,
) -> None:
    """Terminal non-success status updates after content should yield an error event."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_fail2", "evt_0"),
        _text_delta("Partial output", "evt_1"),
        _status_update("inter_fail2", status, "evt_2"),
    )

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
    sdk: _SdkInteractions,
) -> None:
    """SDK exceptions with status_code should be mapped to typed errors."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.side_effect = _sdk_status_error("The model is not found", 404)

    messages = [UserMessage(content="Research something")]

    with pytest.raises(ModelNotFoundError):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.asyncio
async def test_deep_research_unknown_error_code_yields_generic(
    sdk: _SdkInteractions,
) -> None:
    """Unknown error codes should produce LLMProviderError."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = _sdk_stream(
        _created("inter_abc", "evt_0"),
        _error("some_unknown_code", "Something went wrong", "evt_1"),
    )

    messages = [UserMessage(content="Research something")]

    with pytest.raises(LLMProviderError, match="Something went wrong"):
        async for _ in client.generate_response_stream(messages):
            pass


@pytest.mark.parametrize(
    "model_id",
    [
        "deep-research-preview-04-2026",
        "deep-research-max-preview-04-2026",
    ],
)
def test_is_deep_research_model_matches_all_tiers(model_id: str) -> None:
    """Both the 04-2026 regular and max tiers should route to deep research."""
    assert is_deep_research_model(model_id) is True
    assert is_interactions_agent_model(model_id) is True
    assert is_antigravity_model(model_id) is False


@pytest.mark.asyncio
async def test_deep_research_agent_config_includes_visualization(
    sdk: _SdkInteractions,
) -> None:
    """agent_config should request visualizations alongside thought summaries."""
    client = GoogleGenAIClient(
        api_key="test", model="deep-research-max-preview-04-2026"
    )

    sdk.create.return_value = _sdk_stream(
        _created("inter_viz", "evt_0"), _completed("inter_viz", "evt_1")
    )

    async for _ in client.generate_response_stream([UserMessage(content="Test")]):
        pass

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["agent"] == "deep-research-max-preview-04-2026"
    assert call_kwargs["agent_config"] == {
        "type": "deep-research",
        "thinking_summaries": "auto",
        "visualization": "auto",
    }


# --- Pollable-delegation primitives (submit-then-poll, no streaming) ---


@pytest.mark.asyncio
async def test_start_agent_interaction_does_not_stream(
    sdk: _SdkInteractions,
) -> None:
    """The non-blocking submit calls create with stream=False and returns immediately."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    submitted = Interaction(id="inter_submit_1", status="in_progress")
    sdk.create.return_value = submitted

    result = await client.start_agent_interaction([
        UserMessage(content="Research quantum computing.")
    ])

    assert result is submitted
    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["stream"] is False
    assert call_kwargs["background"] is True
    assert "Research quantum computing." in call_kwargs["input"]
    assert "previous_interaction_id" not in call_kwargs


@pytest.mark.asyncio
async def test_start_agent_interaction_passes_previous_interaction_id(
    sdk: _SdkInteractions,
) -> None:
    """An explicit previous_interaction_id overrides history scanning."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.return_value = Interaction(id="inter_submit_2", status="in_progress")

    await client.start_agent_interaction(
        [UserMessage(content="Follow-up question.")],
        previous_interaction_id="inter_chain_from",
    )

    call_kwargs = sdk.create.call_args.kwargs
    assert call_kwargs["previous_interaction_id"] == "inter_chain_from"


@pytest.mark.asyncio
async def test_get_agent_interaction_polls_without_streaming(
    sdk: _SdkInteractions,
) -> None:
    """A single poll calls interactions.get with stream=False."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    polled = Interaction(status="completed", output_text="The final report.")
    sdk.get.return_value = polled

    result = await client.get_agent_interaction("inter_poll_1")

    assert result is polled
    sdk.get.assert_called_once_with(
        client.client.aio.interactions, "inter_poll_1", stream=False
    )


@pytest.mark.asyncio
async def test_get_agent_interaction_validates_mapping_response(
    sdk: _SdkInteractions,
) -> None:
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")
    sdk.get.return_value = {
        "status": "failed",
        "errors": [{"code": "limit", "message": "Capacity exceeded"}],
        "usage": {"total_tokens": 12},
    }

    result = await client.get_agent_interaction("inter_poll_1")

    assert isinstance(result, Interaction)
    assert result.status == "failed"
    assert result.errors is not None and result.errors[0].code == "limit"
    assert result.usage is not None and result.usage.total_tokens == 12


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        [{"FA_GITHUB_GIT_AUTH": {"credential": "stored-git-credential"}}],
        {"FA_GITHUB_GIT_AUTH": {"credential": "stored-git-credential"}},
    ],
)
async def test_agent_response_ignores_unused_environment(
    sdk: _SdkInteractions,
    env: object,
) -> None:
    client = GoogleGenAIClient(api_key="test", model="antigravity-preview-09-2026")
    response = {
        "id": "inter_env",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "The remote report."}],
            }
        ],
        "usage": {"total_tokens": 12},
        "environment": {"type": "remote", "env": env},
    }
    sdk.get.return_value = response
    sdk.create.return_value = response

    # A submitted sandbox run is given an environment of its own first.
    with patch.object(
        _SDK_ASYNC_ENVIRONMENTS_BASE, "create_environment", autospec=True
    ) as create_environment:
        create_environment.return_value = _SDK_ENVIRONMENT_TYPES.Environment(id="env_1")
        results = (
            await client.get_agent_interaction("inter_env"),
            await client.start_agent_interaction([UserMessage(content="Print 1.")]),
        )

    for result in results:
        assert result.id == "inter_env"
        assert result.status == "completed"
        assert result.output_text == "The remote report."
        assert result.usage is not None and result.usage.total_tokens == 12
        assert result.environment is None
    assert response["environment"] == {"type": "remote", "env": env}


@pytest.mark.asyncio
async def test_poll_validation_failure_propagates(
    sdk: _SdkInteractions,
) -> None:
    client = GoogleGenAIClient(api_key="test", model="antigravity-preview-09-2026")
    sdk.get.return_value = {
        "id": "inter_bad",
        "status": "completed",
        "usage": {"total_tokens": []},
    }
    with pytest.raises(ValidationError, match="usage.total_tokens"):
        await client.get_agent_interaction("inter_bad")


@pytest.mark.asyncio
async def test_sdk_poll_parse_failure_propagates(
    sdk: _SdkInteractions,
) -> None:
    client = GoogleGenAIClient(api_key="test", model="antigravity-preview-09-2026")
    original = json.JSONDecodeError("Invalid response", "", 0)
    sdk.get.side_effect = original
    with pytest.raises(json.JSONDecodeError) as exc_info:
        await client.get_agent_interaction("inter_bad")
    assert exc_info.value is original


@pytest.mark.asyncio
async def test_cancel_agent_interaction_calls_cancel(
    sdk: _SdkInteractions,
) -> None:
    """Cancellation delegates directly to the SDK's cancel endpoint."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.cancel.return_value = None

    await client.cancel_agent_interaction("inter_cancel_1")

    sdk.cancel.assert_called_once_with(client.client.aio.interactions, "inter_cancel_1")


@pytest.mark.asyncio
async def test_get_agent_interaction_maps_404_to_task_not_found(
    sdk: _SdkInteractions,
) -> None:
    """A 404 on poll is a cue to re-submit, not a permanent failure."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.get.side_effect = _sdk_status_error("no such interaction", 404)

    with pytest.raises(DelegationTaskNotFoundError):
        await client.get_agent_interaction("inter_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403])
async def test_start_agent_interaction_maps_4xx_to_permanent(
    sdk: _SdkInteractions,
    status_code: int,
) -> None:
    """Bad-auth/bad-request submit failures fail fast rather than retry."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.create.side_effect = _sdk_status_error("bad request", status_code)

    with pytest.raises(DelegationPermanentError):
        await client.start_agent_interaction([UserMessage(content="Test")])


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_get_agent_interaction_maps_429_5xx_to_transient(
    sdk: _SdkInteractions,
    status_code: int,
) -> None:
    """Rate-limit/server errors on poll are retried, not failed."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.get.side_effect = _sdk_status_error("try again", status_code)

    with pytest.raises(DelegationTransientError) as exc_info:
        await client.get_agent_interaction("inter_flaky")
    # A 4xx transient must not also satisfy the permanent subclass check.
    assert not isinstance(exc_info.value, DelegationPermanentError)


@pytest.mark.asyncio
async def test_get_agent_interaction_unrecognized_error_propagates_unwrapped(
    sdk: _SdkInteractions,
) -> None:
    """An error with no recognizable status_code is not wrapped (fails fast by default)."""
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")

    sdk.get.side_effect = ValueError("some unrelated bug")

    with pytest.raises(ValueError, match="some unrelated bug"):
        await client.get_agent_interaction("inter_bug")


@pytest.mark.asyncio
async def test_poll_through_real_sdk_response_parser() -> None:
    response = {
        "id": "inter_sdk",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": "Recovered report."}],
            }
        ],
        "environment": {
            "type": "remote",
            "env": [
                {"FA_GITHUB_GIT_AUTH": {"credential": "stored-git-credential"}},
            ],
        },
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response, request=request)

    client = GoogleGenAIClient(api_key="test", model="antigravity-preview-09-2026")
    await client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client.client = genai.Client(
            api_key="test", http_options=types.HttpOptions(httpx_async_client=transport)
        )
        try:
            result = await client.get_agent_interaction("inter_sdk")
            assert result.status == "completed"
            assert result.output_text == "Recovered report."
        finally:
            await client.close()
