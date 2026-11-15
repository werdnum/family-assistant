"""Request shape and error mapping of the ChatGPT plan client.

The SDK talks to an ``httpx.MockTransport`` that answers in the Responses API's
server-sent-event framing, so the assertions are about the bytes on the wire.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from family_assistant.llm.base import (
    AuthenticationError,
    RateLimitError,
    stream_error_to_exception,
)
from family_assistant.llm.connections import PlanCredentialError, ProviderConnections
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.providers.chatgpt_plan_client import ChatGPTPlanClient

if TYPE_CHECKING:
    from collections.abc import Callable

MODEL = "gpt-6.1-sol"


class _TokenSource:
    def __init__(self, *, error: PlanCredentialError | None = None) -> None:
        self.error = error
        self.rejected = 0

    async def access_token(self) -> str:
        if self.error is not None:
            raise self.error
        return "plan-token"

    def reject_access_token(self) -> None:
        self.rejected += 1


def _response(text: str, *, status: str = "completed") -> dict[str, object]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": MODEL,
        "status": status,
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "total_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _sse(*events: dict[str, object]) -> httpx.Response:
    body = "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    )
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/event-stream"}
    )


def _completed(text: str) -> httpx.Response:
    return _sse(
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**_response(""), "status": "in_progress", "output": []},
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": _response(text),
        },
    )


class _Server:
    def __init__(self, reply: Callable[[httpx.Request], httpx.Response]) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    def body(self) -> dict[str, object]:
        return json.loads(self.requests[-1].content)


def _client(
    server: _Server,
    source: _TokenSource | None = None,
    model_parameters: dict[str, dict[str, object]] | None = None,
) -> tuple[ChatGPTPlanClient, _TokenSource]:
    source = source or _TokenSource()
    connections = ProviderConnections()
    connections.chatgpt_plan = source
    client = ChatGPTPlanClient(
        api_key="",
        model=MODEL,
        connections=connections,
        model_parameters=model_parameters,
    )
    client.client = AsyncOpenAI(
        api_key=source.access_token,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
    )
    return client, source


MESSAGES = [SystemMessage(content="Be brief."), UserMessage(content="Hello")]


async def test_requests_are_streamed_unstored_and_use_the_plan_token() -> None:
    server = _Server(lambda _r: _completed("hi"))
    client, _ = _client(server)

    output = await client.generate_response(MESSAGES)

    body = server.body()
    assert output.content == "hi"
    assert (body["stream"], body["store"]) == (True, False)
    assert server.requests[-1].headers["authorization"] == "Bearer plan-token"


async def test_system_prompt_is_sent_as_a_developer_message() -> None:
    server = _Server(lambda _r: _completed("hi"))
    client, _ = _client(server)

    await client.generate_response(MESSAGES)

    items = server.body()["input"]
    assert isinstance(items, list)
    assert {item.get("role") for item in items} == {"developer", "user"}


def test_unsupported_parameter_fails_at_construction() -> None:
    connections = ProviderConnections()
    connections.chatgpt_plan = _TokenSource()

    with pytest.raises(ValueError, match="temperature"):
        ChatGPTPlanClient(
            api_key="",
            model=MODEL,
            connections=connections,
            model_parameters={"gpt-6": {"temperature": 0.2}},
        )


def test_client_needs_the_application_sign_in() -> None:
    with pytest.raises(ValueError, match="ChatGPT sign-in"):
        ChatGPTPlanClient(api_key="", model=MODEL, connections=ProviderConnections())


async def test_missing_sign_in_is_an_authentication_error() -> None:
    server = _Server(lambda _r: _completed("hi"))
    client, _ = _client(
        server, _TokenSource(error=PlanCredentialError("Sign in under Settings."))
    )

    with pytest.raises(AuthenticationError, match="Sign in under Settings"):
        await client.generate_response(MESSAGES)


async def test_usage_limit_mid_stream_is_a_rate_limit() -> None:
    failed = _response("", status="failed")
    failed["error"] = {
        "code": "subscription_sharing_usage_limit_exceeded",
        "message": "Usage limit reached.",
    }
    server = _Server(
        lambda _r: _sse({
            "type": "response.failed",
            "sequence_number": 1,
            "response": failed,
        })
    )
    client, _ = _client(server)

    with pytest.raises(RateLimitError, match="usage limit"):
        await client.generate_response(MESSAGES)


async def test_streamed_usage_limit_before_the_stream_maps_to_rate_limit() -> None:
    server = _Server(
        lambda _r: httpx.Response(
            429,
            json={
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "Usage limit reached.",
                    "type": "usage_limit",
                }
            },
        )
    )
    client, _ = _client(server)

    events = [event async for event in client.generate_response_stream(MESSAGES)]

    assert isinstance(stream_error_to_exception(events[-1]), RateLimitError)


async def test_rejected_token_is_reported_to_the_sign_in() -> None:
    server = _Server(
        lambda _r: httpx.Response(401, json={"error": {"message": "bad token"}})
    )
    client, source = _client(server)

    with pytest.raises(AuthenticationError):
        await client.generate_response(MESSAGES)
    assert source.rejected == 1


class _Answer(BaseModel):
    answer: str


async def test_structured_output_uses_responses_text_format() -> None:
    server = _Server(lambda _r: _completed('{"answer": "42"}'))
    client, _ = _client(server)

    result = await client.generate_structured(MESSAGES, _Answer)

    assert result == _Answer(answer="42")
    text = server.body()["text"]
    assert isinstance(text, dict)
    assert text["format"]["type"] == "json_schema"


def test_factory_builds_a_plan_client_for_provider_chatgpt() -> None:
    connections = ProviderConnections()
    connections.chatgpt_plan = _TokenSource()

    client = LLMClientFactory.create_client(
        {"model": MODEL, "provider": "chatgpt"}, connections
    )

    assert isinstance(client, ChatGPTPlanClient)


def test_factory_refuses_an_api_key_for_provider_chatgpt() -> None:
    with pytest.raises(ValueError, match="remove its api_key"):
        LLMClientFactory.create_client(
            {"model": MODEL, "provider": "chatgpt", "api_key": "sk-x"},
            ProviderConnections(),
        )
