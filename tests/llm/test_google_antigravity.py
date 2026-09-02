"""Test the Google Antigravity managed agent path through the Interactions API."""

import importlib
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import TypeAdapter

from family_assistant.llm import LLMStreamEvent
from family_assistant.llm.antigravity_egress import EgressNetworkPayload
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    ImageUrlContentPart,
    LLMMessage,
    SystemMessage,
    TextContentPart,
    UserMessage,
)
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
    assert is_interactions_agent_model("gemini-3.8-flash") is False


@pytest.mark.asyncio
async def test_antigravity_stream_sends_agent_config_and_system_instruction(
    mock_genai_client: MagicMock,
) -> None:
    """The interactive path submits the agent id, its model, and a separate prompt."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
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
        "model": "gemini-3.8-flash",
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
        antigravity_model="gemini-3.8-flash",
        antigravity_max_total_tokens=250_000,
    )
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_3", "Done.")
    )

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["agent_config"] == {
        "type": "antigravity",
        "model": "gemini-3.8-flash",
        "max_total_tokens": 250_000,
    }


def test_antigravity_create_kwargs_validate_against_the_sdk_request_model() -> None:
    """The submitted body is what the installed SDK accepts, not just a dict we like."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_max_total_tokens=250_000,
    )

    kwargs = client._build_agent_create_kwargs([
        SystemMessage(content="Be careful."),
        UserMessage(content="Do the thing."),
    ])

    body = _CREATE_INTERACTION_ADAPTER.validate_python({**kwargs, "stream": False})
    assert body.agent == ANTIGRAVITY_AGENT_ID
    assert body.agent_config.type == "antigravity"
    assert body.agent_config.model == "gemini-3.8-flash"
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
        antigravity_model="gemini-3.8-flash",
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
    assert call_kwargs["agent_config"]["model"] == "gemini-3.8-flash"
    assert call_kwargs["previous_interaction_id"] == "inter_ag_prev"


def test_media_injected_as_provider_parts_is_refused_not_dropped() -> None:
    """An image or PDF injection has no text form the agent could receive.

    The Google adapter injects multimodal attachments as provider `parts`,
    which this path cannot serialize into the single `input` string — so the
    turn fails rather than asking the agent about a file it never got.
    """
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)
    injected = UserMessage(content="[System: File from previous tool response]")
    injected.parts = [{"inline_data": {"mime_type": "image/png"}}]

    with pytest.raises(InvalidRequestError, match="cannot read attachments"):
        client._build_agent_create_kwargs([
            UserMessage(content="Crop the attached image."),
            injected,
        ])


def test_image_url_content_part_is_refused_not_dropped() -> None:
    """The same holds for an image_url part reaching the agent path."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)

    with pytest.raises(InvalidRequestError, match="cannot read image_url"):
        client._build_agent_create_kwargs([
            UserMessage(
                content=[
                    TextContentPart(type="text", text="What is in this picture?"),
                    ImageUrlContentPart(
                        type="image_url", image_url={"url": "https://example.com/a.png"}
                    ),
                ]
            )
        ])


def test_text_shaped_attachment_injection_still_reaches_the_agent() -> None:
    """A CSV/JSON/text attachment is injected as text, so it must still pass."""
    client = GoogleGenAIClient(api_key="test", model=ANTIGRAVITY_AGENT_ID)

    kwargs = client._build_agent_create_kwargs([
        UserMessage(content="Transform the attached CSV."),
        UserMessage(content="[System: File from previous tool response]\na,b\n1,2"),
    ])

    assert "Transform the attached CSV." in kwargs["input"]
    assert "a,b" in kwargs["input"]


def _egress_client(network: EgressNetworkPayload) -> GoogleGenAIClient:
    """A client whose egress resolver returns a fixed network payload."""

    class _FixedResolver:
        async def resolve_network(self) -> EgressNetworkPayload | None:
            return network

    return GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
        antigravity_egress_resolver=_FixedResolver(),
    )


@pytest.mark.asyncio
async def test_egress_policy_reaches_the_interactive_path(
    mock_genai_client: MagicMock,
) -> None:
    """The stream path previously had nowhere to put an environment at all.

    Egress policy is static profile config rather than per-turn data, so a
    credential configured for `/coder` has to apply when a user runs the slash
    command directly, not only when a delegation submits.
    """
    client = _egress_client({
        "allowlist": [
            {"domain": "*"},
            {
                "domain": "api.github.com",
                "transform": [{"Authorization": "Bearer ghs_x"}],
            },
        ]
    })
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_env", "Done.")
    )

    await _drain_stream(client, [UserMessage(content="Open a PR.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["environment"] == {
        "type": "remote",
        "network": {
            "allowlist": [
                {"domain": "*"},
                {
                    "domain": "api.github.com",
                    "transform": [{"Authorization": "Bearer ghs_x"}],
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_submit_path_merges_mounted_sources_with_egress_policy(
    mock_genai_client: MagicMock,
) -> None:
    """One environment block carries both, rather than either overwriting the other."""
    client = _egress_client("disabled")
    mock_interaction = MagicMock()
    mock_interaction.id = "inter_ag_env_submit"
    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_interaction)

    await client.start_agent_interaction(
        [UserMessage(content="Analyse the file.")],
        environment_sources=[
            {
                "type": "inline",
                "content": "YSxiCjEsMg==",
                "encoding": "base64",
                "target": "/workspace/data.csv",
            }
        ],
    )

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["environment"] == {
        "type": "remote",
        "sources": [
            {
                "type": "inline",
                "content": "YSxiCjEsMg==",
                "encoding": "base64",
                "target": "/workspace/data.csv",
            }
        ],
        "network": "disabled",
    }


@pytest.mark.asyncio
async def test_default_sandbox_is_stated_rather_than_omitted(
    mock_genai_client: MagicMock,
) -> None:
    """The shipped profile mounts nothing and sets no egress policy.

    Omitting ``environment`` entirely does not get the API's default sandbox --
    it gets ``400 Missing required field 'environment'``, which failed every
    run on a profile that configures no ``antigravity_config.environment``.
    """
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    mock_interaction = MagicMock()
    mock_interaction.id = "inter_ag_plain"
    mock_genai_client.aio.interactions.create = AsyncMock(return_value=mock_interaction)

    await client.start_agent_interaction([UserMessage(content="Do the thing.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["environment"] == {"type": "remote"}


@pytest.mark.asyncio
async def test_interactive_path_also_states_the_default_sandbox(
    mock_genai_client: MagicMock,
) -> None:
    """`/coder` run directly hit the same rejection as a delegated run."""
    client = GoogleGenAIClient(
        api_key="test",
        model=ANTIGRAVITY_AGENT_ID,
        antigravity_model="gemini-3.8-flash",
    )
    mock_genai_client.aio.interactions.create = AsyncMock(
        return_value=_completing_stream("inter_ag_plain_stream", "Done.")
    )

    await _drain_stream(client, [UserMessage(content="Do the thing.")])

    call_kwargs = mock_genai_client.aio.interactions.create.call_args.kwargs
    assert call_kwargs["environment"] == {"type": "remote"}


@pytest.mark.asyncio
async def test_environment_with_egress_validates_against_the_sdk_request_model() -> (
    None
):
    """The transform shape is what the installed SDK accepts, not just a dict we like."""
    client = _egress_client({
        "allowlist": [
            {
                "domain": "github.com",
                "transform": [{"Authorization": "Basic eC1hY2Nlc3M="}],
            }
        ]
    })

    kwargs = client._build_agent_create_kwargs([UserMessage(content="Clone the repo.")])
    kwargs["environment"] = await client._build_agent_environment()

    body = _CREATE_INTERACTION_ADAPTER.validate_python({**kwargs, "stream": False})
    assert body.environment.type == "remote"
    assert body.environment.network.allowlist[0].domain == "github.com"
    assert body.environment.network.allowlist[0].transform == [
        {"Authorization": "Basic eC1hY2Nlc3M="}
    ]
