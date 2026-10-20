"""Configured ``llm_parameters`` reach each provider's request on the wire.

Each client goes through its real SDK to an in-process transport, so what is
asserted is the JSON the provider would actually receive -- not what the client
handed the SDK, which can drop or reject a parameter on its own.
"""

from __future__ import annotations

import json

import httpx
import pytest
from google import genai
from google.genai import types
from openai import AsyncOpenAI

from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import UserMessage
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient

_GEMINI_REPLY = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "{}"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
}

_CHAT_COMPLETION_REPLY = {
    "id": "gen-1",
    "object": "chat.completion",
    "created": 1760400000,
    "model": "google/gemini-3.8-flash",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


def _recording_transport(
    reply: dict[str, object], requests: list[dict[str, object]]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(handler)


def _gemini_client(
    model_parameters: dict[str, dict[str, object]],
    requests: list[dict[str, object]],
) -> GoogleGenAIClient:
    client = GoogleGenAIClient(
        api_key="test-key",
        model="gemini-3.8-flash",
        model_parameters=model_parameters,
    )
    client.client = genai.Client(
        api_key="test-key",
        http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(
                transport=_recording_transport(_GEMINI_REPLY, requests)
            )
        ),
    )
    return client


async def test_gemini_thinking_config_reaches_the_request() -> None:
    requests: list[dict[str, object]] = []
    client = _gemini_client(
        {
            "gemini-3.8-flash": {
                "thinking_config": {"thinking_level": "low", "include_thoughts": True}
            }
        },
        requests,
    )

    await client.generate_response([UserMessage(content="hi")])

    assert requests[0]["generationConfig"]["thinkingConfig"] == {  # type: ignore[index]
        "thinking_level": "LOW",
        "include_thoughts": True,
    }


async def test_gemini_parameters_reach_structured_output_too() -> None:
    requests: list[dict[str, object]] = []
    client = _gemini_client(
        {"gemini-": {"thinking_config": {"thinking_budget": 512}, "seed": 7}},
        requests,
    )

    await client.generate_json([UserMessage(content="Return {}")])

    generation_config = requests[0]["generationConfig"]
    assert generation_config["thinkingConfig"] == {"thinking_budget": 512}  # type: ignore[index]
    assert generation_config["seed"] == 7  # type: ignore[index]


async def test_gemini_max_tokens_is_sent_as_max_output_tokens() -> None:
    requests: list[dict[str, object]] = []
    client = _gemini_client({"gemini-": {"max_tokens": 1024}}, requests)

    await client.generate_response([UserMessage(content="hi")])

    assert requests[0]["generationConfig"]["maxOutputTokens"] == 1024  # type: ignore[index]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"thinking": {"type": "adaptive"}}, "unknown parameter 'thinking'"),
        ({"tools": []}, "the client sets itself"),
        ({"temperature": "warm"}, "Invalid llm_parameters"),
    ],
)
def test_gemini_rejects_parameters_it_cannot_send(
    params: dict[str, object], message: str
) -> None:
    """A parameter that would be dropped or rejected fails when the client is built."""
    with pytest.raises(ValueError, match=message):
        GoogleGenAIClient(
            api_key="test-key",
            model="gemini-3.8-flash",
            model_parameters={"gemini-3.8-flash": params},
        )


def test_gemini_ignores_parameters_for_other_models() -> None:
    client = GoogleGenAIClient(
        api_key="test-key",
        model="gemini-3.8-flash",
        model_parameters={"claude-opus-5": {"thinking": {"type": "adaptive"}}},
    )

    assert client._build_base_generation_config().thinking_config is None


def test_gemini_base_url_reaches_the_sdk_client() -> None:
    client = LLMClientFactory.create_client({
        "provider": "google",
        "model": "gemini-3.8-flash",
        "api_key": "test-key",
        "api_base": "https://gemini.example.test/",
    })

    assert isinstance(client, GoogleGenAIClient)
    assert client.client._api_client._http_options.base_url == (
        "https://gemini.example.test/"
    )


async def test_gemini_versioned_base_url_is_not_versioned_twice() -> None:
    """A legacy ``.../v1beta`` endpoint keeps the SDK from appending another."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=_GEMINI_REPLY)

    client = GoogleGenAIClient(
        api_key="test-key",
        model="gemini-3.8-flash",
        base_url="https://gemini.example.test/v1beta",
    )
    http_options = client.client._api_client._http_options
    http_options.httpx_async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    client.client = genai.Client(api_key="test-key", http_options=http_options)

    await client.generate_response([UserMessage(content="hi")])

    assert urls[0].startswith(
        "https://gemini.example.test/v1beta/models/gemini-3.8-flash:"
    )


def _openai_chat_client(
    model: str,
    model_parameters: dict[str, dict[str, object]],
    requests: list[dict[str, object]],
) -> OpenAIClient:
    base_url = "https://openrouter.ai/api/v1"
    client = OpenAIClient(
        api_key="test-key",
        model=model,
        model_parameters=model_parameters,
        base_url=base_url,
    )
    client.client = AsyncOpenAI(
        api_key="test-key",
        base_url=base_url,
        http_client=httpx.AsyncClient(
            transport=_recording_transport(_CHAT_COMPLETION_REPLY, requests)
        ),
    )
    return client


async def test_openrouter_extension_parameters_reach_the_request() -> None:
    """OpenRouter's ``reasoning`` object is not an SDK keyword argument."""
    requests: list[dict[str, object]] = []
    client = _openai_chat_client(
        "openrouter/google/gemini-3.8-flash",
        {"openrouter/google/gemini-": {"reasoning": {"effort": "medium"}}},
        requests,
    )

    await client.generate_response([UserMessage(content="hi")])

    assert requests[0]["reasoning"] == {"effort": "medium"}


async def test_extension_parameters_merge_with_configured_extra_body() -> None:
    requests: list[dict[str, object]] = []
    client = _openai_chat_client(
        "openrouter/google/gemini-3.8-flash",
        {
            "openrouter/": {
                "reasoning": {"effort": "low"},
                "extra_body": {"provider": {"sort": "price"}},
            }
        },
        requests,
    )

    await client.generate_response([UserMessage(content="hi")])

    assert requests[0]["reasoning"] == {"effort": "low"}
    assert requests[0]["provider"] == {"sort": "price"}


async def test_openai_sdk_parameters_are_sent_as_themselves() -> None:
    requests: list[dict[str, object]] = []
    client = _openai_chat_client(
        "openrouter/openai/gpt-5.6-terra",
        {"gpt-5.6-terra": {"reasoning_effort": "high", "temperature": 0.2}},
        requests,
    )

    await client.generate_response([UserMessage(content="hi")])

    assert requests[0]["reasoning_effort"] == "high"
    assert requests[0]["temperature"] == 0.2


def test_anthropic_base_url_reaches_the_sdk_client() -> None:
    client = AnthropicClient(
        api_key="test-key",
        model="claude-opus-5-5",
        base_url="https://anthropic.example.test",
    )

    assert str(client.client.base_url).rstrip("/") == "https://anthropic.example.test"
    assert "base_url" not in client.default_kwargs
