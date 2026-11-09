"""A model's ``custom_instructions`` reach every request sent to that model."""

from collections.abc import AsyncIterator, Sequence

import pytest
from pydantic import BaseModel

from family_assistant.config_models import ModelTierConfig, RetryModelConfig
from family_assistant.llm import JsonObject, LLMOutput, LLMStreamEvent, UserMessageDict
from family_assistant.llm.custom_instructions import (
    CustomInstructionsClient,
    split_custom_instructions,
    with_custom_instructions,
)
from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.messages import LLMMessage, SystemMessage, UserMessage
from family_assistant.llm.model_tiers import resolve_tier_client_config
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.providers.openai_client import OpenAIClient
from family_assistant.llm.retrying_client import RetryingLLMClient
from family_assistant.tools.types import ToolAttachment, ToolDefinition


class _Answer(BaseModel):
    text: str


class RecordingClient:
    """A fake provider client that keeps the messages each call was sent."""

    def __init__(self) -> None:
        self.model = "gpt-6-luna"
        self.calls: list[list[LLMMessage]] = []

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        self.calls.append(list(messages))
        return LLMOutput(content="ok")

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        self.calls.append(list(messages))
        yield LLMStreamEvent(type="content", content="ok")

    async def generate_structured[T: BaseModel](
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        self.calls.append(list(messages))
        return response_model.model_validate({"text": "ok"})

    async def generate_json(
        self, messages: Sequence[LLMMessage], max_retries: int = 2
    ) -> JsonObject:
        self.calls.append(list(messages))
        return {"ok": True}

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        return {"role": "user", "content": prompt_text or ""}

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        raise NotImplementedError


def _system_text(messages: Sequence[LLMMessage]) -> str:
    first = messages[0]
    assert isinstance(first, SystemMessage)
    return first.content


def test_every_matching_pattern_contributes_in_map_order() -> None:
    instructions, stripped = split_custom_instructions(
        "gpt-6-luna",
        {
            "gpt-": {"custom_instructions": "Broad.", "temperature": 0.2},
            "gemini-": {"custom_instructions": "Not this one."},
            "gpt-6": {"custom_instructions": "  Specific.\n"},
            "luna": {"custom_instructions": "Broad."},
        },
    )

    assert instructions == "Broad.\n\nSpecific."
    assert stripped == {
        "gpt-": {"temperature": 0.2},
        "gemini-": {},
        "gpt-6": {},
        "luna": {},
    }


def test_a_model_with_no_instructions_gets_none() -> None:
    instructions, stripped = split_custom_instructions(
        "gpt-6-luna", {"gpt-": {"temperature": 0.2}}
    )

    assert instructions is None
    assert stripped == {"gpt-": {"temperature": 0.2}}


def test_instructions_that_are_not_text_are_refused() -> None:
    with pytest.raises(TypeError, match="custom_instructions must be a string"):
        split_custom_instructions(
            "gpt-6-luna", {"gpt-": {"custom_instructions": ["a", "b"]}}
        )


def test_instructions_follow_the_profile_prompt_inside_the_cached_prefix() -> None:
    prompt = "You are a helpful assistant."
    original = [
        SystemMessage(content=prompt, stable_prefix_len=len(prompt)),
        UserMessage(content="hi"),
    ]

    sent = with_custom_instructions(original, "Be brief.")

    system = sent[0]
    assert isinstance(system, SystemMessage)
    assert system.content == "You are a helpful assistant.\n\nBe brief."
    assert system.stable_prefix_len == len(system.content)
    assert sent[1] == original[1]
    assert original[0].content == prompt


def test_a_volatile_tail_keeps_its_cache_breakpoint() -> None:
    original = [SystemMessage(content="stable|volatile", stable_prefix_len=6)]

    sent = with_custom_instructions(original, "Be brief.")

    system = sent[0]
    assert isinstance(system, SystemMessage)
    assert system.content == "stable|volatile\n\nBe brief."
    assert system.stable_prefix_len == 6


def test_a_request_with_no_system_prompt_gets_one() -> None:
    sent = with_custom_instructions([UserMessage(content="hi")], "Be brief.")

    assert _system_text(sent) == "Be brief."
    assert sent[1] == UserMessage(content="hi")


@pytest.mark.asyncio
async def test_every_call_path_carries_the_instructions() -> None:
    inner = RecordingClient()
    client = CustomInstructionsClient(inner, instructions="Be brief.")
    messages = [SystemMessage(content="Prompt."), UserMessage(content="hi")]

    await client.generate_response(messages)
    _ = [event async for event in client.generate_response_stream(messages)]
    await client.generate_structured(messages, _Answer)
    await client.generate_json(messages)

    assert [_system_text(call) for call in inner.calls] == ["Prompt.\n\nBe brief."] * 4
    assert client.model == "gpt-6-luna"


def test_the_factory_wraps_the_client_and_strips_the_key() -> None:
    client = LLMClientFactory.create_client({
        "model": "gpt-6-luna",
        "provider": "openai",
        "api_key": "test-key",
        "model_parameters": {
            "gpt-": {"custom_instructions": "Be brief.", "temperature": 0.2}
        },
    })

    assert isinstance(client, CustomInstructionsClient)
    assert client.custom_instructions == "Be brief."
    assert isinstance(client.inner, OpenAIClient)
    assert client.inner.model_parameters == {"gpt-": {"temperature": 0.2}}


def test_a_gemini_model_accepts_the_key_its_client_would_refuse() -> None:
    client = LLMClientFactory.create_client({
        "model": "gemini-3.8-flash",
        "provider": "google",
        "api_key": "test-key",
        "model_parameters": {"gemini-": {"custom_instructions": "Be brief."}},
    })

    assert isinstance(client, CustomInstructionsClient)
    assert isinstance(client.inner, GoogleGenAIClient)


def test_a_chain_entry_overlay_scopes_instructions_to_that_entry() -> None:
    tier = ModelTierConfig(
        chain=[
            RetryModelConfig(
                provider="openai",
                model="gpt-6-luna",
                llm_parameters={"custom_instructions": "Only on this tier."},
            ),
            RetryModelConfig(provider="google", model="gemini-3.8-flash"),
        ]
    )
    config = resolve_tier_client_config(tier, {})
    for entry in config["retry_config"].values():
        entry["api_key"] = "test-key"

    client = LLMClientFactory.create_client(config)

    assert isinstance(client, RetryingLLMClient)
    assert isinstance(client.primary_client, CustomInstructionsClient)
    assert client.primary_client.custom_instructions == "Only on this tier."
    assert isinstance(client.fallback_client, GoogleGenAIClient)
