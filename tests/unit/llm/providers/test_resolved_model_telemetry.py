"""Each provider reports back the model the API actually served.

The requested model is often an alias -- ``-latest``, or a name the provider
routes server-side -- so a latency or quality change that has no deploy behind
it is usually the resolved model moving underneath. These tests pin that the
resolved id reaches both the diagnostics record and ``LLMOutput``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.request_buffer import LLMRequestBuffer, get_request_buffer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Iterator

    from family_assistant.llm import LLMStreamEvent


@pytest.fixture
def buffer() -> Iterator[LLMRequestBuffer]:
    """Empties the global diagnostics buffer around each test."""
    request_buffer = get_request_buffer()
    request_buffer.clear()
    yield request_buffer
    request_buffer.clear()


async def test_anthropic_records_the_served_model(buffer: LLMRequestBuffer) -> None:
    client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-6")
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(input_tokens=3, output_tokens=4),
        model="claude-sonnet-4-6-20250929",
        id="msg_abc",
        stop_reason="end_turn",
    )

    with patch.object(
        client.client.messages, "create", new=AsyncMock(return_value=response)
    ):
        output = await client.generate_response([UserMessage(content="hi")])

    assert output.resolved_model == "claude-sonnet-4-6-20250929"
    record = buffer.get_recent()[0]
    assert record.model_id == "claude-sonnet-4-6"
    assert record.resolved_model_id == "claude-sonnet-4-6-20250929"
    assert record.provider == "anthropic"
    assert record.finish_reason == "end_turn"
    assert record.response_id == "msg_abc"


async def test_openai_chat_completions_records_the_served_model(
    buffer: LLMRequestBuffer,
) -> None:
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.5",
        model_parameters={"gpt-5.5": {"use_responses_api": False}},
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="hi", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=None,
        model="gpt-5.5-2026-05-01",
        id="chatcmpl_abc",
    )

    async def _fake_create(**_kwargs: object) -> SimpleNamespace:
        return response

    client.client = cast(
        "Any",
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_create))
        ),
    )

    output = await client.generate_response([UserMessage(content="hi")])

    assert output.resolved_model == "gpt-5.5-2026-05-01"
    record = buffer.get_recent()[0]
    assert record.model_id == "gpt-5.5"
    assert record.resolved_model_id == "gpt-5.5-2026-05-01"
    assert record.provider == "openai"
    assert record.finish_reason == "stop"


async def test_openai_responses_stream_failure_is_recorded_when_the_consumer_stops(
    buffer: LLMRequestBuffer,
) -> None:
    """The processing loop raises on the error event, closing the generator.

    Anything the provider leaves until after its loop never runs, so a failed
    turn would be the one call missing from the diagnostics buffer.
    """
    # Pinned to the official base URL: OPENAI_BASE_URL in the environment
    # reaches the SDK without passing through this constructor, and a
    # compatible endpoint routes to Chat Completions instead.
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        base_url="https://api.openai.com/v1",
    )

    async def _fake_create(**_kwargs: object) -> AsyncIterator[object]:
        async def _iter() -> AsyncIterator[object]:
            yield SimpleNamespace(
                type="response.failed",
                response=SimpleNamespace(
                    error=SimpleNamespace(message="upstream refused"),
                    id="resp_failed",
                    model="gpt-5.6-sol-2026-05-01",
                ),
            )

        return _iter()

    client.client = cast(
        "Any", SimpleNamespace(responses=SimpleNamespace(create=_fake_create))
    )

    stream = cast(
        "AsyncGenerator[LLMStreamEvent]",
        client.generate_response_stream([UserMessage(content="hi")]),
    )
    async for event in stream:
        if event.type == "error":
            break
    await stream.aclose()

    record = buffer.get_recent()[0]
    assert record.error == "upstream refused"
    assert record.response is None
    # A failed turn is the one most likely to be taken to the provider.
    assert record.response_id == "resp_failed"
    assert record.resolved_model_id == "gpt-5.6-sol-2026-05-01"


async def test_openai_responses_incomplete_run_is_not_recorded_as_a_success(
    buffer: LLMRequestBuffer,
) -> None:
    """A run that stopped on the output-token limit returns a truncated answer.

    The call still returns it -- that behaviour is unchanged -- but the
    diagnostics must not claim the turn went fine.
    """
    client = OpenAIClient(
        api_key="test-key",
        model="gpt-5.6-sol",
        base_url="https://api.openai.com/v1",
    )
    response = SimpleNamespace(
        output_text="half an ans",
        output=[],
        model="gpt-5.6-sol-2026-05-01",
        id="resp_abc",
        status="incomplete",
        usage=None,
    )

    async def _fake_create(**_kwargs: object) -> SimpleNamespace:
        return response

    client.client = cast(
        "Any", SimpleNamespace(responses=SimpleNamespace(create=_fake_create))
    )

    output = await client.generate_response([UserMessage(content="hi")])

    assert output.content == "half an ans"
    record = buffer.get_recent()[0]
    assert record.error == "OpenAI Responses request incomplete"
