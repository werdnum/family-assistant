"""Gemini chat on the Interactions API, recorded against the real API.

These pin what the unit tests can only assume: that the API accepts the steps
the client replays (signatures, tool results, history begun on
generateContent), and that it accepts every tool schema we declare.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from family_assistant.llm import LLMStreamEvent, ToolCallItem
from family_assistant.llm.messages import (
    AssistantMessage,
    LLMMessage,
    ToolMessage,
    UserMessage,
)
from family_assistant.llm.providers.gemini_interactions import (
    stored_replay_steps,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.tools.catalog import TOOLS_DEFINITION
from family_assistant.tools.infrastructure import translate_attachment_schemas_for_llm

from .vcr_helpers import sanitize_response

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolDefinition

MODEL = "gemini-3.8-flash"

WEATHER_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather forecast for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}


def _client(*, use_interactions_api: bool = True) -> GoogleGenAIClient:
    return GoogleGenAIClient(
        api_key=os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        model=MODEL,
        use_interactions_api=use_interactions_api,
    )


def _skip_unless_replayable(llm_record_mode: str) -> None:
    if llm_record_mode != "replay" and not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Recording this test requires GEMINI_API_KEY")


async def _stream_turn(
    client: GoogleGenAIClient, messages: list[LLMMessage]
) -> AssistantMessage:
    """One streamed request, as the chat loop turns it into a message."""
    text: list[str] = []
    calls: list[ToolCallItem] = []
    done: LLMStreamEvent | None = None
    async for event in client.generate_response_stream(messages, tools=[WEATHER_TOOL]):
        if event.type == "content" and event.content:
            text.append(event.content)
        elif event.type == "tool_call" and event.tool_call:
            calls.append(event.tool_call)
        elif event.type == "done":
            done = event
    assert done is not None and done.metadata is not None
    return AssistantMessage(
        content="".join(text) or None,
        tool_calls=calls or None,
        provider_metadata=done.metadata.get("provider_metadata"),
    )


def _answer(messages: list[LLMMessage], assistant: AssistantMessage) -> None:
    messages.append(assistant)
    for call in assistant.tool_calls or ():
        messages.append(
            ToolMessage(
                tool_call_id=call.id,
                name=call.function.name,
                content=json.dumps({"forecast": "22C and sunny"}),
            )
        )


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_a_streamed_tool_call_is_replayed_into_the_next_turn(
    llm_record_mode: str,
) -> None:
    """A tool call streamed in one request is answered from replayed steps."""
    _skip_unless_replayable(llm_record_mode)
    client = _client()
    messages: list[LLMMessage] = [
        UserMessage(content="What's the weather in Sydney? Use the tool.")
    ]

    first = await _stream_turn(client, messages)
    _answer(messages, first)
    second = await client.generate_response(messages, tools=[WEATHER_TOOL])
    await client.close()

    assert first.tool_calls and first.tool_calls[0].function.name == "get_weather"
    assert stored_replay_steps(first), "the streamed steps were not recorded"
    assert second.content and "22" in second.content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_a_conversation_begun_on_generate_content_continues(
    llm_record_mode: str,
) -> None:
    """History saved before the switch replays with its generateContent signature."""
    _skip_unless_replayable(llm_record_mode)
    legacy = _client(use_interactions_api=False)
    messages: list[LLMMessage] = [
        UserMessage(content="What's the weather in Perth? Use the tool.")
    ]

    earlier = await legacy.generate_response(messages, tools=[WEATHER_TOOL])
    await legacy.close()
    _answer(
        messages,
        AssistantMessage(content=earlier.content, tool_calls=earlier.tool_calls),
    )
    client = _client()
    reply = await client.generate_response(messages, tools=[WEATHER_TOOL])
    await client.close()

    assert earlier.tool_calls, "the generateContent turn did not call the tool"
    assert reply.content and "22" in reply.content


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_every_catalog_tool_schema_is_accepted(llm_record_mode: str) -> None:
    """The whole local catalog, as advertised, in one request.

    Interactions rejects a request outright for a schema construct it does not
    accept, so one bad schema would fail every Gemini turn.
    """
    _skip_unless_replayable(llm_record_mode)
    client = _client()

    reply = await client.generate_response(
        [UserMessage(content="Say hello. Do not call any tool.")],
        tools=translate_attachment_schemas_for_llm(TOOLS_DEFINITION),
    )
    await client.close()

    assert reply.content


ACTIVATE_TOOL: ToolDefinition = {
    "type": "function",
    "function": {
        "name": "activate_tools",
        "description": "Activate on-demand tools by name so you can call them.",
        "parameters": {
            "type": "object",
            "properties": {
                "tool_names": {"type": "array", "items": {"type": "string"}}
            },
        },
    },
}


@pytest.mark.no_db
@pytest.mark.llm_integration
@pytest.mark.vcr(before_record_response=sanitize_response)
async def test_a_model_without_tool_retrieval_falls_back(llm_record_mode: str) -> None:
    """A model that refuses native retrieval still serves the turn, narrowed.

    gemini-3.5-flash-lite rejected client-side ``tool_search`` when this was
    recorded; the client resends without it and stops asking.
    """
    _skip_unless_replayable(llm_record_mode)
    client = GoogleGenAIClient(
        api_key=os.getenv("GEMINI_API_KEY", "test-gemini-key"),
        model="gemini-3.5-flash-lite",
    )

    reply = await client.generate_response(
        [UserMessage(content="Say hello.")],
        tools=[ACTIVATE_TOOL, {**WEATHER_TOOL, "defer_loading": True}],
    )
    await client.close()

    assert reply.content
    assert client._native_retrieval_enabled is False
