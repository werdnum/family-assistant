"""Provider-aware token accounting and USD list-price cost estimation.

Prompt-cache and reasoning-token buckets differ across providers (see
``MessageReasoningInfo`` in ``family_assistant.llm.messages``):

- **Google (Gemini)**: ``cached_prompt_tokens`` is a subset of ``prompt_tokens``;
  ``reasoning_tokens`` (thoughts) are reported *separately* from
  ``completion_tokens`` and billed at the output token rate.
- **OpenAI**: ``cached_prompt_tokens`` and ``cache_write_tokens`` are subsets of
  ``prompt_tokens``; ``reasoning_tokens`` are already folded into
  ``completion_tokens``.
- **Anthropic**: ``prompt_tokens`` (uncached), ``cached_prompt_tokens``, and
  ``cache_write_tokens`` are disjoint buckets; thinking tokens are already folded
  into ``completion_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "MODEL_PRICING_USD_PER_M",
    "ModelPrice",
    "TokenTotals",
    "accumulate_usage",
    "estimate_call_cost_usd",
    "lookup_model_price",
]


@dataclass(frozen=True)
class ModelPrice:
    """List API pricing in USD per 1 million tokens."""

    input_per_m: float
    cached_input_per_m: float
    output_per_m: float
    cache_write_per_m: float | None = None
    cache_write_1h_per_m: float | None = None


# Standard list API prices (USD / 1M tokens) as of Nov 2026. Used for eval cost
# comparisons and spend-cap guards, not billing reconciliation.
MODEL_PRICING_USD_PER_M: dict[str, ModelPrice] = {
    "gemini-3.8-flash": ModelPrice(
        input_per_m=0.75,
        cached_input_per_m=0.0375,
        output_per_m=3.75,
    ),
    "gemini-4-hydrogen": ModelPrice(
        input_per_m=1.50,
        cached_input_per_m=0.075,
        output_per_m=7.50,
    ),
    "gemini-4-neon": ModelPrice(
        input_per_m=3.00,
        cached_input_per_m=0.15,
        output_per_m=15.00,
    ),
    "gemini-4-argon": ModelPrice(
        input_per_m=2.00,
        cached_input_per_m=0.10,
        output_per_m=12.00,
    ),
    "gemini-4-carbon": ModelPrice(
        input_per_m=2.00,
        cached_input_per_m=0.10,
        output_per_m=12.00,
    ),
    "gpt-6.2-sol": ModelPrice(
        input_per_m=2.00,
        cached_input_per_m=0.10,
        output_per_m=10.00,
        cache_write_per_m=2.50,
    ),
    "gpt-6.2-astra": ModelPrice(
        input_per_m=5.00,
        cached_input_per_m=0.25,
        output_per_m=25.00,
        cache_write_per_m=6.25,
    ),
    "gpt-6.1-terra": ModelPrice(
        input_per_m=1.00,
        cached_input_per_m=0.05,
        output_per_m=5.00,
    ),
    "claude-haiku-5-5": ModelPrice(
        input_per_m=0.80,
        cached_input_per_m=0.08,
        output_per_m=4.00,
        cache_write_per_m=1.00,
        cache_write_1h_per_m=1.60,
    ),
    "claude-sonnet-5-5": ModelPrice(
        input_per_m=2.00,
        cached_input_per_m=0.10,
        output_per_m=10.00,
        cache_write_per_m=2.50,
        cache_write_1h_per_m=4.00,
    ),
    "claude-opus-5-5": ModelPrice(
        input_per_m=5.00,
        cached_input_per_m=0.50,
        output_per_m=25.00,
        cache_write_per_m=6.25,
        cache_write_1h_per_m=10.00,
    ),
    "claude-fable-5-2": ModelPrice(
        input_per_m=15.00,
        cached_input_per_m=1.50,
        output_per_m=75.00,
        cache_write_per_m=18.75,
        cache_write_1h_per_m=30.00,
    ),
}


@dataclass
class TokenTotals:
    """Normalized token counts summed across all LLM calls in a trial."""

    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    billed_output_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return (
            self.uncached_input_tokens
            + self.cached_input_tokens
            + self.cache_write_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        total = self.total_input_tokens
        return (self.cached_input_tokens / total) if total > 0 else 0.0


def lookup_model_price(model: str) -> ModelPrice | None:
    """Match an exact or prefix model name in :data:`MODEL_PRICING_USD_PER_M`."""
    if model in MODEL_PRICING_USD_PER_M:
        return MODEL_PRICING_USD_PER_M[model]
    # Provider snapshots sometimes append dated suffixes (e.g. gpt-6.2-sol-2026-11-01).
    for prefix in sorted(MODEL_PRICING_USD_PER_M, key=len, reverse=True):
        if model.startswith(prefix):
            return MODEL_PRICING_USD_PER_M[prefix]
    return None


def _as_int(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key, 0)
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    return 0


def accumulate_usage(
    totals: TokenTotals,
    provider: str,
    usage: Mapping[str, object] | None,
) -> tuple[int, int, int, int, int]:
    """Add one call's ``reasoning_info`` into ``totals`` and return billed buckets.

    Returns ``(uncached_in, cached_in, cache_write_5m, cache_write_1h, billed_out)``.
    """
    if not usage:
        return (0, 0, 0, 0, 0)

    prompt = _as_int(usage, "prompt_tokens")
    cached = _as_int(usage, "cached_prompt_tokens")
    cache_write_total = _as_int(usage, "cache_write_tokens")
    cache_write_1h = min(_as_int(usage, "cache_write_1h_tokens"), cache_write_total)
    cache_write_5m = max(cache_write_total - cache_write_1h, 0)
    completion = _as_int(usage, "completion_tokens")
    reasoning = _as_int(usage, "reasoning_tokens")

    norm_provider = provider.lower()
    if norm_provider == "anthropic":
        uncached_in = prompt
        billed_out = completion
    elif norm_provider == "google":
        uncached_in = max(prompt - cached, 0)
        cache_write_5m = 0
        cache_write_1h = 0
        billed_out = completion + reasoning
    else:
        # OpenAI (Responses API & Chat Completions): cached & cache_write are
        # subsets of prompt_tokens, and reasoning is folded into completion_tokens.
        uncached_in = max(prompt - cached - cache_write_total, 0)
        billed_out = completion

    totals.uncached_input_tokens += uncached_in
    totals.cached_input_tokens += cached
    totals.cache_write_tokens += cache_write_5m
    totals.cache_write_1h_tokens += cache_write_1h
    totals.output_tokens += completion
    totals.reasoning_tokens += reasoning
    totals.billed_output_tokens += billed_out
    return (uncached_in, cached, cache_write_5m, cache_write_1h, billed_out)


def estimate_call_cost_usd(
    provider: str,
    model: str,
    usage: Mapping[str, object] | None,
) -> float:
    """Estimate USD cost for a single provider response."""
    price = lookup_model_price(model)
    if price is None or not usage:
        return 0.0
    scratch = TokenTotals()
    uncached_in, cached_in, cw_5m, cw_1h, billed_out = accumulate_usage(
        scratch, provider, usage
    )
    write_5m_rate = (
        price.cache_write_per_m
        if price.cache_write_per_m is not None
        else price.input_per_m
    )
    write_1h_rate = (
        price.cache_write_1h_per_m
        if price.cache_write_1h_per_m is not None
        else write_5m_rate
    )
    return (
        uncached_in * price.input_per_m
        + cached_in * price.cached_input_per_m
        + cw_5m * write_5m_rate
        + cw_1h * write_1h_rate
        + billed_out * price.output_per_m
    ) / 1_000_000.0
