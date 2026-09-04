"""Prometheus metrics for LLM spend, tool execution and processing turns.

Spans already record what a single turn did. These metrics record what the
deployment has spent in aggregate -- per processing profile, per model, per
provider -- which a sampled, short-retention trace store cannot answer. See
:doc:`/docs/design/prometheus-metrics`.

Everything here is fed from a small number of chokepoints
(:class:`~family_assistant.llm.utils.call_telemetry.LLMCallTelemetry`, the tool
executor, the streaming loop) so that a provider or tool added later is counted
by construction rather than by remembering to count it.

**Token accounting is normalised here, once.** Providers disagree about what
their own prompt and completion counts include, so the exported buckets are
defined to be disjoint on every provider: ``input_uncached``, ``input_image``,
``cache_read`` and ``cache_write`` sum to the full prompt, ``output``,
``output_image`` and ``reasoning`` sum to everything generated, and
``tool_use`` is what the provider spent running its own server-side tools. A dashboard can therefore sum over
``kind`` without knowing which provider served the call.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Final

from prometheus_client import Counter, Gauge, Histogram

from family_assistant.llm.call_context import current_processing_profile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from family_assistant.llm.messages import MessageReasoningInfo

logger = logging.getLogger(__name__)

__all__ = [
    "LLM_CALLS",
    "LLM_CALL_DURATION",
    "LLM_TIME_TO_FIRST_OUTPUT",
    "LLM_TOKENS",
    "TOOL_CALLS",
    "TOOL_DURATION",
    "TURNS",
    "TURNS_IN_PROGRESS",
    "TURN_DURATION",
    "UNATTRIBUTED_PROFILE",
    "UNKNOWN_TOOL",
    "TurnMetrics",
    "instrumented_llm_request",
    "normalized_token_buckets",
    "record_llm_call",
    "record_tool_call",
]


UNKNOWN_TOOL: Final = "<unknown>"
"""Label for an execution whose tool name matched no registered tool.

The name in that case is free text from an LLM or an API caller rather than a
registry key, and Prometheus keeps every label tuple it has ever seen, so
recording it verbatim would let unbounded series accumulate. Angle brackets
because no real tool name can contain them.
"""

UNATTRIBUTED_PROFILE: Final = "none"
"""Label for an LLM call made outside any processing profile's turn.

Dropping those calls would hide real spend; a rising ``profile="none"`` series
is itself the signal that something is calling a model outside a turn.
"""

_LLM_LABELS: Final = ("profile", "provider", "model", "resolved_model", "operation")

# Provider call latency spans three orders of magnitude: a cached one-liner
# answers in well under a second, a high-effort reasoning turn with tools runs
# for minutes. Linear buckets would put every interesting call in the last one.
_CALL_DURATION_BUCKETS: Final = (
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    40.0,
    80.0,
    160.0,
    320.0,
    640.0,
)

# Time to first token is a user-perceived latency, so the resolution that
# matters is at the low end, where the difference between "instant" and
# "noticeable" lives.
_TTFO_BUCKETS: Final = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)

_TOOL_DURATION_BUCKETS: Final = (
    0.01,
    0.05,
    0.1,
    0.5,
    1.0,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)

# A turn is one or more provider calls plus every tool in between, so it is
# bounded by max_iterations rather than by any single call.
_TURN_DURATION_BUCKETS: Final = (
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
)


LLM_TOKENS = Counter(
    "family_assistant_llm_tokens",
    "Tokens consumed by LLM calls, in disjoint buckets (see kind).",
    (*_LLM_LABELS, "kind"),
)

LLM_CALLS = Counter(
    "family_assistant_llm_calls",
    "LLM provider calls, by how they ended.",
    (*_LLM_LABELS, "outcome", "error_type"),
)

LLM_CALL_DURATION = Histogram(
    "family_assistant_llm_call_duration_seconds",
    "Wall-clock duration of an LLM provider call.",
    (*_LLM_LABELS, "outcome"),
    buckets=_CALL_DURATION_BUCKETS,
)

LLM_TIME_TO_FIRST_OUTPUT = Histogram(
    "family_assistant_llm_time_to_first_output_seconds",
    "Time until the first streamed token. Streamed calls only.",
    _LLM_LABELS,
    buckets=_TTFO_BUCKETS,
)

TOOL_CALLS = Counter(
    "family_assistant_tool_calls",
    (
        "Tool executions, by how the execution ended -- returned, denied, "
        "not_found, cancelled or error. `returned` does not mean the tool did "
        "what was asked: a tool reports an expected failure by returning a "
        "result, which carries no status field to read."
    ),
    ("profile", "tool", "outcome"),
)

TOOL_DURATION = Histogram(
    "family_assistant_tool_duration_seconds",
    "Wall-clock duration of a tool execution.",
    ("profile", "tool"),
    buckets=_TOOL_DURATION_BUCKETS,
)

TURNS = Counter(
    "family_assistant_turns",
    "Processing turns, by how they ended.",
    ("profile", "outcome"),
)

TURN_DURATION = Histogram(
    "family_assistant_turn_duration_seconds",
    "Wall-clock duration of a processing turn, tools included.",
    ("profile", "outcome"),
    buckets=_TURN_DURATION_BUCKETS,
)

TURNS_IN_PROGRESS = Gauge(
    "family_assistant_turns_in_progress",
    "Processing turns currently running.",
    ("profile",),
)


# Anthropic reports uncached, cache-read and cache-write as three disjoint
# buckets, so its prompt_tokens is only the uncached remainder. Everyone else
# reports the whole prompt in prompt_tokens with the cache numbers as subsets
# of it. An unrecognised provider is assumed to follow the OpenAI convention,
# which is what an OpenAI-compatible endpoint will do.
_DISJOINT_PROMPT_PROVIDERS: Final = frozenset({"anthropic"})

# Google reports thoughts_token_count alongside a candidates_token_count that
# excludes it; OpenAI and Anthropic fold reasoning/thinking into their output
# count. Again, an unrecognised provider is assumed to follow OpenAI.
_REASONING_OUTSIDE_OUTPUT_PROVIDERS: Final = frozenset({"google"})


def normalized_token_buckets(
    reasoning_info: MessageReasoningInfo | None,
    provider: str,
) -> dict[str, int]:
    """Split a provider's usage report into disjoint, comparable buckets.

    Returns a mapping of ``kind`` to token count holding only the buckets the
    provider actually reported, so an absent key means "not reported" rather
    than zero -- the distinction that separates a provider that does not cache
    from one that cached nothing this call.

    The kinds are disjoint by construction: ``input_uncached + input_image +
    cache_read + cache_write`` is the full prompt, ``output + output_image +
    reasoning`` is everything generated, and ``tool_use`` is what the provider
    spent on its own server-side tools -- whichever provider served the call.
    """
    if not reasoning_info:
        return {}

    key = provider.strip().lower()
    buckets: dict[str, int] = {}

    cache_read = reasoning_info.get("cached_prompt_tokens")
    cache_write = reasoning_info.get("cache_write_tokens")
    if cache_read is not None:
        buckets["cache_read"] = max(0, cache_read)
    if cache_write is not None:
        buckets["cache_write"] = max(0, cache_write)

    # Image input is priced above text input wherever a provider reports the
    # split, so it is a tier of its own and comes out of the text bucket
    # rather than being averaged into it.
    image_input = reasoning_info.get("image_input_tokens")
    if image_input is not None:
        buckets["input_image"] = max(0, image_input)

    prompt = reasoning_info.get("prompt_tokens")
    if prompt is not None:
        uncached = prompt
        if key not in _DISJOINT_PROMPT_PROVIDERS:
            uncached -= (cache_read or 0) + (cache_write or 0)
        uncached -= image_input or 0
        buckets["input_uncached"] = max(0, uncached)

    reasoning = reasoning_info.get("reasoning_tokens")
    if reasoning is not None:
        buckets["reasoning"] = max(0, reasoning)

    # Already its own bucket wherever it is reported: what the provider spent
    # running a server-side tool, billed apart from prompt and candidates.
    tool_use = reasoning_info.get("tool_use_tokens")
    if tool_use is not None:
        buckets["tool_use"] = max(0, tool_use)

    # Generated image tokens are priced well above generated text on the models
    # that emit both, so they are their own tier, carved out of the text output
    # exactly as image input is carved out of the text input.
    image_output = reasoning_info.get("image_output_tokens")
    if image_output is not None:
        buckets["output_image"] = max(0, image_output)

    completion = reasoning_info.get("completion_tokens")
    if completion is not None:
        output = completion
        if key not in _REASONING_OUTSIDE_OUTPUT_PROVIDERS:
            output -= reasoning or 0
        output -= image_output or 0
        buckets["output"] = max(0, output)

    return buckets


def record_llm_call(
    *,
    profile: str,
    provider: str,
    model: str,
    resolved_model: str | None,
    operation: str,
    outcome: str,
    error_type: str | None,
    duration_seconds: float,
    time_to_first_output_seconds: float | None,
    reasoning_info: MessageReasoningInfo | None,
) -> None:
    """Count one provider call and the tokens it consumed.

    Never raises: a metrics backend that has somehow got into a bad state must
    not take a user's reply down with it.
    """
    labels = (
        profile or UNATTRIBUTED_PROFILE,
        provider,
        model,
        resolved_model or model,
        operation,
    )
    try:
        LLM_CALLS.labels(*labels, outcome, error_type or "").inc()
        LLM_CALL_DURATION.labels(*labels, outcome).observe(duration_seconds)
        if time_to_first_output_seconds is not None:
            LLM_TIME_TO_FIRST_OUTPUT.labels(*labels).observe(
                time_to_first_output_seconds
            )
        for kind, count in normalized_token_buckets(reasoning_info, provider).items():
            LLM_TOKENS.labels(*labels, kind).inc(count)
    except Exception:
        logger.debug("Failed to record LLM call metrics", exc_info=True)


def record_tool_call(
    *,
    profile: str,
    tool: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """Count one tool execution. Never raises.

    ``outcome`` is how the execution ended -- ``returned``, ``denied``,
    ``not_found``, ``cancelled`` or ``error`` -- not whether the tool
    succeeded at its job. Nothing here can tell the difference: a tool reports
    an expected failure by returning a ``ToolResult``, and that type has no
    status field. Calling the returning case ``success`` would let a tool error
    rate read as healthy while real failures flowed through it.
    """
    try:
        TOOL_CALLS.labels(profile, tool, outcome).inc()
        TOOL_DURATION.labels(profile, tool).observe(duration_seconds)
    except Exception:
        logger.debug("Failed to record tool call metrics", exc_info=True)


class TurnMetrics:
    """Counts one processing turn, from construction to :meth:`finish`.

    An object rather than a context manager because its only caller is an
    async generator, where a ``with`` block wrapped around a ``yield`` is the
    shape whose cleanup may not run.

    How many LLM calls a turn took is not a metric of its own: it is
    ``rate(family_assistant_llm_calls_total) /
    rate(family_assistant_turns_total)`` on the same profile, which the two
    counters already answer.
    """

    def __init__(self, profile: str) -> None:
        self._profile = profile
        self._started = time.monotonic()
        self._finished = False
        TURNS_IN_PROGRESS.labels(profile).inc()

    def finish(self, outcome: str) -> None:
        """Record the turn as having ended *outcome*. Idempotent, never raises.

        ``outcome`` is ``success``, ``error`` or ``cancelled``; a browser that
        navigated away is not a failure, and folding it into ``error`` would
        fire an error-rate alert on ordinary use.
        """
        if self._finished:
            return
        self._finished = True
        try:
            TURNS_IN_PROGRESS.labels(self._profile).dec()
            TURNS.labels(self._profile, outcome).inc()
            TURN_DURATION.labels(self._profile, outcome).observe(
                time.monotonic() - self._started
            )
        except Exception:
            logger.debug("Failed to record turn metrics", exc_info=True)


async def instrumented_llm_request[R](
    *,
    provider: str,
    model: str,
    operation: str,
    request: Callable[[], Awaitable[R]],
    usage: Callable[[R], MessageReasoningInfo | None] | None = None,
    served_model: Callable[[R], str | None] | None = None,
) -> R:
    """Count one provider request that is not a chat call.

    For the surfaces that reach a provider without going through
    :class:`~family_assistant.llm.utils.call_telemetry.LLMCallTelemetry` --
    image generation and embeddings -- where a span and a diagnostics record
    would say nothing the caller's own logging does not, but the spend is
    real and has to land in the same counters as everything else.

    ``usage`` is optional because not every such surface reports tokens, and
    not every one is billed in them: image models bill per image on some
    providers and per output token on others, so the call counter is the meter
    that holds everywhere and tokens are recorded only where reported.

    ``served_model`` reads back the model the provider says it actually used,
    where it says so. Without it the ``resolved_model`` label repeats the
    requested one, which makes provider-side routing and alias resolution
    invisible on exactly the calls where an alias is most common.
    """
    started = time.monotonic()
    profile = current_processing_profile() or UNATTRIBUTED_PROFILE

    def record(outcome: str, error_type: str | None, response: R | None) -> None:
        record_llm_call(
            profile=profile,
            provider=provider,
            model=model,
            resolved_model=(
                served_model(response)
                if served_model is not None and response is not None
                else None
            ),
            operation=operation,
            outcome=outcome,
            error_type=error_type,
            duration_seconds=time.monotonic() - started,
            time_to_first_output_seconds=None,
            reasoning_info=(
                usage(response) if usage is not None and response is not None else None
            ),
        )

    try:
        response = await request()
    except Exception as exc:
        record("error", type(exc).__name__, None)
        raise
    except BaseException:
        # Cancellation is not a provider failure, but the request still ran and
        # may still be billed, so it is counted rather than dropped.
        record("cancelled", None, None)
        raise
    record("success", None, response)
    return response
