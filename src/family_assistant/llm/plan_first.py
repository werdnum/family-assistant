"""Run an OpenAI model on the signed-in ChatGPT plan, falling back to the API key.

When someone has signed in with ChatGPT, every direct-OpenAI chat model tries
the plan first. Nobody signed in means straight to the API key, with no failed
attempt first. A plan failure before anything was streamed -- usage limit,
refused model or parameter, lapsed sign-in -- is answered by the API key. A
refusal that will repeat pauses the plan for that model for a while, so it costs
one wasted attempt rather than one per call.

See docs/design/chatgpt-plan-usage.md.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import time
from typing import TYPE_CHECKING, TypeVar

from opentelemetry import trace
from pydantic import BaseModel

from family_assistant.llm.base import (
    AuthenticationError,
    InvalidRequestError,
    LLMProviderError,
    ModelNotFoundError,
    RateLimitError,
    stream_error_to_exception,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from family_assistant.llm import (
        JsonObject,
        LLMInterface,
        LLMMessage,
        LLMOutput,
        LLMStreamEvent,
        UserMessageDict,
    )
    from family_assistant.llm.connections import PlanTokenSource
    from family_assistant.llm.messages import UserMessage
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

__all__ = ["PlanFirstClient"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

# A refusal of the model or its request shape, or of the account's eligibility,
# repeats until something outside this process changes.
_REFUSAL_COOLDOWN_SECONDS = 3600.0
# A usage limit lifts on the plan's own window, which is hours; checking again
# after a while finds out without spending an attempt on every call.
_USAGE_LIMIT_COOLDOWN_SECONDS = 900.0


def _cooldown_for(error: LLMProviderError) -> float | None:
    if isinstance(error, RateLimitError):
        return _USAGE_LIMIT_COOLDOWN_SECONDS
    if isinstance(error, (InvalidRequestError, ModelNotFoundError)):
        return _REFUSAL_COOLDOWN_SECONDS
    # Eligibility refusals; a lapsed sign-in shows up as signed out instead.
    if isinstance(error, AuthenticationError):
        return _REFUSAL_COOLDOWN_SECONDS
    return None


class PlanFirstClient:
    """The plan client while it serves, and the API-key client otherwise."""

    def __init__(
        self,
        plan: LLMInterface,
        api: LLMInterface | None,
        *,
        model: str,
        source: PlanTokenSource,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self.api = api
        self.plan_model = model
        self._source = source
        self._clock = clock
        self._paused_until: float | None = None

    def __getattr__(self, name: str) -> object:
        # Duck-typed reads (model, provider) see the API client's, or the
        # plan's where there is no API key. Guarded so a half-built copy does
        # not recurse.
        if name in {"plan", "api"}:
            raise AttributeError(name)
        return getattr(self.api if self.api is not None else self.plan, name)

    @property
    def plan_paused(self) -> bool:
        return self._paused_until is not None and self._clock() < self._paused_until

    async def _use_plan(self) -> bool:
        return not self.plan_paused and await self._source.available()

    def _fall_back(self, error: LLMProviderError) -> None:
        cooldown = _cooldown_for(error)
        if cooldown is not None:
            self._paused_until = self._clock() + cooldown
        logger.warning(
            "ChatGPT plan request for %s failed (%s: %s); using the API key%s.",
            self.plan_model,
            type(error).__name__,
            error,
            f" and pausing the plan for this model for {cooldown:.0f}s"
            if cooldown
            else "",
        )
        trace.get_current_span().add_event(
            "llm.chatgpt_plan_fallback",
            attributes={"model": self.plan_model, "error_type": type(error).__name__},
        )

    async def _run(self, invoke: Callable[[LLMInterface], Awaitable[R]]) -> R:
        api = self.api
        # Without an API key the plan is the only route, and its own error is
        # the answer when it fails.
        if api is None:
            return await invoke(self.plan)
        if not await self._use_plan():
            return await invoke(api)
        try:
            return await invoke(self.plan)
        except LLMProviderError as error:
            self._fall_back(error)
        return await invoke(api)

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        return await self._run(
            lambda client: client.generate_response(messages, tools, tool_choice)
        )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        api = self.api
        if api is None or not await self._use_plan():
            async for event in (api or self.plan).generate_response_stream(
                messages, tools, tool_choice
            ):
                yield event
            return
        stream = self.plan.generate_response_stream(messages, tools, tool_choice)
        try:
            first = await anext(stream)
            # A failure before anything was produced arrives as a leading
            # error event: the request never started, so the API key may.
            if first.type == "error":
                error = stream_error_to_exception(first)
                if isinstance(error, LLMProviderError):
                    raise error
        except StopAsyncIteration:
            return
        except LLMProviderError as error:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    closing = aclose()
                    if inspect.isawaitable(closing):
                        await closing
            self._fall_back(error)
        else:
            # Once the plan has answered, a later failure is the answer's.
            yield first
            async for event in stream:
                yield event
            return
        async for event in api.generate_response_stream(messages, tools, tool_choice):
            yield event

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        return await self._run(
            lambda client: client.generate_structured(
                messages, response_model, max_retries
            )
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        return await self._run(
            lambda client: client.generate_json(messages, max_retries)
        )

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        return await (self.api or self.plan).format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def create_attachment_injection(self, attachment: ToolAttachment) -> UserMessage:
        return (self.api or self.plan).create_attachment_injection(attachment)

    async def close(self) -> None:
        for client in (self.plan, self.api):
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
