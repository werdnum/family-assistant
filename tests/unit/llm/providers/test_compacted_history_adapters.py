"""Every adapter renders a compacted turn from its messages alone.

A compacted turn carries no provider replay state, so each adapter has to
produce a well-formed request from the stub text and the activation pair.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from family_assistant.llm.deferred_tools import resolve_deferred_tools, tool_name
from family_assistant.llm.messages import (
    AssistantMessage,
    ImageUrlContentPart,
    LLMMessage,
    MessageWithMetadata,
    TextContentPart,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.anthropic_client import AnthropicClient
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.tool_call import ToolCallFunction, ToolCallItem
from family_assistant.processing.history_compaction import render_compacted_turn

if TYPE_CHECKING:
    from google.genai import types

    from family_assistant.tools.types import ToolDefinition

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
STUB = "[Called web_fetch at 2026-10-05 12:00; get_message_history retrieves"
ANSWER = "Here is what I found."


@pytest.fixture(autouse=True)
def _clear_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def _row(index: int, message: LLMMessage) -> MessageWithMetadata:
    return MessageWithMetadata(
        message=message,
        internal_id=str(index),
        interface_message_id=None,
        timestamp=NOW + timedelta(seconds=index),
        conversation_id="c",
        interface_type="telegram",
        turn_id="t",
    )


def _call(call_id: str, name: str) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=[
            ToolCallItem(
                id=call_id,
                type="function",
                function=ToolCallFunction(name=name, arguments="{}"),
            )
        ],
        provider_metadata={"provider": "anthropic", "thinking_blocks": []},
    )


def _tool(name: str, *, deferred: bool = False) -> ToolDefinition:
    definition: ToolDefinition = {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    if deferred:
        definition["defer_loading"] = True
    return definition


TOOLS = [_tool("activate_tools"), _tool("web_fetch", deferred=True)]


def _compacted_history() -> list[LLMMessage]:
    turn = [
        _row(
            0,
            UserMessage(
                content=[
                    TextContentPart(type="text", text="Look this up"),
                    ImageUrlContentPart(
                        type="image_url",
                        image_url={"url": "/api/attachments/att-1"},
                        attachment_id="att-1",
                    ),
                ]
            ),
        ),
        _row(1, _call("c1", "activate_tools")),
        _row(
            2,
            ToolMessage(
                tool_call_id="c1",
                name="activate_tools",
                content="Activated web_fetch",
                activated_tools=["web_fetch"],
            ),
        ),
        _row(3, _call("c2", "web_fetch")),
        _row(4, ToolMessage(tool_call_id="c2", name="web_fetch", content="x" * 5_000)),
        _row(5, AssistantMessage(content=ANSWER)),
    ]
    return [
        *render_compacted_turn(turn, ZoneInfo("UTC")),
        UserMessage(content="And now?"),
    ]


def _anthropic_blocks(
    # ast-grep-ignore: no-dict-any - messages.create kwargs are heterogeneous
    params: dict[str, Any],
    role: str,
    block_type: str,
    # ast-grep-ignore: no-dict-any - content blocks are heterogeneous
) -> list[dict[str, Any]]:
    return [
        block
        for message in params["messages"]
        if message["role"] == role and isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == block_type
    ]


class TestAnthropic:
    @pytest.fixture
    def client(self) -> AnthropicClient:
        return AnthropicClient(api_key="test-key", model="claude-sonnet-5-5")

    def test_compacted_assistant_messages_carry_no_thinking(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        for message in params["messages"]:
            if message["role"] == "assistant":
                assert not any(
                    block["type"] in {"thinking", "redacted_thinking"}
                    for block in message["content"]
                )

    def test_the_stub_and_answer_are_an_assistant_text_block(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        texts = [b["text"] for b in _anthropic_blocks(params, "assistant", "text")]
        assert any(STUB in text and text.endswith(ANSWER) for text in texts)

    def test_the_activation_call_and_result_are_paired(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        uses = _anthropic_blocks(params, "assistant", "tool_use")
        results = _anthropic_blocks(params, "user", "tool_result")
        assert [(use["id"], use["name"]) for use in uses] == [("c1", "activate_tools")]
        assert [result["tool_use_id"] for result in results] == ["c1"]

    def test_the_activated_tool_is_added_after_its_result(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        messages = params["messages"]
        result_index = next(
            i
            for i, m in enumerate(messages)
            if m["role"] == "user"
            and isinstance(m["content"], list)
            and any(b["type"] == "tool_result" for b in m["content"])
        )
        addition = messages[result_index + 1]
        assert addition["role"] == "system"
        assert addition["content"] == [
            {
                "type": "tool_addition",
                "tool": {"type": "tool_reference", "name": "web_fetch"},
            }
        ]
        assert [t["name"] for t in params["tools"]] == ["activate_tools", "web_fetch"]

    def test_roles_alternate_and_end_on_the_new_user_message(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        roles = [m["role"] for m in params["messages"] if m["role"] != "system"]
        assert roles == ["user", "assistant", "user", "assistant", "user"]

    def test_the_attachment_is_a_text_reference_not_an_image(
        self, client: AnthropicClient
    ) -> None:
        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        assert not _anthropic_blocks(params, "user", "image")
        assert any(
            "att-1" in block["text"]
            for block in _anthropic_blocks(params, "user", "text")
        )

    def test_a_model_without_mid_conversation_changes_offers_the_activated_tool(
        self,
    ) -> None:
        client = AnthropicClient(api_key="test-key", model="claude-sonnet-4-6")

        params = client._assemble_request(_compacted_history(), TOOLS, "auto")

        assert [t["name"] for t in params["tools"]] == ["activate_tools", "web_fetch"]
        assert all("defer_loading" not in t for t in params["tools"])


class TestOpenAIResponses:
    @pytest.fixture
    def client(self) -> OpenAIClient:
        return OpenAIClient(api_key="test-key", model="gpt-5.6-sol")

    def test_the_stub_and_answer_are_replayed_from_the_message(
        self, client: OpenAIClient
    ) -> None:
        items = client._messages_to_responses_input(_compacted_history())

        assistant_texts = [
            part["text"]
            for item in items
            if item.get("role") == "assistant" and isinstance(item["content"], list)
            for part in item["content"]
        ]
        assert any(STUB in text and text.endswith(ANSWER) for text in assistant_texts)

    def test_no_provider_output_is_replayed(self, client: OpenAIClient) -> None:
        items = client._messages_to_responses_input(_compacted_history())

        assert not any(item.get("type") == "reasoning" for item in items)
        assert not any("status" in item for item in items)

    def test_the_activation_call_and_output_are_paired(
        self, client: OpenAIClient
    ) -> None:
        items = client._messages_to_responses_input(_compacted_history())

        calls = [i for i in items if i.get("type") == "function_call"]
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        assert [(c["call_id"], c["name"]) for c in calls] == [("c1", "activate_tools")]
        assert [o["call_id"] for o in outputs] == ["c1"]
        assert items.index(calls[0]) < items.index(outputs[0])

    def test_the_attachment_is_a_text_reference_not_an_image(
        self, client: OpenAIClient
    ) -> None:
        items = client._messages_to_responses_input(_compacted_history())

        user_parts = [
            part
            for item in items
            if item.get("role") == "user" and isinstance(item["content"], list)
            for part in item["content"]
        ]
        assert not any(part["type"] == "input_image" for part in user_parts)
        assert any("att-1" in part.get("text", "") for part in user_parts)

    def test_the_activated_tool_is_offered(self) -> None:
        offered = resolve_deferred_tools(TOOLS, _compacted_history())

        assert offered is not None
        assert [tool_name(tool) for tool in offered] == ["activate_tools", "web_fetch"]
        assert all("defer_loading" not in tool for tool in offered)


class TestGemini:
    @pytest.fixture
    def client(self) -> GoogleGenAIClient:
        return GoogleGenAIClient(api_key="test-key", model="gemini-3.8-flash")

    def _contents(self, client: GoogleGenAIClient) -> list[types.Content]:
        messages = client._process_tool_messages(_compacted_history())
        return cast(
            "list[types.Content]", client._convert_messages_to_genai_format(messages)
        )

    def test_contents_alternate_and_end_on_the_new_user_message(
        self, client: GoogleGenAIClient
    ) -> None:
        contents = self._contents(client)

        roles = [content.role for content in contents]
        assert roles == ["user", "model", "user", "model", "user"]

    def test_the_function_call_and_response_are_paired(
        self, client: GoogleGenAIClient
    ) -> None:
        contents = self._contents(client)

        parts = [part for content in contents for part in content.parts or []]
        calls = [p.function_call for p in parts if p.function_call]
        responses = [p.function_response for p in parts if p.function_response]
        assert [c.name for c in calls] == ["activate_tools"]
        assert [r.name for r in responses] == ["activate_tools"]
        assert contents[1].parts is not None
        assert contents[1].parts[0].function_call is not None
        assert contents[2].parts is not None
        assert contents[2].parts[0].function_response is not None

    def test_the_stub_and_answer_are_a_model_text_part(
        self, client: GoogleGenAIClient
    ) -> None:
        contents = self._contents(client)

        texts = [
            part.text
            for content in contents
            if content.role == "model"
            for part in content.parts or []
            if part.text
        ]
        assert any(STUB in text and text.endswith(ANSWER) for text in texts)

    def test_the_activated_tool_is_offered(self, client: GoogleGenAIClient) -> None:
        offered = resolve_deferred_tools(TOOLS, _compacted_history())

        declarations = [
            declaration.name
            for tool in client._prepare_all_tools(offered)
            for declaration in getattr(tool, "function_declarations", None) or []
        ]
        assert declarations == ["activate_tools", "web_fetch"]
