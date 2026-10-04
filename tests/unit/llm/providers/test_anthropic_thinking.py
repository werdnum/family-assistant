"""Unit tests for Anthropic extended-thinking capture and replay.

The API verifies the `signature` on a thinking block when it is replayed, so
these tests are mostly about fidelity: what comes out of a response must go back
in unchanged, in the right position, and must not leak into other providers.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.base import InvalidRequestError, LLMProviderError
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import (
    AnthropicClient,
    log_input_transformations,
)
from family_assistant.tools.types import ToolDefinition

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


def test_extract_thinking_blocks_accepts_plain_dicts() -> None:
    """JSON-shaped SDK substitutes preserve thinking metadata too."""
    blocks = AnthropicClient._extract_thinking_blocks([
        THINKING_BLOCK,
        {"type": "text", "text": "hello"},
        REDACTED_BLOCK,
    ])

    assert blocks == [THINKING_BLOCK, REDACTED_BLOCK]


def test_thinking_blocks_replayed_verbatim_and_first(
    client: AnthropicClient,
) -> None:
    """Thinking is replayed byte-identically, leading the turn as the API emits it.

    Position is a convention, not an API requirement -- the API accepts thinking
    anywhere in the turn. The byte-identical part is the requirement: the
    signature is verified.
    """
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
    ],
)
def test_foreign_metadata_yields_no_thinking_blocks(
    provider_metadata: object,
) -> None:
    """A provider switch mid-thread degrades to a plain replay."""
    assert AnthropicClient._thinking_blocks_from_metadata(provider_metadata) == []


@pytest.mark.parametrize(
    "provider_metadata",
    [
        pytest.param({"provider": "anthropic"}, id="missing-blocks"),
        pytest.param(
            {"provider": "anthropic", "thinking_blocks": "not-a-list"},
            id="wrong-block-type",
        ),
    ],
)
def test_malformed_anthropic_metadata_is_rejected(
    provider_metadata: object,
) -> None:
    """Same-provider corruption must be surfaced instead of losing reasoning."""
    with pytest.raises(TypeError, match="thinking_blocks must be a list"):
        AnthropicClient._thinking_blocks_from_metadata(provider_metadata)


@pytest.mark.parametrize(
    "invalid_block",
    [
        pytest.param("not-an-object", id="scalar"),
        pytest.param({"type": "text", "text": "not thinking"}, id="invalid-type"),
    ],
)
def test_malformed_anthropic_thinking_block_is_rejected(
    invalid_block: object,
) -> None:
    """A corrupt block cannot be partially filtered out of a signed turn."""
    provider_metadata = {
        "provider": "anthropic",
        "thinking_blocks": [THINKING_BLOCK, invalid_block],
    }

    with pytest.raises(TypeError, match="invalid entry at index 1"):
        AnthropicClient._thinking_blocks_from_metadata(provider_metadata)


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

    assert params["thinking"] == {
        "type": "enabled",
        "budget_tokens": 4096,
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }


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

    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"] == {"effort": "high"}


async def test_nonstreaming_thinking_budget_error_keeps_invalid_request_type() -> None:
    """Local config validation is not remapped as a generic provider error."""
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
        await client.generate_response([UserMessage(content="Think carefully")])


async def test_streaming_thinking_budget_error_is_typed_invalid_request() -> None:
    """A stream that fails before any output raises the same typed error.

    Raised rather than yielded as an error event, so a retrying client can
    still hand the request to its fallback.
    """
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
        async for _event in client.generate_response_stream([
            UserMessage(content="Think carefully")
        ]):
            pass


class _FakeAnthropicStream:
    """Minimal SDK-shaped stream that exercises the production event loop."""

    def __init__(self) -> None:
        self._events = [
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(
                    type="thinking_delta", thinking="Checking the arithmetic"
                ),
            )
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event

    async def get_final_message(self) -> object:
        return SimpleNamespace(
            content=[THINKING_BLOCK],
            usage=None,
            model="claude-sonnet-4-6-20250929",
            id="msg_fake",
            stop_reason="end_turn",
        )


async def test_production_stream_loop_emits_thinking_delta() -> None:
    """The production messages.stream branch keeps thinking separate from text."""
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-6")
    fake_stream = _FakeAnthropicStream()

    with (
        patch.object(
            client,
            "_maybe_parse_vcr_stream",
            new=AsyncMock(return_value=None),
        ),
        patch.object(client.client.messages, "stream", return_value=fake_stream),
    ):
        events = [
            event
            async for event in client.generate_response_stream([
                UserMessage(content="What is 42 times 17?")
            ])
        ]

    assert [event.type for event in events] == ["thinking", "done"]
    assert events[0].content == "Checking the arithmetic"
    assert events[1].metadata is not None
    assert events[1].metadata.get("provider_metadata") == _thinking_metadata()


_SHIPPED_THINKING_PARAMS: dict[str, dict[str, object]] = {
    "claude-sonnet-5": {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "max_tokens": 16000,
    }
}

_CALC_TOOL: list[ToolDefinition] = [
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": "Calculate.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _shipped_client() -> AnthropicClient:
    return AnthropicClient(
        api_key="test-key",
        model="claude-sonnet-5",
        model_parameters=_SHIPPED_THINKING_PARAMS,
    )


@pytest.mark.parametrize(
    ("tool_choice", "instruction"),
    [
        ("required", "Respond by calling one of the available tools."),
        ("calc", "Respond by calling the `calc` tool."),
    ],
)
def test_forced_tool_choice_is_requested_in_words(
    tool_choice: str, instruction: str
) -> None:
    """A forced choice goes out as `auto` plus an instruction, keeping thinking.

    Opus 5.5 and Fable 5.1 reject a forced `any`/`tool` choice outright, and
    every generation rejects one alongside thinking, so neither is ever sent.
    """
    params = _shipped_client()._build_request_params(
        api_messages=[{"role": "user", "content": "hi"}],
        system_blocks=None,
        tools=_CALC_TOOL,
        tool_choice=tool_choice,
    )

    assert params["tool_choice"] == {"type": "auto"}
    assert params["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "text", "text": instruction},
            ],
        }
    ]
    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"] == {"effort": "high"}


def test_tool_instruction_follows_tool_results_in_the_last_turn() -> None:
    """After a tool round the last user turn is a list of blocks; it is extended."""
    tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "4"}
    params = _shipped_client()._build_request_params(
        api_messages=[{"role": "user", "content": [tool_result]}],
        system_blocks=None,
        tools=_CALC_TOOL,
        tool_choice="calc",
    )

    assert params["messages"][-1]["content"] == [
        tool_result,
        {"type": "text", "text": "Respond by calling the `calc` tool."},
    ]


def test_auto_tool_choice_keeps_thinking() -> None:
    """The agentic path must be untouched — this is where thinking earns its cost."""
    params = _shipped_client()._build_request_params(
        api_messages=[{"role": "user", "content": "hi"}],
        system_blocks=None,
        tools=_CALC_TOOL,
        tool_choice="auto",
    )

    assert params["tool_choice"] == {"type": "auto"}
    assert params["thinking"]["type"] == "adaptive"
    assert params["output_config"] == {"effort": "high"}


async def test_structured_output_requests_its_tool_in_words() -> None:
    """`generate_structured` asks for its output tool rather than forcing it.

    Asserted on the request the client actually builds rather than on the helper,
    because this path assembles its params inline instead of going through
    `_build_request_params`.
    """

    class _Model(BaseModel):
        answer: str

    client = _shipped_client()
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="return_structured_response",
                input={"answer": "42"},
            )
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="tool_use",
    )
    create = AsyncMock(return_value=response)

    with patch.object(client.client.messages, "create", new=create):
        await client.generate_structured([UserMessage(content="hi")], _Model)

    assert create.await_args is not None
    sent = create.await_args.kwargs
    assert sent["tool_choice"] == {"type": "auto"}
    assert sent["messages"][-1]["content"][-1] == {
        "type": "text",
        "text": "Respond by calling the `return_structured_response` tool.",
    }
    assert sent["thinking"]["type"] == "adaptive"


def _text_only_response() -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="The answer is 4.")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        stop_reason="end_turn",
        model="claude-sonnet-5",
        id="msg_1",
    )


@pytest.mark.parametrize("tool_choice", ["required", "calc"])
async def test_reply_ignoring_the_required_tool_is_a_provider_error(
    tool_choice: str,
) -> None:
    """The instruction is not enforced by the API, so the client enforces it.

    Raising lets a retrying client fall back instead of handing the caller a
    reply without the tool call it required.
    """
    client = _shipped_client()
    create = AsyncMock(return_value=_text_only_response())

    with (
        patch.object(client.client.messages, "create", new=create),
        pytest.raises(LLMProviderError, match="did not call the required tool"),
    ):
        await client.generate_response(
            [UserMessage(content="hi")], tools=_CALC_TOOL, tool_choice=tool_choice
        )


async def test_auto_reply_without_a_tool_call_is_fine() -> None:
    client = _shipped_client()
    create = AsyncMock(return_value=_text_only_response())

    with patch.object(client.client.messages, "create", new=create):
        output = await client.generate_response(
            [UserMessage(content="hi")], tools=_CALC_TOOL, tool_choice="auto"
        )

    assert output.content == "The answer is 4."


def test_thinking_requests_drop_rather_than_reject_mismatched_blocks() -> None:
    """A history the API no longer matches costs the stale reasoning, not the turn.

    Without the explicit setting an enforced account gets a 400, which the
    retry layer turns into a silent fallback to another model.
    """
    client = AnthropicClient(
        api_key="test-key",
        model="claude-opus-5-5",
        model_parameters={"claude-opus-5-5": {"thinking": {"type": "adaptive"}}},
    )

    params = client._build_request_params([], None, None, "auto")

    assert params["thinking"]["block_binding"] == {
        "prefix_mismatch_behavior": "drop_block"
    }
    assert params["extra_headers"]["anthropic-beta"] == (
        "thinking-binding-controls-2026-08-01"
    )


def test_no_binding_without_a_thinking_config() -> None:
    """The field lives inside ``thinking`` and is rejected without the header."""
    client = AnthropicClient(api_key="test-key", model="claude-haiku-4-5")

    params = client._build_request_params([], None, None, "auto")

    assert "thinking" not in params
    assert "extra_headers" not in params


def test_prefix_mismatches_are_logged_as_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed history is worth a warning; a model switch is expected."""
    message = SimpleNamespace(
        input_transformations=[
            {
                "type": "thinking_dropped",
                "path": "messages.3.content.0",
                "reason": "prefix_binding_mismatch",
            },
            {
                "type": "thinking_dropped",
                "path": "messages.1.content.0",
                "reason": "model_binding_mismatch",
            },
        ]
    )

    with caplog.at_level("INFO"):
        log_input_transformations(message)

    levels = {
        record.getMessage().split("reason=")[1].split()[0]: record.levelname
        for record in caplog.records
    }
    assert levels == {
        "prefix_binding_mismatch": "WARNING",
        "model_binding_mismatch": "INFO",
    }
