"""Uniform telemetry for a single LLM provider call.

Every provider call produces two records of the same event: an OpenTelemetry
span and an :class:`LLMRequestRecord` in the in-memory diagnostics ring buffer.
Both used to be assembled by hand at every provider entry point, which is why
they had drifted --
the OpenAI client had no span at all, and every provider reported the *requested*
model as ``gen_ai.response.model`` rather than the model the API actually
served.

``LLMCallTelemetry`` is the single place both are assembled, so a provider that
reports a new detail reports it everywhere at once, and a provider that reports
nothing still gets timing, request shape, and the identifiers needed to join a
span to its ring-buffer record.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry.trace import Span, StatusCode

from family_assistant.llm.messages import (
    MessageReasoningInfo,
    message_to_json_dict,
)
from family_assistant.llm.request_buffer import LLMRequestRecord, get_request_buffer
from family_assistant.llm.utils.usage_telemetry import set_usage_span_attributes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from family_assistant.llm import LLMMessage, LLMOutput, LLMStreamEvent
    from family_assistant.tools.types import ToolDefinition

logger = logging.getLogger(__name__)

__all__ = ["LLMCallTelemetry"]


def _attachment_chars(messages: Sequence[LLMMessage]) -> int:
    """Approximate what tool-result attachments could add to the request.

    ``message_to_json_dict`` deliberately drops ``transient_attachments``, and
    providers only base64-encode them well after this point, so an image or PDF
    of several megabytes would otherwise be invisible -- which is exactly the
    case a slow first token most needs explained. Sized at the base64 expansion
    a provider applies to what it can read.

    This is an upper bound, and reported apart from the serialized payload for
    that reason: a provider substitutes a short text description for a type it
    cannot read, and which types those are differs by provider and model.
    Keeping the two separate means a substituted attachment cannot silently
    inflate the number that says how much was actually sent.
    """
    total = 0
    for message in messages:
        for attachment in getattr(message, "transient_attachments", None) or []:
            if attachment.content is not None:
                total += len(attachment.content) * 4 // 3
            elif attachment.file_path:
                try:
                    total += os.path.getsize(attachment.file_path) * 4 // 3
                except OSError:
                    # A path the provider will also fail to read; its size is
                    # not worth failing a request over.
                    continue
    return total


def _payload_chars(value: object) -> int:
    """Approximate the serialized size of a request payload, in characters.

    Walks the structure instead of serializing it: the payload can carry
    base64-encoded attachments of many megabytes, and their upload time is part
    of what this number exists to explain, so it must be counted without being
    copied.
    """
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(
            len(str(key)) + _payload_chars(item)
            for key, item in value.items()  # pyright: ignore[reportUnknownVariableType]
        )
    if isinstance(value, (list, tuple)):
        return sum(_payload_chars(item) for item in value)  # pyright: ignore[reportUnknownVariableType]
    if value is None:
        return 0
    return len(str(value))


class LLMCallTelemetry:
    """Span attributes and diagnostic record for one provider call.

    The span is owned by the caller -- this class never ends it, because the
    streaming paths keep a span open across an async generator's lifetime and
    end it in their own ``finally``.
    """

    def __init__(
        self,
        span: Span,
        *,
        provider: str,
        system: str,
        requested_model: str,
        messages: Sequence[LLMMessage],
        tools: list[ToolDefinition] | None,
        tool_choice: str | None,
        streaming: bool,
        operation: str = "chat",
    ) -> None:
        self.span = span
        self.provider = provider
        self.requested_model = requested_model
        self.streaming = streaming
        self.request_timestamp = datetime.now(UTC)
        self.request_id = "_".join(
            part
            for part in (
                provider,
                operation if operation != "chat" else None,
                "stream" if streaming else None,
                uuid.uuid4().hex[:16],
            )
            if part
        )
        self.resolved_model: str | None = None

        self._start = time.monotonic()
        self._first_output_at: float | None = None
        self._tools = tools
        self._tool_choice = tool_choice
        self._message_dicts = [message_to_json_dict(msg) for msg in messages]
        self._response_id: str | None = None
        self._finish_reason: str | None = None
        self._reasoning_info: MessageReasoningInfo | None = None
        self._content_chars = 0
        self._thinking_chars: int | None = None
        self._tool_call_count = 0

        # Tool schemas are part of the prompt the model has to process, and a
        # tool-heavy profile sends a lot of them, so they belong in the size
        # that explains a slow first token just as much as the messages do.
        self._payload_chars = _payload_chars(self._message_dicts) + _payload_chars(
            tools
        )
        self._attachment_chars = _attachment_chars(messages)
        self.span.set_attributes({
            "gen_ai.system": system,
            "gen_ai.operation.name": operation,
            "gen_ai.request.model": requested_model,
            "llm.request.id": self.request_id,
            "llm.request.streaming": streaming,
            "llm.request.message_count": len(self._message_dicts),
            "llm.request.payload_chars": self._payload_chars,
            "llm.request.attachment_chars": self._attachment_chars,
            "llm.request.tool_count": len(tools) if tools else 0,
            "llm.request.tool_choice": tool_choice or "",
        })

    @property
    def elapsed_ms(self) -> float:
        """Milliseconds since the call started."""
        return (time.monotonic() - self._start) * 1000

    @property
    def time_to_first_output_ms(self) -> float | None:
        """Milliseconds until the first streamed token, if the turn streamed."""
        if self._first_output_at is None:
            return None
        return (self._first_output_at - self._start) * 1000

    def mark_first_output(self) -> None:
        """Record the arrival of the first token, if not already recorded."""
        if self._first_output_at is None:
            self._first_output_at = time.monotonic()

    def observe_event(self, event: LLMStreamEvent) -> None:
        """Fold one streamed event into the call's counters.

        Call this immediately before yielding an event so that time-to-first-token
        measures what the caller actually saw rather than when the provider's
        SDK buffered it.
        """
        if event.type in {"content", "thinking", "tool_call"}:
            self.mark_first_output()
        if event.type == "content" and event.content:
            self._content_chars += len(event.content)
        elif event.type == "thinking" and event.content:
            # Counted apart from content: reasoning is never shown to the user,
            # so folding it in would make a long answer and a long deliberation
            # look alike -- while dropping it would hide the usual reason a
            # reasoning model is slow.
            self._thinking_chars = (self._thinking_chars or 0) + len(event.content)
        elif event.type == "tool_call":
            self._tool_call_count += 1

    def record_response_metadata(
        self,
        *,
        resolved_model: str | None = None,
        response_id: str | None = None,
        finish_reason: object = None,
    ) -> None:
        """Record what the provider said about the response it served.

        ``resolved_model`` is the model id the API reports back, which is not
        always the one that was requested: aliases such as ``-latest`` and
        provider-side routing resolve to a dated snapshot, and a latency change
        with no deploy behind it is usually that.
        """
        if resolved_model:
            self.resolved_model = resolved_model
        if response_id:
            self._response_id = response_id
        if finish_reason is not None:
            self._finish_reason = getattr(finish_reason, "name", None) or str(
                finish_reason
            )

    def record_usage(self, reasoning_info: MessageReasoningInfo | None) -> None:
        """Record token usage, both on the span and in the diagnostic record."""
        if not reasoning_info:
            return
        self._reasoning_info = reasoning_info
        set_usage_span_attributes(self.span, reasoning_info)

    def record_output(self, output: LLMOutput) -> None:
        """Record the shape of a non-streamed response.

        Deliberately leaves time-to-first-output unset: on a non-streamed call
        it would only ever equal the duration, and an attribute that is present
        but meaningless is worse than an absent one.
        """
        self._content_chars = len(output.content or "")
        self._tool_call_count = len(output.tool_calls or [])
        self.record_usage(output.reasoning_info)

    # ast-grep-ignore: no-dict-any - Serialized LLM response for the diagnostics buffer
    def finish_success(self, response: dict[str, Any] | None) -> None:
        """Close out a successful call: span attributes plus a buffer record."""
        self._set_completion_attributes()
        self._add_record(response=response, error=None)

    def finish_error(self, error: BaseException) -> None:
        """Close out a failed call: error status, span attributes, buffer record."""
        self.span.record_exception(error)
        self.finish_failure(str(error), error_type=type(error).__name__)

    def finish_failure(self, message: str, *, error_type: str) -> None:
        """Close out a call that failed without raising.

        Some providers report a failure in-band -- an error frame on an
        otherwise well-formed stream -- and the call returns normally. Those
        turns are the ones worth finding later, so they get the same error
        status and diagnostic record as a raised exception.
        """
        self._set_completion_attributes()
        self.span.set_status(StatusCode.ERROR, message)
        self.span.set_attribute("llm.error.type", error_type)
        self._add_record(response=None, error=message)

    def _set_completion_attributes(self) -> None:
        attributes: dict[str, str | int | float] = {
            "gen_ai.response.model": self.resolved_model or self.requested_model,
            "llm.response.model_resolved": self.resolved_model is not None,
            "llm.duration_ms": self.elapsed_ms,
            "llm.response.content_chars": self._content_chars,
            "llm.response.tool_call_count": self._tool_call_count,
        }
        if self._thinking_chars is not None:
            # Absent means "not measured" rather than "none produced": reasoning
            # text is only visible where it streams, so a zero here would make a
            # non-streamed reasoning turn look like it never deliberated.
            attributes["llm.response.thinking_chars"] = self._thinking_chars
        first_output_ms = self.time_to_first_output_ms
        if first_output_ms is not None:
            attributes["llm.time_to_first_output_ms"] = first_output_ms
        if self._response_id:
            attributes["gen_ai.response.id"] = self._response_id
        if self._finish_reason:
            attributes["gen_ai.response.finish_reason"] = self._finish_reason
        self.span.set_attributes(attributes)

    def _add_record(
        self,
        *,
        # ast-grep-ignore: no-dict-any - Serialized LLM response for the diagnostics buffer
        response: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        try:
            get_request_buffer().add(
                LLMRequestRecord(
                    timestamp=self.request_timestamp,
                    request_id=self.request_id,
                    model_id=self.requested_model,
                    messages=self._message_dicts,
                    tools=self._tools,
                    tool_choice=self._tool_choice,
                    response=response,
                    duration_ms=self.elapsed_ms,
                    error=error,
                    provider=self.provider,
                    resolved_model_id=self.resolved_model,
                    streaming=self.streaming,
                    time_to_first_output_ms=self.time_to_first_output_ms,
                    finish_reason=self._finish_reason,
                    response_id=self._response_id,
                    usage=self._reasoning_info,
                    request_payload_chars=self._payload_chars,
                    request_attachment_chars=self._attachment_chars,
                )
            )
        except Exception as record_err:
            logger.debug(f"Failed to record LLM request: {record_err}")
