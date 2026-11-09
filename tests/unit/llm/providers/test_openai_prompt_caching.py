"""OpenAI Responses requests carry cache controls on the models that take them.

GPT-5.6 and later accept explicit cache breakpoints, cache lifetimes and
``additional_tools`` input items. These tests pin the request shape; the live
API's acceptance of it is recorded in
``tests/integration/llm/test_openai_prompt_caching.py``.
"""

import json

import httpx
import pytest
from openai import AsyncOpenAI

from family_assistant.llm import ToolCallFunction, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.request_buffer import get_request_buffer
from family_assistant.tools.types import ToolDefinition

PROMPT = "You are the household assistant.\n\nAlways be brief."
STABLE = len("You are the household assistant.")


@pytest.fixture(autouse=True)
def _clear_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def _tool(name: str, *, deferred: bool = False) -> ToolDefinition:
    tool: ToolDefinition = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    if deferred:
        tool["defer_loading"] = True
    return tool


TOOLS = [
    _tool("activate_tools"),
    _tool("set_lights", deferred=True),
    _tool("lock_door", deferred=True),
]


def _call(call_id: str, name: str) -> ToolCallItem:
    return ToolCallItem(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments="{}"),
    )


def _conversation() -> list[LLMMessage]:
    return [
        SystemMessage(content=PROMPT, stable_prefix_len=STABLE),
        UserMessage(content="Turn on the kitchen lights."),
    ]


def _input(params: dict[str, object]) -> list[dict[str, object]]:
    items = params["input"]
    assert isinstance(items, list)
    return items


def _cache_options(params: dict[str, object]) -> object:
    """The request's ``prompt_cache_options``, wherever the SDK boundary put it."""
    extra = params.get("extra_body")
    if isinstance(extra, dict) and "prompt_cache_options" in extra:
        return extra["prompt_cache_options"]
    return params.get("prompt_cache_options")


def _tools(params: dict[str, object]) -> list[dict[str, object]]:
    tools = params["tools"]
    assert isinstance(tools, list)
    return tools


def _breakpoints(params: dict[str, object]) -> list[dict[str, object]]:
    """Every ``prompt_cache_breakpoint`` in the request, in order."""
    found = []
    for item in _input(params):
        content = item.get("content")
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and "prompt_cache_breakpoint" in block:
                found.append(block["prompt_cache_breakpoint"])
    return found


class TestWhichModelsTakeCacheControls:
    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5.6-terra",
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6.1-terra",
            "gpt-6-astra",
            "gpt-5.10",
        ],
    )
    def test_gpt_5_6_and_later_on_the_responses_api(self, model: str) -> None:
        assert OpenAIClient(api_key="k", model=model)._supports_cache_controls()

    @pytest.mark.parametrize(
        "model", ["gpt-5.5", "gpt-5.2", "gpt-4.1", "gpt-4.1-nano", "some-other-model"]
    )
    def test_earlier_or_unversioned_models_are_left_alone(self, model: str) -> None:
        """They reject the fields with a 400, so missing out costs a cache hit,
        never a request."""
        assert not OpenAIClient(api_key="k", model=model)._supports_cache_controls()

    def test_chat_completions_is_left_alone(self) -> None:
        client = OpenAIClient(
            api_key="k",
            model="gpt-6-sol",
            model_parameters={"gpt-6-sol": {"use_responses_api": False}},
        )

        assert not client._supports_cache_controls()

    def test_an_openai_compatible_backend_is_left_alone(self) -> None:
        client = OpenAIClient(
            api_key="k", model="gpt-6-sol", base_url="https://openrouter.ai/api/v1"
        )

        assert not client._supports_cache_controls()


class TestStablePrefixBreakpoint:
    def test_stable_system_prompt_is_kept_for_half_a_day(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        params = client._build_responses_params(
            _conversation(), TOOLS, "auto", stream=True
        )

        system = _input(params)[0]
        assert system == {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": PROMPT[:STABLE],
                    "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "12h"},
                },
                {"type": "input_text", "text": PROMPT[STABLE:]},
            ],
        }

    def test_fully_stable_prompt_is_one_marked_block(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-6-sol")
        messages: list[LLMMessage] = [
            SystemMessage(content=PROMPT, stable_prefix_len=len(PROMPT)),
            UserMessage(content="Hi"),
        ]

        params = client._build_responses_params(messages, None, None, stream=True)

        assert _input(params)[0]["content"] == [
            {
                "type": "input_text",
                "text": PROMPT,
                "prompt_cache_breakpoint": {"mode": "explicit", "ttl": "12h"},
            }
        ]

    def test_a_prompt_with_no_stable_prefix_is_not_marked(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-6-sol")
        messages: list[LLMMessage] = [
            SystemMessage(content=PROMPT),
            UserMessage(content="Hi"),
        ]

        params = client._build_responses_params(messages, None, None, stream=True)

        assert _input(params)[0] == {"role": "system", "content": PROMPT}
        assert _breakpoints(params) == []

    def test_a_later_system_notice_is_never_marked(self) -> None:
        """Only the prompt every request opens with is shared; a notice is
        conversation content and stays where it occurred, unchanged."""
        client = OpenAIClient(api_key="k", model="gpt-6-sol")
        messages = [
            *_conversation(),
            AssistantMessage(content="Done."),
            SystemMessage(content="Delegation 7 finished.", stable_prefix_len=5),
            UserMessage(content="Thanks"),
        ]

        params = client._build_responses_params(messages, None, None, stream=True)

        assert {"role": "system", "content": "Delegation 7 finished."} in _input(params)
        assert len(_breakpoints(params)) == 1


class TestCachingMode:
    def test_the_agent_loop_keeps_the_implicit_breakpoint(self) -> None:
        """It caches each tool round at the end of the latest message, which
        the next request in the loop reads back."""
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        params = client._build_responses_params(
            _conversation(), TOOLS, "auto", stream=True
        )

        assert _cache_options(params) == {"mode": "implicit"}

    def test_a_one_shot_request_writes_nothing_past_the_shared_prefix(self) -> None:
        """Its tail is unique, so a write there is billed at 1.25x and never read."""
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        params = client._build_responses_params(
            _conversation(), None, None, stream=False
        )

        assert _cache_options(params) == {"mode": "explicit"}
        assert _breakpoints(params) == [{"mode": "explicit", "ttl": "12h"}]

    def test_an_earlier_model_gets_no_cache_fields_at_all(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-5.2")

        params = client._build_responses_params(
            _conversation(), TOOLS, "auto", stream=True
        )

        assert _cache_options(params) is None
        assert _input(params)[0] == {"role": "system", "content": PROMPT}


class TestDeferredTools:
    def _after_activation(self, *, parallel: bool = False) -> list[LLMMessage]:
        calls = [_call("call_1", "activate_tools")]
        results: list[LLMMessage] = [
            ToolMessage(
                tool_call_id="call_1",
                name="activate_tools",
                content="Activated tools: set_lights.",
                activated_tools=["set_lights"],
            )
        ]
        if parallel:
            calls.append(_call("call_2", "activate_tools"))
            results.append(
                ToolMessage(
                    tool_call_id="call_2", name="activate_tools", content="Nothing new."
                )
            )
        return [
            *_conversation(),
            AssistantMessage(content=None, tool_calls=calls),
            *results,
        ]

    def test_the_tools_array_never_carries_a_deferred_tool(self) -> None:
        """Loading a tool must not change the array every earlier byte follows."""
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        before = client._build_responses_params(
            _conversation(), TOOLS, "auto", stream=True
        )
        after = client._build_responses_params(
            self._after_activation(), TOOLS, "auto", stream=True
        )

        assert [t["name"] for t in _tools(before)] == ["activate_tools"]
        assert _tools(after) == _tools(before)

    def test_an_activation_surfaces_the_tool_after_its_output(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        params = client._build_responses_params(
            self._after_activation(), TOOLS, "auto", stream=True
        )

        items = _input(params)
        assert items[-2]["type"] == "function_call_output"
        assert items[-1] == {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                {
                    "type": "function",
                    "name": "set_lights",
                    "description": "The set_lights tool.",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": False,
                }
            ],
        }

    def test_the_addition_waits_for_the_end_of_a_parallel_batch(self) -> None:
        client = OpenAIClient(api_key="k", model="gpt-6-sol")

        params = client._build_responses_params(
            self._after_activation(parallel=True), TOOLS, "auto", stream=True
        )

        kinds = [item.get("type") for item in _input(params)]
        assert kinds[-3:] == [
            "function_call_output",
            "function_call_output",
            "additional_tools",
        ]

    def test_an_earlier_model_still_narrows_the_array(self) -> None:
        """Without ``additional_tools`` the activated tool has to be in the array."""
        client = OpenAIClient(api_key="k", model="gpt-5.2")

        params = client._build_responses_params(
            self._after_activation(), TOOLS, "auto", stream=True
        )

        assert [t["name"] for t in _tools(params)] == ["activate_tools", "set_lights"]
        assert all(item.get("type") != "additional_tools" for item in _input(params))


_CHAT_COMPLETION_REPLY = {
    "id": "gen-1",
    "object": "chat.completion",
    "created": 1760400000,
    "model": "openai/gpt-5.4",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}


class TestTelemetry:
    async def test_diagnostics_record_the_tools_the_request_sent(self) -> None:
        sent: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=_CHAT_COMPLETION_REPLY)

        base_url = "https://openrouter.ai/api/v1"
        client = OpenAIClient(
            api_key="test", model="openrouter/openai/gpt-5.4", base_url=base_url
        )
        client.client = AsyncOpenAI(
            api_key="test",
            base_url=base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        buffer = get_request_buffer()
        buffer.clear()

        await client.generate_response(_conversation(), TOOLS)

        [record] = buffer.get_recent(limit=1)
        buffer.clear()
        recorded = [tool["function"]["name"] for tool in record.tools or ()]
        sent_tools = sent[0]["tools"]
        assert isinstance(sent_tools, list)
        assert recorded == [tool["function"]["name"] for tool in sent_tools]
        assert recorded == ["activate_tools"]
