"""Routing OpenAI models through the signed-in ChatGPT plan before the API key."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from family_assistant.llm import LLMOutput, LLMStreamEvent
from family_assistant.llm.base import (
    InvalidRequestError,
    ProviderConnectionError,
    RateLimitError,
)
from family_assistant.llm.connections import ProviderConnections
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.plan_first import PlanFirstClient
from family_assistant.llm.providers.chatgpt_plan_client import ChatGPTPlanClient
from family_assistant.llm.providers.openai_client import OpenAIClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from family_assistant.llm import LLMInterface
    from family_assistant.llm.messages import LLMMessage
    from family_assistant.tools.types import ToolDefinition

MODEL = "gpt-6.1-sol"
MESSAGES = [UserMessage(content="Hello")]


class _Source:
    def __init__(self, *, signed_in: bool = True) -> None:
        self.signed_in = signed_in

    async def available(self) -> bool:
        return self.signed_in

    async def access_token(self) -> str:
        return "plan-token"

    def reject_access_token(self) -> None:
        pass


class _Client:
    """Answers with a fixed reply or raises a fixed error; counts calls."""

    def __init__(
        self,
        name: str,
        *,
        error: Exception | None = None,
        stream: Sequence[LLMStreamEvent] | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.stream = stream
        self.calls = 0

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMOutput(content=self.name)

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        for event in self.stream or [
            LLMStreamEvent(type="content", content=self.name),
            LLMStreamEvent(type="done"),
        ]:
            yield event


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _routed(
    plan: _Client,
    api: _Client | None,
    *,
    signed_in: bool = True,
    clock: _Clock | None = None,
) -> PlanFirstClient:
    return PlanFirstClient(
        cast("LLMInterface", plan),
        cast("LLMInterface | None", api),
        model=MODEL,
        source=_Source(signed_in=signed_in),
        clock=clock or _Clock(),
    )


def _content(events: list[LLMStreamEvent]) -> str:
    return "".join(event.content or "" for event in events if event.type == "content")


async def test_signed_in_requests_go_to_the_plan() -> None:
    client = _routed(_Client("plan"), _Client("api"))

    output = await client.generate_response(MESSAGES)

    assert output.content == "plan"


async def test_signed_out_requests_go_straight_to_the_api_key() -> None:
    plan = _Client("plan")
    client = _routed(plan, _Client("api"), signed_in=False)

    output = await client.generate_response(MESSAGES)

    assert (output.content, plan.calls) == ("api", 0)


async def test_a_plan_failure_is_answered_by_the_api_key() -> None:
    client = _routed(
        _Client("plan", error=ProviderConnectionError("down", "chatgpt", MODEL)),
        _Client("api"),
    )

    output = await client.generate_response(MESSAGES)

    assert output.content == "api"


async def test_a_usage_limit_pauses_the_plan_for_this_model() -> None:
    plan = _Client("plan", error=RateLimitError("limit", "chatgpt", MODEL))
    client = _routed(plan, _Client("api"))
    await client.generate_response(MESSAGES)

    await client.generate_response(MESSAGES)

    assert plan.calls == 1


async def test_the_plan_is_tried_again_once_the_pause_ends() -> None:
    clock = _Clock()
    plan = _Client("plan", error=InvalidRequestError("refused", "chatgpt", MODEL))
    client = _routed(plan, _Client("api"), clock=clock)
    await client.generate_response(MESSAGES)
    plan.error = None
    clock.now = 3601.0

    output = await client.generate_response(MESSAGES)

    assert output.content == "plan"


async def test_without_an_api_key_the_plan_error_is_the_answer() -> None:
    client = _routed(
        _Client("plan", error=RateLimitError("limit", "chatgpt", MODEL)), None
    )

    with pytest.raises(RateLimitError):
        await client.generate_response(MESSAGES)


async def test_a_stream_refused_before_it_starts_falls_back() -> None:
    refused = LLMStreamEvent(
        type="error",
        error="limit",
        metadata={"error_type": "rate_limit", "provider": "chatgpt", "model": MODEL},
    )
    client = _routed(_Client("plan", stream=[refused]), _Client("api"))

    events = [event async for event in client.generate_response_stream(MESSAGES)]

    assert _content(events) == "api"


async def test_a_stream_that_fails_after_answering_is_not_replayed() -> None:
    api = _Client("api")
    partial = [
        LLMStreamEvent(type="content", content="half"),
        LLMStreamEvent(
            type="error",
            error="limit",
            metadata={"error_type": "rate_limit", "provider": "chatgpt"},
        ),
    ]
    client = _routed(_Client("plan", stream=partial), api)

    events = [event async for event in client.generate_response_stream(MESSAGES)]

    assert (_content(events), events[-1].type, api.calls) == ("half", "error", 0)


def _connections() -> ProviderConnections:
    connections = ProviderConnections()
    connections.chatgpt_plan = _Source()
    return connections


def _inner(client: LLMInterface) -> tuple[object, object]:
    assert isinstance(client, PlanFirstClient)
    return type(client.plan), type(client.api)


def test_openai_models_try_the_plan_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = LLMClientFactory.create_client(
        {"model": MODEL, "provider": "openai"}, _connections()
    )

    assert _inner(client) == (ChatGPTPlanClient, OpenAIClient)


def test_without_an_api_key_the_plan_is_the_only_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = LLMClientFactory.create_client(
        {"model": MODEL, "provider": "openai"}, _connections()
    )

    assert _inner(client) == (ChatGPTPlanClient, type(None))


def test_a_model_the_plan_refuses_stays_on_its_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = LLMClientFactory.create_client(
        {
            "model": MODEL,
            "provider": "openai",
            "model_parameters": {MODEL: {"temperature": 0.2}},
        },
        _connections(),
    )

    assert isinstance(client, OpenAIClient)


def test_other_openai_compatible_endpoints_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

    client = LLMClientFactory.create_client(
        {"model": "openrouter/openai/gpt-6.1-sol"}, _connections()
    )

    assert isinstance(client, OpenAIClient)


def test_without_the_sign_in_feature_models_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = LLMClientFactory.create_client(
        {"model": MODEL, "provider": "openai"}, ProviderConnections()
    )

    assert isinstance(client, OpenAIClient)
