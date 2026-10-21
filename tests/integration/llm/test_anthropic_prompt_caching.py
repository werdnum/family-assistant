"""Prompt-cache breakpoints, checked against what the Anthropic API reports.

The unit tests pin which breakpoints the client sends. This checks that the API
accepts them on the agent loop's real request shapes -- a one-hour breakpoint
over tools and the stable system prompt, five-minute ones on the conversation,
deferred tools surfaced by a ``tool_addition`` system message, under the
thinking-binding and tool-changes betas -- and that each lands in the TTL
bucket it was meant for. Replaying thinking blocks is covered by
``test_anthropic_streaming_thinking_round_trip``.

VCR matching strips ``cache_control``, so replay cannot notice a breakpoint
moving. What the cassette holds is the API's own account, in ``usage``, of what
it cached for the recorded request.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from anthropic.types import Message, Usage

from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.tools.types import ToolDefinition

from .vcr_helpers import sanitize_response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

CASSETTE = Path(
    "tests/cassettes/llm/test_agent_loop_cache_breakpoints_ttls.yaml",
)

MODEL = "claude-opus-5-5"

# Above the minimum cacheable prefix on every current model, so a missing
# write means the breakpoint was refused rather than that the prefix was short.
MIN_CACHEABLE_TOKENS = 4096
STABLE_SYSTEM_PROMPT = "You are the family's household assistant.\n\n" + "\n".join(
    f"House rule {i}: when asked about room {i}, refer to it as room number {i} "
    f"and remember that its lights are on circuit {i % 7}."
    for i in range(400)
)
VOLATILE_SYSTEM_SUFFIX = "\n\nCurrent time: Wednesday 21 October 2026, 14:05 AEDT."

TOOLS: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "activate_tools",
            "description": "Load on-demand tools by name so they can be called.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_names": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["tool_names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_lights",
            "description": "Turn the lights in a room on or off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room": {"type": "string"},
                    "on": {"type": "boolean"},
                },
                "required": ["room", "on"],
            },
        },
        "defer_loading": True,
    },
]


@pytest.fixture
def require_cassette(llm_record_mode: str) -> None:
    """Name the missing recording instead of failing on a downstream assertion."""
    if llm_record_mode == "replay" and not CASSETTE.exists():
        pytest.fail(
            f"Cassette missing at {CASSETTE}. Record with LLM_RECORD_MODE=record."
        )


def _cached_tokens(usage: Usage) -> int:
    """Prompt tokens served from or written to the cache, either way."""
    return (usage.cache_read_input_tokens or 0) + (
        usage.cache_creation_input_tokens or 0
    )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
@pytest.mark.usefixtures("require_cassette")
async def test_agent_loop_cache_breakpoints_ttls(
    llm_record_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two agent-loop turns and a one-shot call, each cached as intended.

    The one-shot (non-streamed) call has only the system breakpoint, so what it
    reads is exactly the tools and stable system prompt; it must write nothing.
    Each agent-loop turn must cache past that prefix, through its own tail, and
    the continuation -- a deferred tool surfaced by ``tool_addition`` -- must
    put nothing new under the hour.

    Re-recording within a TTL of the previous recording finds entries already
    warm and reads what a cold run would write, so the assertions count a read
    and a write alike wherever warmth can differ. Change the stable prompt to
    record cold.
    """
    if llm_record_mode != "replay" and not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("Recording this test requires ANTHROPIC_API_KEY")

    client = AnthropicClient(
        api_key=os.getenv("ANTHROPIC_API_KEY", "test-anthropic-key"),
        model=MODEL,
        model_parameters={
            MODEL: {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
            }
        },
    )

    # The client keeps only what it needs from each response; the cache
    # breakdown by TTL is read off the SDK's own return value.
    responses: list[Message] = []
    create: Callable[..., Awaitable[Message]] = client.client.messages.create

    async def recording_create(**kwargs: Any) -> Message:  # noqa: ANN401 # forwards the SDK's untyped create() kwargs
        response = await create(**kwargs)
        responses.append(response)
        return response

    monkeypatch.setattr(client.client.messages, "create", recording_create)

    system = SystemMessage(
        content=STABLE_SYSTEM_PROMPT + VOLATILE_SYSTEM_SUFFIX,
        stable_prefix_len=len(STABLE_SYSTEM_PROMPT),
    )
    first_turn: list[LLMMessage] = [
        system,
        UserMessage(
            content=(
                "Turn on the kitchen lights. The set_lights tool is not loaded "
                "yet, so load it with activate_tools first."
            )
        ),
    ]

    events = [
        event
        async for event in client.generate_response_stream(first_turn, tools=TOOLS)
    ]
    tool_calls = [e.tool_call for e in events if e.type == "tool_call" and e.tool_call]
    assert [call.function.name for call in tool_calls] == ["activate_tools"]
    done = next(e for e in events if e.type == "done")
    assert done.metadata is not None
    # Adaptive thinking may skip a turn this simple; one that did think has its
    # blocks replayed in the continuation, as the agent loop would.
    provider_metadata = done.metadata.get("provider_metadata")

    continuation: list[LLMMessage] = [
        *first_turn,
        AssistantMessage(
            content=None,
            tool_calls=tool_calls,
            provider_metadata=provider_metadata,
        ),
        ToolMessage(
            tool_call_id=tool_calls[0].id,
            name="activate_tools",
            content="Activated tools: set_lights.",
            activated_tools=["set_lights"],
        ),
    ]
    events = [
        event
        async for event in client.generate_response_stream(continuation, tools=TOOLS)
    ]
    assert any(e.type == "done" for e in events)

    await client.generate_response(continuation, tools=TOOLS)
    await client.close()

    first, second, one_shot = (response.usage for response in responses)

    prefix_tokens = one_shot.cache_read_input_tokens or 0
    assert prefix_tokens > MIN_CACHEABLE_TOKENS
    assert one_shot.cache_creation_input_tokens == 0

    assert first.cache_creation is not None
    if not first.cache_read_input_tokens:
        assert first.cache_creation.ephemeral_1h_input_tokens == prefix_tokens
    assert _cached_tokens(first) > prefix_tokens

    assert second.cache_creation is not None
    assert second.cache_creation.ephemeral_1h_input_tokens == 0
    assert _cached_tokens(second) > _cached_tokens(first)
