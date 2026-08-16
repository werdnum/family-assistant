"""Test the Google Antigravity managed agent path through the Interactions API."""

import importlib
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import TypeAdapter

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import LLMMessage, SystemMessage, UserMessage
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_antigravity_model,
    is_deep_research_model,
    is_interactions_agent_model,
)

ANTIGRAVITY_AGENT_ID = "antigravity-preview-05-2026"

_SDK_INTERACTIONS_MODULE = importlib.import_module("google.genai._gaos.models")
_CREATE_INTERACTION_ADAPTER = TypeAdapter(
    _SDK_INTERACTIONS_MODULE.CreateInteractionRequestBody
)


@pytest.fixture
def mock_genai_client() -> Generator[MagicMock]:
    with patch(
        "family_assistant.llm.providers.google_genai_client.genai.Client"
    ) as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.aio = MagicMock()
        mock_instance.aio.interactions = MagicMock()
        yield mock_instance


async def _drain_stream(
    client: GoogleGenAIClient, messages: list[LLMMessage]
) -> list[LLMStreamEvent]:
    events = []
    async for event in client.generate_response_stream(messages):
        events.append(event)
    return events


def _completing_stream(interaction_id: str, text: str) -> AsyncGenerator[MagicMock]:
    async def generator() -> AsyncGenerator[MagicMock]:
        mock_start = MagicMock()
        mock_start.event_type = "interaction.created"
        mock_start.interaction.id = interaction_id
        mock_start.event_id = "evt_0"
        yield mock_start

        mock_text = MagicMock()
        mock_text.event_type = "step.delta"
        mock_text.delta.type = "text"
        mock_text.delta.text = text
        mock_text.event_id = "evt_1"
        yield mock_text

        mock_complete = MagicMock()
        mock_complete.event_type = "interaction.completed"
        mock_complete.event_id = "evt_2"
        yield mock_complete

    return generator()


@pytest.mark.parametrize(
    "model_id",
    [
        ANTIGRAVITY_AGENT_ID,
        "antigravity-preview-09-2027",
        # The client prefixes every configured model id with `models/`, so the
        # predicate has to match its own `model_name` too -- otherwise the
        # profile falls through to an ordinary chat completion.
        f"models/{ANTIGRAVITY_AGENT_ID}",
    ],
)
def test_antigravity_model_ids_route_to_the_agent_path(model_id: str) -> None:
    """The current preview id and any later antigravity revision both match."""
    assert is_antigravity_model(model_id) is True
    assert is_interactions_agent_model(model_id) is True
    assert is_deep_research_model(model_id) is False


def test_gemini_chat_model_is_not_an_interactions_agent() -> None:
    """The agent's own reasoning model, used directly, is an ordinary chat model."""
    assert is_interactions_agent_model("gemini-3.7-flash") is False


@pytest.mark.asyncio
async def test_antigravity_stream_sends_agent_config_and_system_instruction(
    mock_genai_client: MagicMock,
) -> None:
    """The interactive path submits the agent id, its model, and a separate prompt."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.7-flash",
    )
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_1", "Done.")
    )

    events = await _drain_stream(
        client,
        [
            SystemMessage(content="You are an autonomous task agent."),
            UserMessage(content="Chart the attached numbers."),
        ],
    )

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["agent"] == ANTIGRAVITY_AGENT_ID
    assert call_kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.7-flash",
    }
    assert call_kwargs["background"] is True
    assert call_kwargs["stream"] is True
    # The prompt rides in system_instruction, so the task the agent plans
    # against is only the user's request.
    assert call_kwargs["input"] == "Chart the attached numbers."
    assert "You are an autonomous task agent." in call_kwargs["system_instruction"]

    content_events = [e for e in events if e.type == "content"]
    assert [e.content for e in content_events] == ["Done."]


@pytest.mark.asyncio
async def test_antigravity_agent_config_omits_unset_fields(
    mock_genai_client: MagicMock,
) -> None:
    """Without configured overrides the API's own defaults are left to apply."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_2", "Done.")
    )

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["agent_config"] == {"type": "antigravity"}
    assert "system_instruction" not in call_kwargs


@pytest.mark.asyncio
async def test_antigravity_agent_config_carries_max_total_tokens(
    mock_genai_client: MagicMock,
) -> None:
    """A configured run budget reaches the API."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.7-flash",
        antigravity_max_total_tokens=250_000,
    )
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_3", "Done.")
    )

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.7-flash",
        "max_total_tokens": 250_000,
    }


def test_antigravity_create_kwargs_validate_against_the_sdk_request_model() -> None:
    """The submitted body is what the installed SDK accepts, not just a dict we like."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.7-flash",
        antigravity_max_total_tokens=250_000,
    )

    kwargs = client._build_agent_create_kwargs([
        SystemMessage(content="Be careful."),
        UserMessage(content="Do the thing."),
    ])

    body = _CREATE_INTERACTION_ADAPTER.validate_python({**kwargs, "stream": False})
    assert body.agent == ANTIGRAVITY_AGENT_ID
    assert body.agent_config.type == "antigravity"
    assert body.agent_config.model == "gemini-3.7-flash"
    assert body.agent_config.max_total_tokens == 250_000
    assert body.system_instruction is not None


def test_antigravity_requires_non_empty_input() -> None:
    """A system prompt alone is not a task for an agent that executes work."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)

    with pytest.raises(InvalidRequestError, match="Antigravity requires non-empty"):
        client._build_agent_create_kwargs([SystemMessage(content="Be careful.")])


@pytest.mark.asyncio
async def test_start_agent_interaction_submits_antigravity_without_streaming(
    mock_genai_client: MagicMock,
) -> None:
    """The submit-then-poll path reuses the same agent shaping with stream=False."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.7-flash",
    )
    mock_interaction = MagicMock()
    mock_interaction.id = "inter_ag_submit"
    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_interaction)

    result = await client.start_agent_interaction(
        [UserMessage(content="Do the thing.")],
        previous_interaction_id="inter_ag_prev",
    )

    assert result is mock_interaction
    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["stream"] is False
    assert call_kwargs["agent"] == ANTIGRAVITY_AGENT_ID
    assert call_kwargs["agent_config"]["model"] == "gemini-3.7-flash"
    assert call_kwargs["previous_interaction_id"] == "inter_ag_prev"
