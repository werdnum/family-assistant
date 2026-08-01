"""Unit tests for Anthropic extended-thinking capture and replay.

The API verifies the `signature` on a thinking block when it is replayed, so
these tests are mostly about fidelity: what comes out of a response must go back
in unchanged, in the right position, and must not leak into other providers.
"""

import pytest

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.base import InvalidRequestError
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient

THINKING_BLOCK: dict[str, object] = {
    "type": "thinking",
    "thinking": "42 * 17 = 714. I should use the calculate tool.",
    "signature": "ErUBCkYIBRgCKkBm2n0p" * 4,
}
REDACTED_BLOCK: dict[str, object] = {
    "type": "redacted_thinking",
    "data": "EroBCkYIBRgCKkBopaque",
}


class _Block:
    """Stand-in for an SDK content block, which exposes `model_dump`."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.type = payload["type"]

    def model_dump(self, **_kwargs: object) -> dict[str, object]:
        return dict(self._payload)


@pytest.fixture
def client() -> AnthropicClient:
    return AnthropicClient(api_key="test-key", model="claude-sonnet-4-6")


def _thinking_metadata() -> dict[str, object]:
    return {"provider": "anthropic", "thinking_blocks": [THINKING_BLOCK]}


def test_extract_thinking_blocks_keeps_only_thinking_types() -> None:
    """Text and tool_use blocks are not reasoning state."""
    blocks = AnthropicClient._extract_thinking_blocks([
        _Block(THINKING_BLOCK),
        _Block({"type": "text", "text": "hello"}),
        _Block(REDACTED_BLOCK),
        _Block({"type": "tool_use", "id": "toolu_1", "name": "calculate"}),
    ])

    assert blocks == [THINKING_BLOCK, REDACTED_BLOCK]


def test_thinking_blocks_replayed_verbatim_and_first(
    client: AnthropicClient,
) -> None:
    """Thinking must lead the assistant turn, byte-identical to what came back."""
    messages: list[LLMMessage] = [
        UserMessage(content="What is 42 times 17?"),
        AssistantMessage(
            content="Let me calculate that.",
            tool_calls=[
                ToolCallItem(
                    id="toolu_1",
                    type="function",
                    function=ToolCallFunction(
                        name="calculate", arguments='{"expression": "42 * 17"}'
                    ),
                )
            ],
            provider_metadata=_thinking_metadata(),
        ),
    ]

    _system, api_messages = client._convert_messages_to_anthropic_format(messages)

    content = api_messages[-1]["content"]
    assert [block["type"] for block in content] == ["thinking", "text", "tool_use"]
    assert content[0] == THINKING_BLOCK


def test_assistant_turn_without_thinking_metadata_is_unchanged(
    client: AnthropicClient,
) -> None:
    """Thinking disabled is the default, and must stay a no-op."""
    messages: list[LLMMessage] = [
        AssistantMessage(content="Plain answer, no reasoning captured."),
    ]

    _system, api_messages = client._convert_messages_to_anthropic_format(messages)

    assert [block["type"] for block in api_messages[0]["content"]] == ["text"]


@pytest.mark.parametrize(
    "provider_metadata",
    [
        pytest.param(None, id="none"),
        pytest.param(
            {"provider": "google", "thought_signature": "abc"}, id="gemini-signature"
        ),
        pytest.param(
            {"openai_response_output": [{"type": "reasoning"}]}, id="openai-responses"
        ),
        pytest.param({"provider": "anthropic"}, id="anthropic-without-blocks"),
        pytest.param(
            {"provider": "anthropic", "thinking_blocks": "not-a-list"},
            id="malformed-blocks",
        ),
    ],
)
def test_foreign_or_malformed_metadata_yields_no_thinking_blocks(
    provider_metadata: object,
) -> None:
    """A provider switch mid-thread degrades to a plain replay, never an error."""
    assert AnthropicClient._thinking_blocks_from_metadata(provider_metadata) == []


def test_tool_results_still_convert_alongside_thinking(
    client: AnthropicClient,
) -> None:
    """The tool_result turn is unaffected by thinking replay."""
    messages: list[LLMMessage] = [
        UserMessage(content="What is 42 times 17?"),
        AssistantMessage(
            content=None,
            tool_calls=[
                ToolCallItem(
                    id="toolu_1",
                    type="function",
                    function=ToolCallFunction(
                        name="calculate", arguments='{"expression": "42 * 17"}'
                    ),
                )
            ],
            provider_metadata=_thinking_metadata(),
        ),
        ToolMessage(tool_call_id="toolu_1", content="714", name="calculate"),
    ]

    _system, api_messages = client._convert_messages_to_anthropic_format(messages)

    assert [block["type"] for block in api_messages[1]["content"]] == [
        "thinking",
        "tool_use",
    ]
    assert api_messages[2]["content"][0]["type"] == "tool_result"


def test_thinking_budget_at_or_above_max_tokens_is_rejected() -> None:
    """A budget that cannot fit is a config error, not a mid-turn 400."""
    client = AnthropicClient(
        api_key="test-key",
        model="claude-sonnet-4-6",
        model_parameters={
            "claude-sonnet-4-6": {
                "thinking": {"type": "enabled", "budget_tokens": 8192}
            }
        },
    )

    with pytest.raises(InvalidRequestError, match="must be less than max_tokens"):
        client._build_request_params([], None, None, "auto")


def test_thinking_budget_below_max_tokens_is_accepted() -> None:
    """The normal case passes the thinking config straight through."""
    client = AnthropicClient(
        api_key="test-key",
        model="claude-sonnet-4-6",
        model_parameters={
            "claude-sonnet-4-6": {
                "thinking": {"type": "enabled", "budget_tokens": 4096}
            }
        },
    )

    params = client._build_request_params([], None, None, "auto")

    assert params["thinking"] == {"type": "enabled", "budget_tokens": 4096}


def test_adaptive_thinking_shape_is_not_budget_checked() -> None:
    """Newer models use `adaptive` + effort and carry no budget to validate."""
    client = AnthropicClient(
        api_key="test-key",
        model="claude-sonnet-4-6",
        model_parameters={
            "claude-sonnet-4-6": {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
            }
        },
    )

    params = client._build_request_params([], None, None, "auto")

    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "high"}
