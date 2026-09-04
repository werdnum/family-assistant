"""Tests for the Prometheus metrics exported from the telemetry chokepoints."""

from __future__ import annotations

import urllib.request
from typing import TYPE_CHECKING

import pytest
from opentelemetry import trace as otel_trace
from prometheus_client import REGISTRY

from family_assistant.config_models import AppConfig
from family_assistant.llm.call_context import (
    current_processing_profile,
    reset_processing_profile,
    set_processing_profile,
)
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry
from family_assistant.observability.exporter import start_metrics_exporter
from family_assistant.observability.metrics import (
    TurnMetrics,
    normalized_token_buckets,
    record_tool_call,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from family_assistant.llm.messages import MessageReasoningInfo


def _sample(name: str, labels: Mapping[str, str]) -> float:
    """The current value of one sample, treating "never observed" as zero."""
    return REGISTRY.get_sample_value(name, dict(labels)) or 0.0


@pytest.fixture
def profile() -> Iterator[str]:
    """A profile label unique to the test, so counters cannot collide.

    The registry is process-global and shared with every other test in the
    worker; a per-test label is what keeps assertions about absolute values
    honest without resetting global state.
    """
    label = f"test_profile_{id(object())}"
    token = set_processing_profile(label)
    try:
        yield label
    finally:
        reset_processing_profile(token)


def _telemetry(
    *, provider: str, model: str, streaming: bool = False
) -> LLMCallTelemetry:
    span = otel_trace.get_tracer(__name__).start_span("test")
    return LLMCallTelemetry(
        span,
        provider=provider,
        system=provider,
        requested_model=model,
        messages=[],
        tools=None,
        tool_choice=None,
        streaming=streaming,
    )


# --- Token normalisation -------------------------------------------------


def test_anthropic_prompt_buckets_are_taken_as_already_disjoint() -> None:
    """Anthropic's prompt_tokens is the uncached remainder, not the whole prompt."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 100,
        "cached_prompt_tokens": 900,
        "cache_write_tokens": 50,
        "completion_tokens": 200,
    }

    buckets = normalized_token_buckets(usage, "anthropic")

    assert buckets["input_uncached"] == 100
    assert buckets["cache_read"] == 900
    assert buckets["cache_write"] == 50


def test_openai_prompt_buckets_are_split_out_of_the_reported_total() -> None:
    """OpenAI reports the whole prompt, with the cache numbers as subsets of it."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 1050,
        "cached_prompt_tokens": 900,
        "cache_write_tokens": 50,
    }

    buckets = normalized_token_buckets(usage, "openai")

    assert buckets["input_uncached"] == 100
    assert (
        buckets["input_uncached"] + buckets["cache_read"] + buckets["cache_write"]
        == usage["prompt_tokens"]
    )


def test_openai_reasoning_is_carved_out_of_the_output_it_is_reported_inside() -> None:
    usage: MessageReasoningInfo = {"completion_tokens": 500, "reasoning_tokens": 300}

    buckets = normalized_token_buckets(usage, "openai")

    assert buckets["output"] == 200
    assert buckets["reasoning"] == 300


def test_google_reasoning_is_kept_alongside_an_output_that_excludes_it() -> None:
    """Google's candidates count already excludes thoughts, so nothing is carved out."""
    usage: MessageReasoningInfo = {"completion_tokens": 200, "reasoning_tokens": 300}

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["output"] == 200
    assert buckets["reasoning"] == 300


def test_unreported_buckets_are_absent_rather_than_zero() -> None:
    """A provider that does not cache must be distinguishable from a cache miss."""
    usage: MessageReasoningInfo = {"prompt_tokens": 100, "completion_tokens": 20}

    buckets = normalized_token_buckets(usage, "google")

    assert "cache_read" not in buckets
    assert "cache_write" not in buckets
    assert "reasoning" not in buckets


def test_server_side_tool_tokens_are_their_own_bucket() -> None:
    """Gemini bills code execution and grounding apart from prompt and output."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "tool_use_tokens": 400,
    }

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["tool_use"] == 400
    assert buckets["input_uncached"] == 100
    assert buckets["output"] == 20


def test_no_usage_reported_yields_no_buckets() -> None:
    assert normalized_token_buckets(None, "openai") == {}


def test_inconsistent_provider_numbers_never_produce_a_negative_bucket() -> None:
    """A cache count larger than the prompt it is a subset of must not go negative."""
    usage: MessageReasoningInfo = {"prompt_tokens": 10, "cached_prompt_tokens": 50}

    buckets = normalized_token_buckets(usage, "openai")

    assert buckets["input_uncached"] == 0


# --- LLM call chokepoint -------------------------------------------------


def test_successful_call_counts_tokens_against_the_active_profile(
    profile: str,
) -> None:
    telemetry = _telemetry(provider="anthropic", model="claude-test")
    telemetry.record_response_metadata(resolved_model="claude-test-20260101")
    telemetry.record_usage({"prompt_tokens": 100, "completion_tokens": 20})

    telemetry.finish_success(response=None)

    labels = {
        "profile": profile,
        "provider": "anthropic",
        "model": "claude-test",
        "resolved_model": "claude-test-20260101",
        "operation": "chat",
    }
    assert (
        _sample(
            "family_assistant_llm_tokens_total", {**labels, "kind": "input_uncached"}
        )
        == 100
    )
    assert (
        _sample("family_assistant_llm_tokens_total", {**labels, "kind": "output"}) == 20
    )
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "success", "error_type": ""},
        )
        == 1
    )


def test_failed_call_still_counts_the_tokens_it_burned(profile: str) -> None:
    """A prompt that was sent and then rate-limited cost exactly what it cost."""
    telemetry = _telemetry(provider="openai", model="gpt-test")
    telemetry.record_usage({"prompt_tokens": 500})

    telemetry.finish_failure("rate limited", error_type="RateLimitError")

    labels = {
        "profile": profile,
        "provider": "openai",
        "model": "gpt-test",
        "resolved_model": "gpt-test",
        "operation": "chat",
    }
    assert (
        _sample(
            "family_assistant_llm_tokens_total", {**labels, "kind": "input_uncached"}
        )
        == 500
    )
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "error", "error_type": "RateLimitError"},
        )
        == 1
    )


def test_call_outside_a_turn_is_labelled_unattributed_rather_than_dropped() -> None:
    telemetry = _telemetry(provider="openai", model="gpt-oneshot")
    labels = {
        "profile": "none",
        "provider": "openai",
        "model": "gpt-oneshot",
        "resolved_model": "gpt-oneshot",
        "operation": "chat",
    }
    before = _sample(
        "family_assistant_llm_calls_total",
        {**labels, "outcome": "success", "error_type": ""},
    )

    telemetry.finish_success(response=None)

    after = _sample(
        "family_assistant_llm_calls_total",
        {**labels, "outcome": "success", "error_type": ""},
    )
    assert after - before == 1


def test_the_profile_is_captured_when_the_call_starts(profile: str) -> None:
    """A streamed call ends after its caller has left the profile's block."""
    telemetry = _telemetry(provider="google", model="gemini-test", streaming=True)

    reset_processing_profile(set_processing_profile("some_other_profile"))
    assert current_processing_profile() == profile
    assert telemetry.profile == profile


def test_an_abandoned_streamed_call_still_records_an_outcome(profile: str) -> None:
    """A client that disconnects mid-stream raises past every except Exception."""
    telemetry = _telemetry(provider="google", model="gemini-abandoned", streaming=True)
    telemetry.record_usage({"prompt_tokens": 700})

    telemetry.finish_abandoned()

    labels = {
        "profile": profile,
        "provider": "google",
        "model": "gemini-abandoned",
        "resolved_model": "gemini-abandoned",
        "operation": "chat",
    }
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "cancelled", "error_type": ""},
        )
        == 1
    )
    assert (
        _sample(
            "family_assistant_llm_tokens_total", {**labels, "kind": "input_uncached"}
        )
        == 700
    )


def test_abandoning_a_call_that_already_finished_counts_it_once(profile: str) -> None:
    """The provider's finally runs after its own terminal path."""
    telemetry = _telemetry(provider="openai", model="gpt-once")

    telemetry.finish_success(response=None)
    telemetry.finish_abandoned()

    labels = {
        "profile": profile,
        "provider": "openai",
        "model": "gpt-once",
        "resolved_model": "gpt-once",
        "operation": "chat",
    }
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "cancelled", "error_type": ""},
        )
        == 0
    )
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "success", "error_type": ""},
        )
        == 1
    )


# --- Tool and turn chokepoints -------------------------------------------


def test_tool_execution_is_counted_by_outcome() -> None:
    labels = {"profile": "p_tool", "tool": "get_note", "outcome": "denied"}
    before = _sample("family_assistant_tool_calls_total", labels)

    record_tool_call(
        profile="p_tool", tool="get_note", outcome="denied", duration_seconds=0.5
    )

    assert _sample("family_assistant_tool_calls_total", labels) - before == 1
    assert (
        _sample(
            "family_assistant_tool_duration_seconds_count",
            {"profile": "p_tool", "tool": "get_note"},
        )
        >= 1
    )


def test_a_finished_turn_leaves_the_in_progress_gauge_where_it_found_it() -> None:
    gauge_labels = {"profile": "p_turn"}
    before = _sample("family_assistant_turns_in_progress", gauge_labels)

    turn = TurnMetrics("p_turn")
    assert _sample("family_assistant_turns_in_progress", gauge_labels) - before == 1
    turn.finish("success")

    assert _sample("family_assistant_turns_in_progress", gauge_labels) == before
    assert (
        _sample(
            "family_assistant_turns_total",
            {"profile": "p_turn", "outcome": "success"},
        )
        == 1
    )


def test_finishing_a_turn_twice_counts_it_once() -> None:
    """The loop finishes on its own exit path and again in cleanup."""
    turn = TurnMetrics("p_turn_twice")
    turn.finish("success")
    turn.finish("cancelled")

    assert (
        _sample(
            "family_assistant_turns_total",
            {"profile": "p_turn_twice", "outcome": "cancelled"},
        )
        == 0
    )


# --- Exposition ----------------------------------------------------------


def test_the_exporter_serves_the_metrics_it_has_collected() -> None:
    """The whole point of the module is what a scrape gets back."""
    record_tool_call(
        profile="p_export", tool="get_note", outcome="returned", duration_seconds=0.1
    )
    server = start_metrics_exporter(0, addr="127.0.0.1")
    assert server is not None

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/metrics", timeout=10
        ) as response:
            body = response.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert 'family_assistant_tool_calls_total{outcome="returned"' in body
    assert 'profile="p_export"' in body


# --- Managed-agent runs --------------------------------------------------


class _FakeInteractionUsage:
    total_input_tokens = 12000
    total_output_tokens = 3000
    total_cached_tokens = 9000
    total_thought_tokens = 800
    total_tool_use_tokens = 450
    total_tokens = 15800


def test_an_agent_run_reports_the_tokens_it_spent() -> None:
    """A managed agent's spend is on the interaction, not in a chat response."""
    usage = GoogleGenAIClient._reasoning_info_from_interaction_usage(
        _FakeInteractionUsage()
    )
    assert usage is not None

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["cache_read"] == 9000
    assert buckets["input_uncached"] == 3000
    assert buckets["output"] == 3000
    assert buckets["reasoning"] == 800
    assert buckets["tool_use"] == 450


def test_an_agent_run_with_no_usage_reports_none() -> None:
    assert GoogleGenAIClient._reasoning_info_from_interaction_usage(None) is None


def test_a_returning_tool_is_not_recorded_as_a_success() -> None:
    """A tool reports an expected failure by returning; nothing can tell.

    `ToolResult` carries no status field, so calling the returning case
    "success" would let a tool error rate read as healthy while real failures
    flowed through it.
    """
    record_tool_call(
        profile="p_returned",
        tool="generate_image",
        outcome="returned",
        duration_seconds=0.1,
    )

    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_returned", "tool": "generate_image", "outcome": "success"},
        )
        == 0
    )
    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_returned", "tool": "generate_image", "outcome": "returned"},
        )
        == 1
    )


def test_a_cancelled_structured_call_still_records_an_outcome(profile: str) -> None:
    """Cancellation during tool-call review passes every `except Exception`."""
    telemetry = _telemetry(provider="anthropic", model="claude-structured")
    telemetry.record_usage({"prompt_tokens": 300})

    telemetry.finish_abandoned()

    labels = {
        "profile": profile,
        "provider": "anthropic",
        "model": "claude-structured",
        "resolved_model": "claude-structured",
        "operation": "chat",
    }
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {**labels, "outcome": "cancelled", "error_type": ""},
        )
        == 1
    )


def test_the_exporter_binds_loopback_unless_told_otherwise() -> None:
    """The endpoint has no auth of its own, so the bind address is the control."""
    assert AppConfig().metrics_bind_host == "127.0.0.1"
    assert AppConfig().metrics_enabled is False
