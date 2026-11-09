"""How the Anthropic client sizes ``max_tokens`` for each call path.

The requests go through the real SDK to an in-process transport, so the SDK's
own refusal of large non-streaming requests is exercised rather than mocked.
"""

import json
from unittest.mock import patch

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient

_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-fable-5-2",
    "content": [{"type": "text", "text": '{"answer": "42"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 5},
}

_STREAM_EVENTS = [
    (
        "message_start",
        {"type": "message_start", "message": {**_MESSAGE, "content": []}},
    ),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


class _FakeMessagesEndpoint:
    """Answers /v1/messages and keeps every request body it was sent."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if body.get("stream"):
            sse = "".join(
                f"event: {name}\ndata: {json.dumps(data)}\n\n"
                for name, data in _STREAM_EVENTS
            )
            return httpx.Response(
                200, text=sse, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_MESSAGE)


def _client(
    endpoint: _FakeMessagesEndpoint,
    model_parameters: dict[str, dict[str, object]] | None = None,
) -> AnthropicClient:
    client = AnthropicClient(
        api_key="test-key",
        model="claude-fable-5-2",
        model_parameters=model_parameters,
    )
    client.client = anthropic.AsyncAnthropic(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint.handle)),
        max_retries=0,
    )
    return client


async def _no_vcr_replay(_params: dict[str, object]) -> None:
    return None


async def _stream(client: AnthropicClient) -> None:
    # Skips the test-only replay branch so the request goes out as a stream.
    with patch.object(client, "_maybe_parse_vcr_stream", new=_no_vcr_replay):
        async for _event in client.generate_response_stream([
            UserMessage(content="hi")
        ]):
            pass


async def test_streamed_turn_gets_the_streaming_cap() -> None:
    endpoint = _FakeMessagesEndpoint()

    await _stream(_client(endpoint))

    assert endpoint.bodies[0]["stream"] is True
    assert endpoint.bodies[0]["max_tokens"] == 64_000


async def test_non_streamed_response_gets_a_cap_the_sdk_accepts() -> None:
    endpoint = _FakeMessagesEndpoint()

    await _client(endpoint).generate_response([UserMessage(content="hi")])

    assert endpoint.bodies[0]["max_tokens"] == 16_000


async def test_structured_output_gets_a_cap_the_sdk_accepts() -> None:
    class _Answer(BaseModel):
        answer: str

    endpoint = _FakeMessagesEndpoint()

    await _client(endpoint).generate_structured([UserMessage(content="hi")], _Answer)

    assert endpoint.bodies[0]["max_tokens"] == 16_000


async def test_replayed_stream_keeps_the_streaming_cap() -> None:
    """Under pytest a stream is replayed as one non-streaming request.

    It carries the streaming cap so recorded requests still match, which the
    SDK would refuse without a stream unless given an explicit timeout.
    """
    endpoint = _FakeMessagesEndpoint()

    events = [
        event
        async for event in _client(endpoint).generate_response_stream([
            UserMessage(content="hi")
        ])
    ]

    assert endpoint.bodies[0]["max_tokens"] == 64_000
    assert "stream" not in endpoint.bodies[0]
    assert [event.type for event in events][-1] == "done"


@pytest.mark.parametrize("streaming", [True, False])
async def test_configured_max_tokens_overrides_either_default(
    *, streaming: bool
) -> None:
    endpoint = _FakeMessagesEndpoint()
    client = _client(endpoint, {"claude-fable-5-2": {"max_tokens": 12_000}})

    if streaming:
        await _stream(client)
    else:
        await client.generate_response([UserMessage(content="hi")])

    assert endpoint.bodies[0]["max_tokens"] == 12_000
