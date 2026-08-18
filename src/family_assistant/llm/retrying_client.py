"""
LLM client that provides retry and fallback capabilities.

Retry strategy:
- Attempt 1: Primary model
- Attempt 2: Retry primary model (if Attempt 1 was a retriable error)
- Attempt 3: Fallback model (if configured and previous attempts failed)
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, TypeVar, cast

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

if TYPE_CHECKING:
    from family_assistant.tools.types import ToolAttachment, ToolDefinition

from . import (
    JsonObject,
    LLMInterface,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
    StructuredOutputError,
    UserMessageDict,
)
from .base import (
    LLMProviderError,
    ProviderConnectionError,
    ProviderTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from .messages import UserMessage
from .utils.usage_telemetry import set_usage_span_attributes


class _StreamTelemetry:
    """Retry-layer view of a streamed turn.

    The provider spans time a single attempt; this times what the caller
    actually waited for, which includes rate-limit sleeps and a fallback's
    whole second attempt. Time to first output is the number that matters for
    perceived latency, and only this layer can measure it end to end.
    """

    def __init__(self, span: trace.Span, requested_model: str) -> None:
        self._span = span
        self._requested_model = requested_model
        self._start = time.monotonic()
        self._first_output_at: float | None = None
        self._resolved_model: str | None = None

    def observe(self, event: LLMStreamEvent) -> None:
        """Fold one streamed event into the span's attributes."""
        if event.type == "error":
            # The consumer raises on this event, which closes this generator
            # where it is suspended at its `yield` -- `finish` never runs. A
            # failed turn recorded nowhere is the one worst worth losing, so it
            # is marked as the event passes through.
            self._span.set_status(StatusCode.ERROR, event.error or "stream failed")
            self._span.set_attribute(
                "llm.duration_ms", (time.monotonic() - self._start) * 1000
            )
            error_type = (event.metadata or {}).get("error_type")
            if isinstance(error_type, str):
                self._span.set_attribute("llm.error.type", error_type)
            return
        if (
            event.type in {"content", "thinking", "tool_call"}
            and self._first_output_at is None
        ):
            self._first_output_at = time.monotonic()
            self._span.set_attribute(
                "llm.time_to_first_output_ms",
                (self._first_output_at - self._start) * 1000,
            )
        if event.type != "done" or not event.metadata:
            return
        # Providers report streaming usage and the model they actually served in
        # the terminal `done` event rather than on a span of their own, so
        # reading it here is what makes those attributes present for every
        # provider instead of only the ones that instrument themselves.
        set_usage_span_attributes(self._span, event.metadata.get("reasoning_info"))
        self._resolved_model = event.metadata.get("resolved_model")

    def finish(self, *, model: str) -> None:
        """Record which model served the stream, and how long it all took."""
        self._span.set_attributes({
            "gen_ai.response.model": self._resolved_model or model,
            "llm.response.model_resolved": self._resolved_model is not None,
            "llm.duration_ms": (time.monotonic() - self._start) * 1000,
        })


def _note_attempt(
    span: trace.Span, *, attempts: int, model: str, is_fallback: bool
) -> None:
    """Mark an attempt as it starts, on the span as well as in an event.

    Set here rather than alongside a result so the count survives the paths
    where every attempt fails: those raise before any success-recording runs,
    and a failed turn is when the number of attempts is most worth having.
    """
    span.add_event(
        "llm.attempt",
        attributes={
            "attempt_number": attempts,
            "model": model,
            "is_fallback": is_fallback,
        },
    )
    span.set_attributes({"llm.attempts": attempts, "llm.fallback_used": is_fallback})


def _record_attempt_outcome(
    span: trace.Span,
    result: "LLMOutput",
    *,
    model: str,
) -> None:
    """Record the attempt that succeeded, and the model that actually served it."""
    span.set_attributes({
        "gen_ai.response.model": result.resolved_model or model,
        "llm.response.model_resolved": result.resolved_model is not None,
    })
    set_usage_span_attributes(span, result.reasoning_info)


T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RetryingLLMClient:
    """
    LLM client that provides retry and fallback capabilities.

    Retry logic:
    - One retry on primary model for retriable errors
    - Fallback to alternative model if all primary attempts fail
    """

    def __init__(
        self,
        primary_client: "LLMInterface",
        primary_model: str,
        fallback_client: "LLMInterface | None" = None,
        fallback_model: str | None = None,
    ) -> None:
        """
        Initialize the retrying client.

        Args:
            primary_client: The primary LLM client
            primary_model: Model name for the primary client (for logging)
            fallback_client: Optional fallback LLM client
            fallback_model: Model name for the fallback client (for logging)
        """
        self.primary_client = primary_client
        self.primary_model = primary_model
        self.fallback_client = fallback_client
        self.fallback_model = (
            fallback_model or "openai/gpt-5.6-terra"
        )  # Default fallback

        logger.info(
            f"RetryingLLMClient initialized with primary model: {primary_model}, "
            f"fallback model: {fallback_model if fallback_client else 'None'}"
        )

    async def generate_response(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> "LLMOutput":
        """Generate response with retry and fallback logic."""
        with tracer.start_as_current_span(
            "llm.generate",
            attributes={
                "gen_ai.request.model": self.primary_model,
                "llm.has_fallback": bool(self.fallback_client),
            },
        ) as span:
            # Define retriable errors
            retriable_errors = (
                ProviderConnectionError,
                ProviderTimeoutError,
                RateLimitError,
                ServiceUnavailableError,
            )

            last_exception: Exception | None = None
            # Counts provider calls actually issued, not the nominal slot: a
            # non-retriable failure on the primary skips straight to the
            # fallback, and reporting that as a third attempt would invent a
            # retry that never happened.
            attempts = 0

            # Attempt 1: Primary model
            attempts += 1
            _note_attempt(
                span,
                attempts=attempts,
                model=self.primary_model,
                is_fallback=False,
            )
            try:
                logger.info(f"Attempt 1: Primary model ({self.primary_model})")
                result = await self.primary_client.generate_response(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                )
                _record_attempt_outcome(span, result, model=self.primary_model)
                return result
            except retriable_errors as e:
                logger.warning(
                    f"Attempt 1 (Primary model {self.primary_model}) failed with retriable error: {e}. "
                    f"Retrying primary model."
                )
                last_exception = e
            except LLMProviderError as e:  # Non-retriable provider error
                logger.warning(
                    f"Attempt 1 (Primary model {self.primary_model}) failed with provider error: {e}. "
                    f"Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.exception(
                    f"Attempt 1 (Primary model {self.primary_model}) failed with unexpected error: {e}"
                )
                last_exception = e

            # Attempt 2: Retry Primary model (if Attempt 1 was a retriable error)
            if isinstance(last_exception, retriable_errors):
                attempts += 1
                _note_attempt(
                    span,
                    attempts=attempts,
                    model=self.primary_model,
                    is_fallback=False,
                )
                try:
                    logger.info(
                        f"Attempt 2: Retrying primary model ({self.primary_model})"
                    )
                    result = await self.primary_client.generate_response(
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    _record_attempt_outcome(span, result, model=self.primary_model)
                    return result
                except retriable_errors as e:
                    logger.warning(
                        f"Attempt 2 (Retry Primary model {self.primary_model}) failed with retriable error: {e}. "
                        f"Proceeding to fallback."
                    )
                    last_exception = e
                except LLMProviderError as e:  # Non-retriable provider error on retry
                    logger.warning(
                        f"Attempt 2 (Retry Primary model {self.primary_model}) failed with provider error: {e}. "
                        f"Proceeding to fallback."
                    )
                    last_exception = e
                except Exception as e:
                    logger.exception(
                        f"Attempt 2 (Retry Primary model {self.primary_model}) failed with unexpected error: {e}"
                    )
                    last_exception = e

            # Attempt 3: Fallback model (if configured and primary attempts failed)
            if self.fallback_client and last_exception:
                # Check if fallback model is same as primary
                if self.fallback_model == self.primary_model:
                    logger.warning(
                        f"Fallback model '{self.fallback_model}' is the same as the primary model '{self.primary_model}'. "
                        f"Skipping fallback."
                    )
                    if last_exception:
                        raise last_exception
                    # This case should ideally not happen
                    raise LLMProviderError(
                        message="All attempts failed without a specific error to raise.",
                        provider="unknown",
                        model=self.primary_model,
                    )

                attempts += 1
                _note_attempt(
                    span,
                    attempts=attempts,
                    model=self.fallback_model or "",
                    is_fallback=True,
                )
                logger.info(f"Attempt 3: Fallback model ({self.fallback_model})")
                try:
                    result = await self.fallback_client.generate_response(
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                    )
                    _record_attempt_outcome(
                        span, result, model=self.fallback_model or ""
                    )
                    return result
                except Exception as e:
                    logger.exception(
                        f"Attempt 3 (Fallback model {self.fallback_model}) also failed: {e}"
                    )
                    # Re-raise the last exception from primary attempts as it's likely more informative
                    raise last_exception from e

            # If all attempts failed, raise the last exception
            if last_exception:
                logger.error(
                    f"All LLM attempts failed. Raising last recorded exception: {last_exception}"
                )
                raise last_exception
            else:
                # Should not be reached if logic is correct, but as a safeguard:
                logger.error(
                    "All LLM attempts failed without a specific exception captured."
                )
                raise LLMProviderError(
                    message="All LLM attempts failed without a specific exception.",
                    provider="unknown",
                    model=self.primary_model,
                )

    async def generate_response_stream(
        self,
        messages: Sequence[LLMMessage],
        tools: "list[ToolDefinition] | None" = None,
        tool_choice: str | None = "auto",
    ) -> AsyncIterator[LLMStreamEvent]:
        """Generate streaming response with retry and fallback logic."""
        span = tracer.start_span(
            "llm.generate_stream",
            attributes={
                "gen_ai.request.model": self.primary_model,
                "llm.has_fallback": bool(self.fallback_client),
            },
        )
        stream_telemetry = _StreamTelemetry(span, self.primary_model)
        # Counts provider calls actually issued: a failure with no rate-limit
        # delay to wait out goes straight to the fallback, and the fallback is
        # then the second attempt rather than the third.
        attempts = 0
        try:
            with trace.use_span(span, end_on_exit=False):
                attempts += 1
                _note_attempt(
                    span,
                    attempts=attempts,
                    model=self.primary_model,
                    is_fallback=False,
                )
                logger.info(f"Streaming from primary model ({self.primary_model})")

                events_yielded = False
                try:
                    async for event in self.primary_client.generate_response_stream(
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                    ):
                        with trace.use_span(span, end_on_exit=False):
                            stream_telemetry.observe(event)
                            yield event  # noqa: ASYNC119
                        events_yielded = True
                    stream_telemetry.finish(model=self.primary_model)
                except Exception as e:
                    logger.exception(
                        f"Streaming from primary model {self.primary_model} failed: {e}"
                    )

                    # Only retry/fallback if no content has been sent to the client
                    if not events_yielded:
                        # For rate limits with a short retry_after, wait and retry on primary
                        if (
                            isinstance(e, RateLimitError)
                            and e.retry_after is not None
                            and e.retry_after <= 60
                        ):
                            span.add_event(
                                "llm.rate_limit_retry",
                                attributes={
                                    "retry_after_seconds": e.retry_after,
                                    "model": self.primary_model,
                                },
                            )
                            logger.info(
                                f"Rate limited. Waiting {e.retry_after:.0f}s before retrying primary model."
                            )
                            # ast-grep-ignore: no-asyncio-sleep-in-tests - Production retry delay, not test code
                            await asyncio.sleep(e.retry_after)
                            # Counted after the wait, not before it: a turn
                            # cancelled during the backoff never issues this
                            # call, and llm.attempts counts calls issued.
                            attempts += 1
                            _note_attempt(
                                span,
                                attempts=attempts,
                                model=self.primary_model,
                                is_fallback=False,
                            )
                            try:
                                async for (
                                    event
                                ) in self.primary_client.generate_response_stream(
                                    messages=messages,
                                    tools=tools,
                                    tool_choice=tool_choice,
                                ):
                                    events_yielded = True
                                    with trace.use_span(span, end_on_exit=False):
                                        stream_telemetry.observe(event)
                                        yield event  # noqa: ASYNC119
                                stream_telemetry.finish(model=self.primary_model)
                                return
                            except Exception as retry_err:
                                logger.warning(
                                    f"Retry after rate limit delay also failed: {retry_err}"
                                )
                                if events_yielded:
                                    span.set_status(StatusCode.ERROR, str(retry_err))
                                    span.record_exception(retry_err)
                                    raise retry_err
                                span.add_event(
                                    "llm.rate_limit_retry_failed",
                                    attributes={
                                        "error": str(retry_err),
                                        "model": self.primary_model,
                                    },
                                )
                                # Fall through to fallback logic below

                        if self.fallback_client:
                            # Check if fallback model is same as primary
                            if self.fallback_model == self.primary_model:
                                logger.warning(
                                    f"Fallback model '{self.fallback_model}' is the same as the primary model '{self.primary_model}'. "
                                    f"Skipping fallback."
                                )
                                span.set_status(StatusCode.ERROR, str(e))
                                span.record_exception(e)
                                raise

                            attempts += 1
                            _note_attempt(
                                span,
                                attempts=attempts,
                                model=self.fallback_model or "",
                                is_fallback=True,
                            )
                            span.add_event(
                                "llm.fallback",
                                attributes={
                                    "fallback_model": self.fallback_model or "",
                                    "original_error": str(e),
                                },
                            )
                            logger.info(
                                f"Falling back to {self.fallback_model} (no content emitted yet)"
                            )
                            try:
                                async for (
                                    event
                                ) in self.fallback_client.generate_response_stream(
                                    messages=messages,
                                    tools=tools,
                                    tool_choice=tool_choice,
                                ):
                                    with trace.use_span(span, end_on_exit=False):
                                        stream_telemetry.observe(event)
                                        yield event  # noqa: ASYNC119
                                stream_telemetry.finish(model=self.fallback_model or "")
                            except Exception as fallback_error:
                                logger.exception(
                                    f"Fallback streaming model {self.fallback_model} also failed: {fallback_error}"
                                )
                                # Raise the original error as it's likely more relevant
                                span.set_status(StatusCode.ERROR, str(e))
                                span.record_exception(e)
                                raise e from fallback_error
                        else:
                            logger.warning(
                                "Cannot fallback: No fallback client configured."
                            )
                            span.set_status(StatusCode.ERROR, str(e))
                            span.record_exception(e)
                            raise
                    else:
                        logger.warning(
                            "Cannot fallback: Content already emitted to stream."
                        )
                        span.set_status(StatusCode.ERROR, str(e))
                        span.record_exception(e)
                        raise
        finally:
            span.end()

    async def format_user_message_with_file(
        self,
        prompt_text: str | None,
        file_path: str | None,
        mime_type: str | None,
        max_text_length: int | None,
    ) -> UserMessageDict:
        """Format user message with file - delegates to primary client."""
        return await self.primary_client.format_user_message_with_file(
            prompt_text, file_path, mime_type, max_text_length
        )

    def create_attachment_injection(
        self,
        attachment: "ToolAttachment",
    ) -> UserMessage:
        """Create attachment injection - delegates to primary client."""
        return self.primary_client.create_attachment_injection(attachment)

    async def _run_non_streaming_with_retry(
        self,
        *,
        span_name: str,
        operation_name: str,
        invoke_primary: Callable[[], Awaitable[R]],
        invoke_fallback: Callable[[], Awaitable[R]],
    ) -> R:
        """Run a non-streaming operation with retry on the primary and fallback."""
        with tracer.start_as_current_span(
            span_name,
            attributes={
                "gen_ai.request.model": self.primary_model,
                "llm.has_fallback": bool(self.fallback_client),
            },
        ) as span:
            retriable_errors = (
                ProviderConnectionError,
                ProviderTimeoutError,
                RateLimitError,
                ServiceUnavailableError,
            )

            last_exception: Exception | None = None
            # Counts provider calls actually issued; see generate_response.
            attempts = 1

            _note_attempt(
                span,
                attempts=attempts,
                model=self.primary_model,
                is_fallback=False,
            )
            try:
                logger.info(
                    f"{operation_name} attempt 1: Primary model ({self.primary_model})"
                )
                result = await invoke_primary()
                span.set_attribute("gen_ai.response.model", self.primary_model)
                return result
            except retriable_errors as e:
                logger.warning(
                    f"{operation_name} attempt 1 (Primary model {self.primary_model}) "
                    f"failed with retriable error: {e}. Retrying primary model."
                )
                last_exception = e
            except StructuredOutputError as e:
                logger.warning(
                    f"{operation_name} attempt 1 (Primary model {self.primary_model}) "
                    f"failed with structured output error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except LLMProviderError as e:
                logger.warning(
                    f"{operation_name} attempt 1 (Primary model {self.primary_model}) "
                    f"failed with provider error: {e}. Proceeding to fallback."
                )
                last_exception = e
            except Exception as e:
                logger.exception(
                    f"{operation_name} attempt 1 (Primary model {self.primary_model}) "
                    f"failed with unexpected error: {e}"
                )
                last_exception = e

            if isinstance(last_exception, retriable_errors):
                attempts += 1
                _note_attempt(
                    span,
                    attempts=attempts,
                    model=self.primary_model,
                    is_fallback=False,
                )
                try:
                    logger.info(
                        f"{operation_name} attempt 2: Retrying primary model ({self.primary_model})"
                    )
                    result = await invoke_primary()
                    span.set_attribute("gen_ai.response.model", self.primary_model)
                    return result
                except retriable_errors as e:
                    logger.warning(
                        f"{operation_name} attempt 2 (Retry Primary model {self.primary_model}) "
                        f"failed with retriable error: {e}. Proceeding to fallback."
                    )
                    last_exception = e
                except LLMProviderError as e:
                    logger.warning(
                        f"{operation_name} attempt 2 (Retry Primary model {self.primary_model}) "
                        f"failed with provider error: {e}. Proceeding to fallback."
                    )
                    last_exception = e
                except Exception as e:
                    logger.exception(
                        f"{operation_name} attempt 2 (Retry Primary model {self.primary_model}) "
                        f"failed with unexpected error: {e}"
                    )
                    last_exception = e

            if self.fallback_client and last_exception:
                if self.fallback_model == self.primary_model:
                    logger.warning(
                        f"Fallback model '{self.fallback_model}' is the same as the primary model "
                        f"'{self.primary_model}'. Skipping fallback."
                    )
                    raise last_exception

                attempts += 1
                _note_attempt(
                    span,
                    attempts=attempts,
                    model=self.fallback_model or "",
                    is_fallback=True,
                )
                logger.info(
                    f"{operation_name} attempt 3: Fallback model ({self.fallback_model})"
                )
                try:
                    result = await invoke_fallback()
                    span.set_attribute(
                        "gen_ai.response.model", self.fallback_model or ""
                    )
                    return result
                except Exception as e:
                    logger.exception(
                        f"{operation_name} attempt 3 (Fallback model {self.fallback_model}) "
                        f"also failed: {e}"
                    )
                    raise last_exception from e

            if last_exception:
                logger.error(
                    f"All {operation_name.lower()} attempts failed. "
                    f"Raising last recorded exception: {last_exception}"
                )
                raise last_exception

            raise LLMProviderError(
                message=f"All {operation_name.lower()} attempts failed without a specific exception.",
                provider="unknown",
                model=self.primary_model,
            )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[T],
        max_retries: int = 2,
    ) -> T:
        """Generate structured response with retry and fallback logic.

        Note: The underlying client handles validation retries. This method only
        handles provider-level retries (connection errors, rate limits, etc.).
        """
        return await self._run_non_streaming_with_retry(
            span_name="llm.generate_structured",
            operation_name="Structured output",
            invoke_primary=lambda: self.primary_client.generate_structured(
                messages=messages,
                response_model=response_model,
                max_retries=max_retries,
            ),
            invoke_fallback=lambda: cast(
                "LLMInterface", self.fallback_client
            ).generate_structured(
                messages=messages,
                response_model=response_model,
                max_retries=max_retries,
            ),
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        """Generate JSON object response with retry and fallback logic."""
        return await self._run_non_streaming_with_retry(
            span_name="llm.generate_json",
            operation_name="JSON output",
            invoke_primary=lambda: self.primary_client.generate_json(
                messages=messages,
                max_retries=max_retries,
            ),
            invoke_fallback=lambda: cast(
                "LLMInterface", self.fallback_client
            ).generate_json(
                messages=messages,
                max_retries=max_retries,
            ),
        )
