"""Tests for the shared per-call LLM telemetry helper.

These cover what a provider gets for free by going through
``LLMCallTelemetry``: span attributes describing the request and the response,
timing including time to first token, and a matching record in the diagnostics
ring buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from family_assistant.llm import (
    LLMOutput,
    LLMStreamEvent,
    ToolCallFunction,
    ToolCallItem,
)
from family_assistant.llm.messages import SystemMessage, ToolMessage, UserMessage
from family_assistant.llm.request_buffer import LLMRequestBuffer, get_request_buffer
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry
from family_assistant.tools.types import ToolAttachment, ToolDefinition

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace import ReadableSpan


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """An in-memory exporter wired to its own provider."""
    span_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    yield span_exporter
    provider.shutdown()


@pytest.fixture
def buffer() -> Iterator[LLMRequestBuffer]:
    """Empties the global diagnostics buffer around each test."""
    request_buffer = get_request_buffer()
    request_buffer.clear()
    yield request_buffer
    request_buffer.clear()


def _telemetry(
    exporter: InMemorySpanExporter,
    *,
    streaming: bool = False,
) -> LLMCallTelemetry:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    span = provider.get_tracer("test").start_span("llm.provider.generate")
    return LLMCallTelemetry(
        span,
        provider="testprovider",
        system="testprovider",
        requested_model="model-latest",
        messages=[SystemMessage(content="system"), UserMessage(content="hello")],
        tools=None,
        tool_choice="auto",
        streaming=streaming,
    )


def _finished(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


class TestRequestAttributes:
    def test_request_shape_is_recorded_before_the_call_returns(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        del buffer
        telemetry = _telemetry(exporter)
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        assert attributes["gen_ai.system"] == "testprovider"
        assert attributes["gen_ai.request.model"] == "model-latest"
        assert attributes["llm.request.message_count"] == 2
        assert attributes["llm.request.tool_count"] == 0
        assert attributes["llm.request.streaming"] is False
        payload_chars = attributes["llm.request.payload_chars"]
        assert isinstance(payload_chars, int)
        assert payload_chars > 0

    def test_span_and_buffer_record_share_a_request_id(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter)
        telemetry.finish_success(None)
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        records = buffer.get_recent()
        assert len(records) == 1
        assert attributes["llm.request.id"] == records[0].request_id


class TestResponseAttributes:
    def test_resolved_model_overrides_the_requested_one(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter)
        telemetry.record_response_metadata(
            resolved_model="model-2026-08-01",
            response_id="resp_123",
            finish_reason="stop",
        )
        telemetry.finish_success(None)
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        assert attributes["gen_ai.request.model"] == "model-latest"
        assert attributes["gen_ai.response.model"] == "model-2026-08-01"
        assert attributes["llm.response.model_resolved"] is True
        assert attributes["gen_ai.response.id"] == "resp_123"
        assert attributes["gen_ai.response.finish_reason"] == "stop"

        record = buffer.get_recent()[0]
        assert record.model_id == "model-latest"
        assert record.resolved_model_id == "model-2026-08-01"
        assert record.finish_reason == "stop"

    def test_a_provider_that_reports_no_model_falls_back_to_the_request(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        del buffer
        telemetry = _telemetry(exporter)
        telemetry.finish_success(None)
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        assert attributes["gen_ai.response.model"] == "model-latest"
        assert attributes["llm.response.model_resolved"] is False

    def test_output_shape_and_usage_are_recorded(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter)
        telemetry.record_output(
            LLMOutput(
                content="four chars ok",
                tool_calls=[
                    ToolCallItem(
                        id="call_1",
                        type="function",
                        function=ToolCallFunction(name="t", arguments="{}"),
                    )
                ],
                reasoning_info={"prompt_tokens": 11, "completion_tokens": 22},
            )
        )
        telemetry.finish_success(None)
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        assert attributes["llm.response.content_chars"] == len("four chars ok")
        assert attributes["llm.response.tool_call_count"] == 1
        assert attributes["gen_ai.usage.input_tokens"] == 11
        assert attributes["gen_ai.usage.output_tokens"] == 22
        assert buffer.get_recent()[0].usage == {
            "prompt_tokens": 11,
            "completion_tokens": 22,
        }


class TestStreaming:
    def test_time_to_first_output_is_recorded_from_the_first_content_event(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter, streaming=True)
        telemetry.observe_event(LLMStreamEvent(type="content", content="hi"))
        telemetry.observe_event(LLMStreamEvent(type="content", content=" there"))
        telemetry.finish_success({"streaming": True})
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        first_output_ms = attributes["llm.time_to_first_output_ms"]
        duration_ms = attributes["llm.duration_ms"]
        assert isinstance(first_output_ms, float)
        assert isinstance(duration_ms, float)
        assert first_output_ms <= duration_ms
        assert attributes["llm.response.content_chars"] == len("hi there")

        record = buffer.get_recent()[0]
        assert record.streaming is True
        assert record.time_to_first_output_ms is not None

    def test_a_stream_that_never_emits_reports_no_first_output(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter, streaming=True)
        telemetry.observe_event(LLMStreamEvent(type="done"))
        telemetry.finish_success({"streaming": True})
        telemetry.span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        assert "llm.time_to_first_output_ms" not in attributes
        assert buffer.get_recent()[0].time_to_first_output_ms is None


class TestPayloadSize:
    def test_tool_attachments_count_toward_the_payload(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        """Attachments are dropped from the serialized message but still upload."""
        del buffer
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        span = provider.get_tracer("test").start_span("llm.provider.generate")
        telemetry = LLMCallTelemetry(
            span,
            provider="testprovider",
            system="testprovider",
            requested_model="model-latest",
            messages=[
                ToolMessage(
                    content="see attachment",
                    tool_call_id="call_1",
                    name="some_tool",
                    _attachments=[
                        ToolAttachment(mime_type="image/png", content=b"x" * 30_000)
                    ],
                )
            ],
            tools=None,
            tool_choice="auto",
            streaming=False,
        )
        telemetry.finish_success(None)
        span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        payload_chars = attributes["llm.request.payload_chars"]
        assert isinstance(payload_chars, int)
        # Base64 expands the 30kB image to ~40kB; the text alone is a few dozen.
        assert payload_chars > 39_000

    def test_tool_schemas_count_toward_the_payload(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        """A tool-heavy profile sends schemas the model still has to process."""
        del buffer
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        span = provider.get_tracer("test").start_span("llm.provider.generate")
        tool: ToolDefinition = {
            "type": "function",
            "function": {
                "name": "a_tool",
                "description": "d" * 5_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        telemetry = LLMCallTelemetry(
            span,
            provider="testprovider",
            system="testprovider",
            requested_model="model-latest",
            messages=[UserMessage(content="hi")],
            tools=[tool],
            tool_choice="auto",
            streaming=False,
        )
        telemetry.finish_success(None)
        span.end()

        attributes = _finished(exporter).attributes
        assert attributes is not None
        payload_chars = attributes["llm.request.payload_chars"]
        assert isinstance(payload_chars, int)
        assert payload_chars > 5_000


class TestErrors:
    def test_failure_marks_the_span_and_records_the_error(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        telemetry = _telemetry(exporter)
        telemetry.finish_error(ValueError("boom"))
        telemetry.span.end()

        span = _finished(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["llm.error.type"] == "ValueError"
        assert [e.name for e in span.events] == ["exception"]

        record = buffer.get_recent()[0]
        assert record.error == "boom"
        assert record.response is None

    def test_an_in_band_failure_is_recorded_like_a_raised_one(
        self, exporter: InMemorySpanExporter, buffer: LLMRequestBuffer
    ) -> None:
        """A provider that reports failure on the stream still leaves a record."""
        telemetry = _telemetry(exporter, streaming=True)
        telemetry.finish_failure("upstream refused", error_type="response.failed")
        telemetry.span.end()

        span = _finished(exporter)
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["llm.error.type"] == "response.failed"

        record = buffer.get_recent()[0]
        assert record.error == "upstream refused"
