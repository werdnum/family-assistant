"""Tests for the Prometheus metrics exported from the telemetry chokepoints."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pytest
from opentelemetry import trace as otel_trace
from prometheus_client import REGISTRY
from pydantic import ValidationError

from family_assistant.config_models import AppConfig
from family_assistant.llm.call_context import (
    current_processing_profile,
    reset_model_selection,
    reset_processing_profile,
    set_model_selection,
    set_processing_profile,
)
from family_assistant.llm.model_selection import ResolvedModelSelection
from family_assistant.llm.providers.google_genai_client import GoogleGenAIClient
from family_assistant.llm.utils.call_telemetry import LLMCallTelemetry
from family_assistant.observability.exporter import start_metrics_exporter
from family_assistant.observability.metrics import (
    UNKNOWN_TOOL,
    TurnMetrics,
    instrumented_llm_request,
    normalized_token_buckets,
    record_tool_call,
)
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.tools import ToolNotFoundError
from family_assistant.tools.infrastructure import (
    LocalToolsProvider,
    TaintTrackingToolsProvider,
    ToolDescriptorProvider,
)
from family_assistant.tools.metadata import (
    ToolRegistration,
    ToolTag,
    make_local_tool_metadata,
)
from family_assistant.tools.on_demand import OnDemandToolsView
from family_assistant.tools.types import ToolExecutionContext
from family_assistant.tools.video_backends import (
    GeminiOmniVideoBackend,
    VideoGenerationError,
    VideoGenerationRequest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

    from family_assistant.llm.messages import MessageReasoningInfo
    from family_assistant.storage.database import Database
    from family_assistant.tools.types import ToolDefinition


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


def _returns[T](value: T) -> Callable[[], Awaitable[T]]:
    """A request callable that succeeds with *value*."""

    async def request() -> T:
        return value

    return request


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


def test_image_input_is_its_own_tier_carved_out_of_the_text_input() -> None:
    """GPT Image bills image input above text, so an aggregate cannot be costed."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 1000,
        "completion_tokens": 400,
        "total_tokens": 1400,
        "image_input_tokens": 900,
    }

    buckets = normalized_token_buckets(usage, "openai")

    assert buckets["input_image"] == 900
    assert buckets["input_uncached"] == 100
    assert buckets["input_uncached"] + buckets["input_image"] == 1000


def test_a_provider_that_reports_no_modality_split_emits_no_image_bucket() -> None:
    """Absent means "not reported", so a text-only price join stays correct."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 1000,
        "completion_tokens": 400,
        "total_tokens": 1400,
    }

    buckets = normalized_token_buckets(usage, "openai")

    assert "input_image" not in buckets
    assert buckets["input_uncached"] == 1000


def test_generated_image_tokens_are_carved_out_of_the_text_output() -> None:
    """Image output is priced well above text output on models that emit both."""
    usage: MessageReasoningInfo = {
        "prompt_tokens": 100,
        "completion_tokens": 1300,
        "total_tokens": 1400,
        "image_output_tokens": 1200,
    }

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["output_image"] == 1200
    assert buckets["output"] == 100
    assert buckets["output"] + buckets["output_image"] == 1300


def test_reasoning_and_image_output_are_both_removed_from_the_text_output() -> None:
    """The generated tiers stay disjoint when a provider reports both."""
    usage: MessageReasoningInfo = {
        "completion_tokens": 1000,
        "reasoning_tokens": 300,
        "image_output_tokens": 500,
    }

    buckets = normalized_token_buckets(usage, "openai")

    assert buckets["reasoning"] == 300
    assert buckets["output_image"] == 500
    assert buckets["output"] == 200


def test_a_cached_image_is_counted_once_not_in_two_buckets() -> None:
    """Cached image tokens are the overlap of the cache and the image split.

    Counted in both, the prompt tiers stop summing to the prompt and the cost
    join overstates the call -- which defeats the point of disjoint buckets.
    """
    usage: MessageReasoningInfo = {
        "prompt_tokens": 10_000,
        "cached_prompt_tokens": 6_000,
        "image_input_tokens": 7_000,
        "cached_image_tokens": 5_000,
    }

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["cache_read"] == 6_000
    assert buckets["input_image"] == 2_000
    assert buckets["input_uncached"] == 2_000
    # The prompt tiers reconstruct the prompt exactly, which is the invariant
    # every cost query depends on.
    assert (
        buckets["cache_read"] + buckets["input_image"] + buckets["input_uncached"]
        == 10_000
    )


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
        "tier": "none",
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
        "tier": "none",
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
        "tier": "none",
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


def test_the_resolved_tier_labels_the_call_and_lands_in_the_usage(
    profile: str,
) -> None:
    """Spend is attributed to the tier that incurred it, not just the profile."""
    selection = ResolvedModelSelection(tier="deep", requested="deep", source="user")
    token = set_model_selection(selection)
    try:
        telemetry = _telemetry(provider="openai", model="gpt-sol")
        usage = telemetry.finalize_usage({"prompt_tokens": 10})
        telemetry.finish_success(response=None)
    finally:
        reset_model_selection(token)

    assert usage.get("model_tier") == "deep"
    assert usage.get("model_tier_source") == "user"
    assert usage.get("model_tier_requested") == "deep"
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {
                "profile": profile,
                "tier": "deep",
                "provider": "openai",
                "model": "gpt-sol",
                "resolved_model": "gpt-sol",
                "operation": "chat",
                "outcome": "success",
                "error_type": "",
            },
        )
        == 1
    )


def test_an_unselected_tier_records_the_default_without_a_requested_one() -> None:
    """ "Nobody asked" must stay distinguishable from "asked for the default"."""
    selection = ResolvedModelSelection.unselected("standard")
    token = set_model_selection(selection)
    try:
        telemetry = _telemetry(provider="google", model="gemini-test")
        usage = telemetry.finalize_usage(None)
    finally:
        reset_model_selection(token)

    assert usage.get("model_tier") == "standard"
    assert usage.get("model_tier_source") == "default"
    assert "model_tier_requested" not in usage


def test_the_profile_is_captured_when_the_call_starts(profile: str) -> None:
    """A streamed call ends after its caller has left the profile's block."""
    telemetry = _telemetry(provider="google", model="gemini-test", streaming=True)

    reset_processing_profile(set_processing_profile("some_other_profile"))
    assert current_processing_profile() == profile
    assert telemetry.attribution.profile_id == profile


def test_an_abandoned_streamed_call_still_records_an_outcome(profile: str) -> None:
    """A client that disconnects mid-stream raises past every except Exception."""
    telemetry = _telemetry(provider="google", model="gemini-abandoned", streaming=True)
    telemetry.record_usage({"prompt_tokens": 700})

    telemetry.finish_abandoned()

    labels = {
        "profile": profile,
        "tier": "none",
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
        "tier": "none",
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


def test_an_agent_run_reports_its_token_modalities() -> None:
    """An Interactions run can be multimodal both ways -- images in, video out."""

    @dataclass
    class _Modality:
        modality: str
        tokens: int

    class _Usage:
        total_input_tokens = 5000
        total_output_tokens = 9000
        total_tokens = 14000
        input_tokens_by_modality = [_Modality("TEXT", 500), _Modality("IMAGE", 4500)]
        output_tokens_by_modality = [_Modality("IMAGE", 8000)]

    usage = GoogleGenAIClient._reasoning_info_from_interaction_usage(_Usage())
    assert usage is not None

    buckets = normalized_token_buckets(usage, "google")

    assert buckets["input_image"] == 4500
    assert buckets["input_uncached"] == 500
    assert buckets["output_image"] == 8000
    assert buckets["output"] == 1000


def test_a_committed_run_is_counted_even_if_its_usage_cannot_be_read() -> None:
    """The enrichment read may fail; the run still happened and was still billed.

    The caller has already committed the terminal transition, so no later poll
    reaches this hook again -- dropping the call here would lose the whole run
    rather than its token detail.
    """
    client = GoogleGenAIClient(api_key="test", model="deep-research-preview-04-2026")
    # Bound to a stand-in rather than a whole ProcessingService: the two
    # attributes below are all this method reads, and constructing the service
    # would pull in the entire processing stack for one call.
    service = SimpleNamespace(
        service_config=SimpleNamespace(id="p_unreadable"),
        _google_client=lambda: client,
    )

    InteractionsAgentProcessingService._record_run_metrics(
        cast("InteractionsAgentProcessingService", service), None, outcome="success"
    )

    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {
                "profile": "p_unreadable",
                "tier": "none",
                "provider": "google",
                "model": "models/deep-research-preview-04-2026",
                "resolved_model": "models/deep-research-preview-04-2026",
                "operation": "deep_research",
                "outcome": "success",
                "error_type": "",
            },
        )
        == 1
    )


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
        "tier": "none",
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


@pytest.mark.asyncio
async def test_video_generation_is_counted_like_any_other_provider_request(
    profile: str,
) -> None:
    """Video is billable and reaches a provider, so it lands in the same counters.

    Veo bills per second of video and reports no usage block, so the call
    counter is its only meter -- the same treatment the per-image models get.
    """
    result = await instrumented_llm_request(
        provider="google",
        model="veo-test",
        operation="video",
        request=_returns("a video"),
    )

    assert result == "a video"
    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {
                "profile": profile,
                "tier": "none",
                "provider": "google",
                "model": "veo-test",
                "resolved_model": "veo-test",
                "operation": "video",
                "outcome": "success",
                "error_type": "",
            },
        )
        == 1
    )


@pytest.mark.asyncio
async def test_a_request_rejected_before_the_provider_is_not_counted(
    profile: str,
) -> None:
    """Options the backend cannot honour never reach Google, so they are no call.

    Counting them would put traffic that does not exist into the error rate,
    which is what the companion deployment alerts on.
    """
    backend = GeminiOmniVideoBackend(api_key="unused", model="omni-test")
    request = VideoGenerationRequest(prompt="a cat", negative_prompt="dogs")

    with pytest.raises(VideoGenerationError):
        await backend.generate_video(request)

    for outcome in ("success", "error"):
        assert (
            _sample(
                "family_assistant_llm_calls_total",
                {
                    "profile": profile,
                    "tier": "none",
                    "provider": "google",
                    "model": "omni-test",
                    "resolved_model": "omni-test",
                    "operation": "video",
                    "outcome": outcome,
                    "error_type": "VideoGenerationError" if outcome == "error" else "",
                },
            )
            == 0
        )


@pytest.mark.asyncio
async def test_the_served_model_is_recorded_where_the_provider_reports_it(
    profile: str,
) -> None:
    """Otherwise resolved_model repeats the alias and hides provider routing."""

    class _Response:
        model = "gemini-3-pro-image-2026-01-01"

    await instrumented_llm_request(
        provider="google",
        model="gemini-3-pro-image",
        operation="image",
        request=_returns(_Response()),
        served_model=lambda response: response.model,
    )

    assert (
        _sample(
            "family_assistant_llm_calls_total",
            {
                "profile": profile,
                "tier": "none",
                "provider": "google",
                "model": "gemini-3-pro-image",
                "resolved_model": "gemini-3-pro-image-2026-01-01",
                "operation": "image",
                "outcome": "success",
                "error_type": "",
            },
        )
        == 1
    )


def test_the_metrics_port_may_not_be_the_application_port() -> None:
    """The exporter binds first, so a collision takes out the API, not metrics."""
    with pytest.raises(ValidationError, match="must differ from server_port"):
        AppConfig(metrics_enabled=True, metrics_port=8000, server_port=8000)

    # The same collision is harmless while the exporter is off, so it is allowed.
    assert AppConfig(metrics_port=8000, server_port=8000).metrics_port == 8000


def test_the_exporter_binds_loopback_unless_told_otherwise() -> None:
    """The endpoint has no auth of its own, so the bind address is the control."""
    assert AppConfig().metrics_bind_host == "127.0.0.1"
    assert AppConfig().metrics_enabled is False


# --- Tool accounting at the provider boundary ----------------------------


def _execution_context() -> ToolExecutionContext:
    """The minimum a tool execution needs; no database is touched here."""
    return ToolExecutionContext(
        interface_type="test",
        conversation_id="conversation",
        user_name="Test User",
        turn_id="turn",
        db_context=cast("Database", None),
        processing_service=None,
        clock=None,
        home_assistant_client=None,
        event_sources=None,
        attachment_registry=None,
        camera_backend=None,
        timezone=ZoneInfo("UTC"),
        credential_resolvers=None,
        api_backend=None,
    )


def _one_tool_provider(behaviour: Exception | str) -> LocalToolsProvider:
    """A real provider whose single tool does what the test needs.

    Real rather than a fake: the accounting sits on the provider that wraps
    this one, and the capability protocols it has to keep satisfying are the
    real ones.
    """

    async def some_tool(**_kwargs: object) -> str:
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    return LocalToolsProvider(
        registrations=[
            ToolRegistration(
                definition=cast(
                    "ToolDefinition",
                    {
                        "type": "function",
                        "function": {
                            "name": "some_tool",
                            "description": "A tool.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                ),
                implementation=some_tool,
                metadata=make_local_tool_metadata([ToolTag.READ_ONLY]),
            )
        ]
    )


@pytest.mark.asyncio
async def test_the_provider_counts_the_executions_that_reach_it() -> None:
    """Every entry path holds this provider, so counting here counts them all."""
    provider = TaintTrackingToolsProvider(
        _one_tool_provider("done"), profile="p_returned"
    )

    await provider.execute_tool("some_tool", {}, _execution_context())

    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_returned", "tool": "some_tool", "outcome": "returned"},
        )
        == 1
    )


@pytest.mark.asyncio
async def test_an_unknown_tool_is_counted_under_a_bounded_label() -> None:
    """An unresolvable name is caller-supplied text, so it must not be a label.

    Prometheus keeps every label tuple for the life of the process, so a typo
    loop against /api/tools/execute/{tool_name} would otherwise grow the series
    set without bound.
    """
    provider = TaintTrackingToolsProvider(
        _one_tool_provider("done"), profile="p_missing"
    )

    for name in ("no_such_tool", "another_typo", "../../etc/passwd"):
        with pytest.raises(ToolNotFoundError):
            await provider.execute_tool(name, {}, _execution_context())
        assert (
            _sample(
                "family_assistant_tool_calls_total",
                {"profile": "p_missing", "tool": name, "outcome": "not_found"},
            )
            == 0
        )

    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_missing", "tool": UNKNOWN_TOOL, "outcome": "not_found"},
        )
        == 3
    )


@pytest.mark.asyncio
async def test_a_tool_that_raises_is_still_counted_as_returned() -> None:
    """This is why the label is `returned` and not `success`.

    A tool that blows up does not propagate: the provider converts it into an
    error *result*, which comes back through the ordinary return path. Nothing
    at this level can tell that from an answer, so calling it success would
    make a tool error rate read as healthy while real failures flowed through.
    """
    provider = TaintTrackingToolsProvider(
        _one_tool_provider(RuntimeError("boom")), profile="p_raised"
    )

    await provider.execute_tool("some_tool", {}, _execution_context())

    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_raised", "tool": "some_tool", "outcome": "returned"},
        )
        == 1
    )
    assert (
        _sample(
            "family_assistant_tool_calls_total",
            {"profile": "p_raised", "tool": "some_tool", "outcome": "success"},
        )
        == 0
    )


def test_counting_tools_does_not_cost_the_provider_its_capabilities() -> None:
    """Regression: the accounting must not change what the provider *is*.

    Doing this in a wrapper broke startup. Providers are probed with
    ``isinstance`` against capability protocols, and a wrapper delegating via
    ``__getattr__`` satisfies ``hasattr`` but not ``isinstance`` -- so it
    passed every unit test and then failed to boot, because OnDemandToolsView
    refuses a provider that does not expose tool descriptors.
    """
    provider = TaintTrackingToolsProvider(
        _one_tool_provider("done"), profile="p_capability"
    )

    assert isinstance(provider, ToolDescriptorProvider)
    OnDemandToolsView(wrapped_provider=provider, on_demand_tool_names={"some_tool"})
