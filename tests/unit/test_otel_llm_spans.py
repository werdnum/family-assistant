"""Tests for OpenTelemetry spans in the LLM layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar, cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

import family_assistant.llm.retrying_client as rc_module
from family_assistant.llm import (
    JsonObject,
    LLMMessage,
    LLMOutput,
    LLMStreamEvent,
)
from family_assistant.llm.base import InvalidRequestError, ProviderConnectionError
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.retrying_client import RetryingLLMClient
from tests.mocks.mock_llm import (  # pylint: disable=no-name-in-module
    RuleBasedMockLLMClient,  # pylint can't resolve tests as a package
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence

    from family_assistant.tools.types import ToolDefinition

_T = TypeVar("_T", bound=BaseModel)


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    """Provides an InMemorySpanExporter with a local TracerProvider."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    original_tracer = rc_module.tracer
    rc_module.tracer = provider.get_tracer("test")

    yield exporter

    rc_module.tracer = original_tracer
    provider.shutdown()


class FailingMockClient(RuleBasedMockLLMClient):
    """Mock client that always raises ProviderConnectionError."""

    async def generate_response(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = "auto",
    ) -> LLMOutput:
        raise ProviderConnectionError(
            "Connection failed", provider="test", model="test-model"
        )

    async def generate_structured(
        self,
        messages: Sequence[LLMMessage],
        response_model: type[_T],
        max_retries: int = 2,
    ) -> _T:
        raise ProviderConnectionError(
            "Connection failed", provider="test", model="test-model"
        )

    async def generate_json(
        self,
        messages: Sequence[LLMMessage],
        max_retries: int = 2,
    ) -> JsonObject:
        raise ProviderConnectionError(
            "Connection failed", provider="test", model="test-model"
        )


class SimpleResponse(BaseModel):
    answer: str


class TestRetryingLLMClientSpans:
    @pytest.mark.asyncio
    async def test_generate_response_creates_span_with_attributes(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="Hello!"),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "llm.generate"
        assert span.attributes is not None
        assert span.attributes["gen_ai.request.model"] == "test-model"
        assert span.attributes["llm.has_fallback"] is False

    @pytest.mark.asyncio
    async def test_generate_response_records_attempt_events(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="Hello!"),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        attempt_events = [e for e in span.events if e.name == "llm.attempt"]
        assert len(attempt_events) >= 1
        assert attempt_events[0].attributes is not None
        assert attempt_events[0].attributes["attempt_number"] == 1

    @pytest.mark.asyncio
    async def test_generate_response_sets_usage_attributes(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(
                content="ok",
                reasoning_info={
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            ),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.attributes is not None
        assert span.attributes["gen_ai.usage.input_tokens"] == 10
        assert span.attributes["gen_ai.usage.output_tokens"] == 20

    @pytest.mark.asyncio
    async def test_generate_response_sets_error_on_failure(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        failing_client = FailingMockClient(
            rules=[], default_response=LLMOutput(content="unused")
        )
        client = RetryingLLMClient(
            primary_client=failing_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        with pytest.raises(ProviderConnectionError):
            await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == StatusCode.ERROR
        exception_events = [e for e in span.events if e.name == "exception"]
        assert len(exception_events) >= 1

    @pytest.mark.asyncio
    async def test_generate_response_records_fallback_attempt(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        failing_client = FailingMockClient(
            rules=[], default_response=LLMOutput(content="unused")
        )
        fallback_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="fallback response"),
        )
        client = RetryingLLMClient(
            primary_client=failing_client,
            primary_model="test-model",
            fallback_client=fallback_client,
            fallback_model="fallback-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        result = await client.generate_response(messages=messages)
        assert result.content == "fallback response"

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.attributes is not None
        assert span.attributes["llm.has_fallback"] is True

        attempt_events = [e for e in span.events if e.name == "llm.attempt"]
        # Attempt 1 (primary), Attempt 2 (retry primary), Attempt 3 (fallback)
        assert len(attempt_events) >= 3

        fallback_attempts = [
            e
            for e in attempt_events
            if e.attributes is not None and e.attributes.get("is_fallback") is True
        ]
        assert len(fallback_attempts) >= 1

    @pytest.mark.asyncio
    async def test_generate_response_stream_creates_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="streamed response"),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        async for _event in client.generate_response_stream(messages=messages):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "llm.generate_stream"
        assert span.attributes is not None
        assert span.attributes["gen_ai.request.model"] == "test-model"

    @pytest.mark.asyncio
    async def test_generate_structured_creates_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="unused"),
            structured_rules=[
                (lambda _args: True, SimpleResponse(answer="42")),
            ],
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        result = await client.generate_structured(
            messages=messages, response_model=SimpleResponse
        )
        assert result.answer == "42"

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "llm.generate_structured"
        assert span.attributes is not None
        assert span.attributes["gen_ai.request.model"] == "test-model"

    @pytest.mark.asyncio
    async def test_generate_json_creates_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[(lambda _args: True, LLMOutput(content='{"answer": "42"}'))],
            default_response=LLMOutput(content="unused"),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        result = await client.generate_json(messages=messages)
        assert result == {"answer": "42"}

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "llm.generate_json"
        assert span.attributes is not None
        assert span.attributes["gen_ai.request.model"] == "test-model"

    @pytest.mark.asyncio
    async def test_generate_response_reports_the_model_the_provider_served(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(
                content="ok", resolved_model="test-model-2026-08-01"
            ),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes is not None
        assert spans[0].attributes["gen_ai.request.model"] == "test-model"
        assert spans[0].attributes["gen_ai.response.model"] == "test-model-2026-08-01"
        assert spans[0].attributes["llm.attempts"] == 1
        assert spans[0].attributes["llm.fallback_used"] is False

    @pytest.mark.asyncio
    async def test_generate_response_counts_attempts_through_to_the_fallback(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        failing_client = FailingMockClient(
            rules=[], default_response=LLMOutput(content="unused")
        )
        fallback_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(content="fallback response"),
        )
        client = RetryingLLMClient(
            primary_client=failing_client,
            primary_model="test-model",
            fallback_client=fallback_client,
            fallback_model="fallback-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes is not None
        assert spans[0].attributes["llm.attempts"] == 3
        assert spans[0].attributes["llm.fallback_used"] is True
        assert spans[0].attributes["gen_ai.response.model"] == "fallback-model"

    @pytest.mark.asyncio
    async def test_stream_span_records_latency_and_resolved_model(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        mock_client = RuleBasedMockLLMClient(
            rules=[],
            default_response=LLMOutput(
                content="streamed response", resolved_model="test-model-2026-08-01"
            ),
        )
        client = RetryingLLMClient(
            primary_client=mock_client,
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        async for _event in client.generate_response_stream(messages=messages):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = spans[0].attributes
        assert attributes is not None
        assert attributes["gen_ai.response.model"] == "test-model-2026-08-01"
        assert attributes["llm.attempts"] == 1
        assert attributes["llm.fallback_used"] is False
        # The first chunk arrives well before the stream ends, which is the whole
        # point of measuring the two separately.
        first_output_ms = attributes["llm.time_to_first_output_ms"]
        duration_ms = attributes["llm.duration_ms"]
        assert isinstance(first_output_ms, float)
        assert isinstance(duration_ms, float)
        assert first_output_ms < duration_ms

    @pytest.mark.asyncio
    async def test_a_non_retriable_failure_does_not_invent_a_retry(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """A non-retriable primary error skips straight to the fallback."""

        class _NonRetriableClient(RuleBasedMockLLMClient):
            async def generate_response(
                self,
                messages: list[LLMMessage],
                tools: list[ToolDefinition] | None = None,
                tool_choice: str | None = "auto",
            ) -> LLMOutput:
                raise InvalidRequestError(
                    "bad request", provider="test", model="test-model"
                )

        client = RetryingLLMClient(
            primary_client=_NonRetriableClient(
                rules=[], default_response=LLMOutput(content="unused")
            ),
            primary_model="test-model",
            fallback_client=RuleBasedMockLLMClient(
                rules=[], default_response=LLMOutput(content="fallback response")
            ),
            fallback_model="fallback-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        await client.generate_response(messages=messages)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes is not None
        # Two provider calls were made, not three.
        assert spans[0].attributes["llm.attempts"] == 2
        assert spans[0].attributes["llm.fallback_used"] is True

    @pytest.mark.asyncio
    async def test_an_in_band_stream_error_marks_the_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """The consumer raises on the error event, so this is the only chance."""

        class _ErrorStreamClient(RuleBasedMockLLMClient):
            def generate_response_stream(
                self,
                messages: list[LLMMessage],
                tools: list[ToolDefinition] | None = None,
                tool_choice: str | None = "auto",
            ) -> AsyncIterator[LLMStreamEvent]:
                async def _stream() -> AsyncIterator[LLMStreamEvent]:
                    yield LLMStreamEvent(type="content", content="partial")
                    yield LLMStreamEvent(
                        type="error",
                        error="upstream refused",
                        metadata={"error_type": "rate_limit"},
                    )

                return _stream()

        client = RetryingLLMClient(
            primary_client=_ErrorStreamClient(
                rules=[], default_response=LLMOutput(content="unused")
            ),
            primary_model="test-model",
        )
        messages = [SystemMessage(content="system"), UserMessage(content="hello")]

        # Mirrors the processing loop, which stops consuming as soon as it sees
        # the error event, so the generator is closed rather than run to its
        # own completion and `finish` never gets to record anything.
        stream = cast(
            "AsyncGenerator[LLMStreamEvent]",
            client.generate_response_stream(messages=messages),
        )
        async for event in stream:
            if event.type == "error":
                break
        await stream.aclose()

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert spans[0].attributes is not None
        assert spans[0].attributes["llm.error.type"] == "rate_limit"
