"""The Gemini request for a conversation is a prefix of the next one's.

Gemini caches implicitly, so the only lever FA has is to send each request as
an extension of the previous one: there are no breakpoints to place, and no
diagnostics that would report a prefix that broke. This test is therefore the
only guard against a conversion that starts re-rendering history -- replaying
a thought signature differently, moving a system notice, reshaping a message.

One difference is allowed, and pinned: Gemini has no way to add a tool
mid-conversation, so activating a deferred tool grows the tool list, which
sits ahead of the contents and costs that request its cache. The list may
change only when a tool was activated since the previous request, and only by
growing. Preloading on-demand tools instead was measured to cost several times
more than the misses it avoids; see docs/design/prompt-caching.md.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from family_assistant.llm.deferred_tools import (
    activated_tool_names,
    resolve_deferred_tools,
    tool_name,
)
from family_assistant.llm.google_types import (
    GeminiProviderMetadata,
    GeminiThoughtSignature,
)
from family_assistant.llm.messages import AssistantMessage, LLMMessage, UserMessage
from family_assistant.llm.providers.gemini_interactions import (
    interaction_steps_metadata,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.storage.delegation_runs import delegation_wake_instruction
from tests.unit.llm.agent_loop_conversations import (
    STEP,
    TOOLS,
    Step,
    messages_for,
    opening,
    sends_a_request,
    wake_trigger,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm import JsonObject, JsonValue

MODEL = "gemini-3.8-flash"

Part = tuple[str, object]


def _decorate(
    message: AssistantMessage, index: int, thinking: bool
) -> AssistantMessage:
    """Attach a thought signature to each call, as a thinking turn stores it."""
    if not thinking or not message.tool_calls:
        return message
    # Stored form: what a persisted row replays.
    calls = [
        dataclasses.replace(
            call,
            provider_metadata=GeminiProviderMetadata(
                thought_signature=GeminiThoughtSignature(f"sig-{index}-{n}".encode())
            ).to_dict(),
        )
        for n, call in enumerate(message.tool_calls)
    ]
    return message.model_copy(update={"tool_calls": calls})


def _parts(client: GoogleGenAIClient, messages: Sequence[LLMMessage]) -> list[Part]:
    """Each part with the role it is sent under, as the streaming path renders it.

    Part-level, so the comparison does not depend on how consecutive messages
    are grouped into contents.
    """
    contents = client._convert_messages_to_genai_format(
        client._process_tool_messages(list(messages))
    )
    flat: list[Part] = []
    for content in contents:
        role = getattr(content, "role", None)
        for part in getattr(content, "parts", None) or ():
            flat.append((str(role), part.model_dump(exclude_none=True)))
    return flat


def _tool_names(messages: Sequence[LLMMessage]) -> list[str | None]:
    return [tool_name(tool) for tool in resolve_deferred_tools(TOOLS, messages) or ()]


def _decorate_with_recorded_steps(
    message: AssistantMessage, index: int, thinking: bool
) -> AssistantMessage:
    """Record the steps an Interactions turn stores for this message."""
    steps: list[JsonObject] = []
    if thinking:
        steps.append({"type": "thought", "signature": f"thought-{index}"})
    if message.content:
        steps.append({
            "type": "model_output",
            "content": [{"type": "text", "text": message.content}],
        })
    steps.extend(
        {
            "type": "function_call",
            "id": call.id,
            "name": call.function.name,
            "arguments": cast("JsonValue", call.function.arguments),
        }
        for call in message.tool_calls or ()
    )
    return message.model_copy(
        update={"provider_metadata": interaction_steps_metadata(steps)}
    )


def _interaction_steps(
    client: GoogleGenAIClient, messages: Sequence[LLMMessage]
) -> list[Part]:
    """The Interactions input steps a chat request carries."""
    settings = client._interaction_settings
    assert settings is not None
    request = client._interaction_chat_request(messages, None, None, settings)
    return [("step", step) for step in request["input"]]


Decorate = Callable[[AssistantMessage, int, bool], AssistantMessage]
Render = Callable[[GoogleGenAIClient, "Sequence[LLMMessage]"], "list[Part]"]


def _assert_each_request_extends_the_previous_one(
    steps: list[Step], render: Render, decorate: Decorate
) -> None:
    client = GoogleGenAIClient(api_key="test-key", model=MODEL)
    history = opening()
    instruction: str | None = None

    def request(messages: list[LLMMessage]) -> list[Part]:
        trailing = (
            [UserMessage(content=instruction, is_turn_scaffolding=True)]
            if instruction is not None
            else []
        )
        return render(client, [*messages, *trailing])

    previous = request(history)
    previous_instruction = instruction
    previous_tools = _tool_names(history)
    previous_activated = activated_tool_names(history)

    for index, step in enumerate(steps):
        history.extend(messages_for(step, index, history, decorate))
        if step.kind == "wake":
            instruction = delegation_wake_instruction(wake_trigger(index))
        elif step.kind == "user":
            instruction = None
        if not sends_a_request(history):
            continue
        current = request(history)
        tools = _tool_names(history)
        activated = activated_tool_names(history)

        if tools != previous_tools:
            assert activated != previous_activated
            assert set(previous_tools) < set(tools)
        earlier = previous
        if previous_instruction is not None:
            trailing = render(
                client,
                [UserMessage(content=previous_instruction, is_turn_scaffolding=True)],
            )
            assert earlier[-len(trailing) :] == trailing
            earlier = earlier[: -len(trailing)]
        assert current[: len(earlier)] == earlier
        previous = current
        previous_instruction = instruction
        previous_tools = tools
        previous_activated = activated


_PROPERTY_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@pytest.mark.no_db
class TestGooglePrefixStability:
    @given(st.lists(STEP, min_size=1, max_size=12))
    @_PROPERTY_SETTINGS
    def test_each_request_extends_the_previous_one(self, steps: list[Step]) -> None:
        _assert_each_request_extends_the_previous_one(steps, _parts, _decorate)

    @given(st.lists(STEP, min_size=1, max_size=12))
    @_PROPERTY_SETTINGS
    def test_each_interactions_request_extends_the_previous_one(
        self, steps: list[Step]
    ) -> None:
        _assert_each_request_extends_the_previous_one(
            steps, _interaction_steps, _decorate_with_recorded_steps
        )

    @given(st.lists(STEP, min_size=1, max_size=12))
    @_PROPERTY_SETTINGS
    def test_history_from_generate_content_extends_on_interactions(
        self, steps: list[Step]
    ) -> None:
        _assert_each_request_extends_the_previous_one(
            steps, _interaction_steps, _decorate
        )
