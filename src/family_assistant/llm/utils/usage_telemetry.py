"""Span attributes for LLM token usage, including prompt-cache accounting."""

from opentelemetry.trace import Span

from family_assistant.llm.messages import MessageReasoningInfo

__all__ = ["set_usage_span_attributes"]


def set_usage_span_attributes(
    span: Span,
    reasoning_info: MessageReasoningInfo | None,
) -> None:
    """Record token usage from *reasoning_info* onto *span*.

    Cache attributes are only set when the provider reported them, so their
    absence on a span means "not reported" rather than "zero" -- a distinction
    that matters when the provider or model silently does not cache.
    """
    if not reasoning_info:
        return

    if "prompt_tokens" in reasoning_info:
        span.set_attribute("gen_ai.usage.input_tokens", reasoning_info["prompt_tokens"])
    if "completion_tokens" in reasoning_info:
        span.set_attribute(
            "gen_ai.usage.output_tokens", reasoning_info["completion_tokens"]
        )
    if "cached_prompt_tokens" in reasoning_info:
        span.set_attribute(
            "gen_ai.usage.cached_input_tokens",
            reasoning_info["cached_prompt_tokens"],
        )
    if "cache_write_tokens" in reasoning_info:
        span.set_attribute(
            "gen_ai.usage.cache_write_input_tokens",
            reasoning_info["cache_write_tokens"],
        )
