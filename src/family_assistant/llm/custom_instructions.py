"""Operator-written instructions appended to every system prompt a model gets.

Some models have habits worth correcting for wherever they are used -- drafting
messages nobody asked for, hedging with caveats nobody needed. The correction
belongs to the model, not to a profile: the same profile can run on several
models through tiers, fallbacks and experiments, and the habit follows the
model.

The instructions are configured as the reserved ``custom_instructions`` key of
the model-keyed ``llm_parameters`` map, so a chain entry's own
``llm_parameters`` overlay can scope them to one tier. The factory strips the
key before the provider client sees the map -- it is not a request parameter --
and wraps the client in ``CustomInstructionsClient``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from family_assistant.llm.messages import SystemMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from family_assistant.llm import (
        JsonObject,
        LLMInterface,
        LLMMessage,
        LLMOutput,
        LLMStreamEvent,
        UserMessageDict,
    )
    from family_assistant.llm.messages import UserMessage
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

__all__ = [
    "CUSTOM_INSTRUCTIONS_KEY",
    "CustomInstructionsClient",
    "split_custom_instructions",
]

T = TypeVar("T", bound=BaseModel)

CUSTOM_INSTRUCTIONS_KEY = "custom_instructions"


def split_custom_instructions(
    model: str,
    model_parameters: Mapping[str, Mapping[str, object]],
) -> tuple[str | None, dict[str, dict[str, object]]]:
    """The instructions for ``model``, and the parameter map without them.

    Every pattern matching ``model`` contributes, in map order, so a broad
    pattern ("gpt-") and a specific one ("gpt-5.5") combine rather than one
    silently shadowing the other. The key is removed from every pattern, not
    only matching ones, because the provider clients validate or forward
    whatever they are given.
    """
    parts: list[str] = []
    stripped: dict[str, dict[str, object]] = {}
    for pattern, params in model_parameters.items():
        rest = {k: v for k, v in params.items() if k != CUSTOM_INSTRUCTIONS_KEY}
        stripped[pattern] = rest
        if pattern not in model or CUSTOM_INSTRUCTIONS_KEY not in params:
            continue
        value = params[CUSTOM_INSTRUCTIONS_KEY]
        if not isinstance(value, str):
            msg = (
                f"llm_parameters[{pattern!r}].{CUSTOM_INSTRUCTIONS_KEY} must be "
                f"a string, got {type(value).__name__}."
            )
            raise TypeError(msg)
        text = value.strip()
        if text and text not in parts:
            parts.append(text)
    return ("\n\n".join(parts) or None), stripped


def with_custom_instructions(
    messages: Sequence[LLMMessage], instructions: str
) -> list[LLMMessage]:
    """``messages`` with ``instructions`` at the end of the system prompt.

    Appended to the first system message, so they sit after the profile's own
    prompt and read as the last word on style. A conversation with no system
    message gets one. When the whole of the original prompt was marked stable,
    the cache breakpoint moves to the new end: the instructions are as constant
    as the prompt they follow. A prompt with a volatile tail keeps its
    breakpoint, and the instructions ride in the uncached tail.
    """
    result = list(messages)
    for index, message in enumerate(result):
        if not isinstance(message, SystemMessage):
            continue
        content = f"{message.content.rstrip()}\n\n{instructions}".lstrip()
        stable = message.stable_prefix_len
        if stable is not None and stable >= len(message.content):
            stable = len(content)
        result[index] = message.model_copy(
            update={"content": content, "stable_prefix_len": stable}
        )
        return result
    result.insert(
        0, SystemMessage(content=instructions, stable_prefix_len=len(instructions))
    )
    return result


class CustomInstructionsClient:
    """Adds a model's custom instructions to every request sent to it."""

    def __init__(self, inner: LLMInterface, *, instructions: str) -> None:
        self.inner = inner
        self.custom_instructions = instructions

    def __getattr__(self, name: str) -> object:
        # Duck-typed reads of the model or provider see the wrapped client's.
        # `inner` lands here only before __init__ has set it, where delegating
        # would recurse.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    def _messages(self, messages: Sequence[LLMMessage]) -> list[LLMMessage]:
        return with_custom_instructions(messages, self.custom_instructions)

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        return await self.inner.generate_response(
            self._messages(messages), tools, tool_choice
        )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        async for event in self.inner.generate_response_stream(
            self._messages(messages), tools, tool_choice
        ):
            yield event

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        return await self.inner.generate_structured(
            self._messages(messages), response_model, max_retries
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        return await self.inner.generate_json(self._messages(messages), max_retries)

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        return await self.inner.format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        return self.inner.create_attachment_injection(attachment)

    async def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
