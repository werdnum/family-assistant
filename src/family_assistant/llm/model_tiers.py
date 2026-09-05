"""Resolving named model tiers into LLM client configuration.

A tier is a model recipe -- primary provider/model, optional availability
fallback, and per-entry request parameters. This module turns one into the dict
``LLMClientFactory.create_client`` takes, and refuses the profile
configurations where a tier cannot mean what it says.

Per-entry ``llm_parameters`` overlay the top-level ``llm_parameters`` map rather
than replacing it. That map is keyed by model *substring* and resolved in
insertion order, so the same model at two reasoning efforts is inexpressible
there: whichever pattern matches first wins for every use of the model. An
overlay is therefore re-inserted under the entry's exact model id **after** every
global pattern, so it is the last thing applied for that entry and reaches no
other entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from family_assistant.llm.providers.google_genai_client import (
    is_interactions_agent_model,
)

if TYPE_CHECKING:
    from family_assistant.config_models import (
        ModelTierConfig,
        RetryModelConfig,
        ServiceProfile,
    )


def resolve_entry_model_parameters(
    entry: RetryModelConfig,
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    global_llm_parameters: dict[str, dict[str, Any]],
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
) -> dict[str, dict[str, Any]]:
    """The model-keyed parameter map one chain entry should be served with."""
    if not entry.llm_parameters:
        return global_llm_parameters
    if not entry.model:
        msg = "A chain entry with llm_parameters must name its model."
        raise ValueError(msg)
    params = dict(global_llm_parameters)
    existing = params.pop(entry.model, {})
    params[entry.model] = {**existing, **entry.llm_parameters}
    return params


def resolve_entry_client_config(
    entry: RetryModelConfig,
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    global_llm_parameters: dict[str, dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
) -> dict[str, Any]:
    """One chain entry as the factory's single-client configuration."""
    # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
    client_config: dict[str, Any] = {
        "model": entry.model,
        "model_parameters": resolve_entry_model_parameters(
            entry, global_llm_parameters
        ),
    }
    if entry.provider:
        client_config["provider"] = entry.provider
    return client_config


def resolve_tier_client_config(
    tier: ModelTierConfig,
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    global_llm_parameters: dict[str, dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
) -> dict[str, Any]:
    """A tier as the factory's client configuration.

    A single-entry chain produces the plain single-client shape rather than a
    retry wrapper around one model, so a one-model tier behaves exactly like a
    profile that names the model inline.
    """
    primary = resolve_entry_client_config(tier.chain[0], global_llm_parameters)
    if len(tier.chain) == 1:
        return primary
    return {
        "retry_config": {
            "primary": primary,
            "fallback": resolve_entry_client_config(
                tier.chain[1], global_llm_parameters
            ),
        }
    }


def validate_profile_model_tier(
    profile_conf: ServiceProfile,
    model_tiers: dict[str, ModelTierConfig],
) -> ModelTierConfig | None:
    """Resolve a profile's tier, refusing configurations it cannot honour.

    Returns the tier the profile runs on, or ``None`` when it names an inline
    model instead. Raises ``ValueError`` naming the profile otherwise: an
    unknown tier, a tier on a profile whose runtime is coupled to one provider
    or one API surface, or an eligibility list that does not describe reachable
    tiers.
    """
    profile_id = profile_conf.id
    processing_config = profile_conf.processing_config
    tier_name = processing_config.model_tier
    allowed = profile_conf.allowed_model_tiers

    if tier_name is None:
        if allowed is not None:
            msg = (
                f"Profile '{profile_id}' sets allowed_model_tiers without a "
                "model_tier. A profile that names an inline model runs on that "
                "model only; selection needs a default tier to fall back to."
            )
            raise ValueError(msg)
        return None

    tier = model_tiers.get(tier_name)
    if tier is None:
        known = ", ".join(sorted(model_tiers)) or "(none configured)"
        msg = (
            f"Profile '{profile_id}' names unknown model_tier '{tier_name}'. "
            f"Configured tiers: {known}."
        )
        raise ValueError(msg)

    _reject_tier_on_pinned_runtime(profile_conf, tier_name, tier)

    if allowed is not None:
        unknown = sorted(set(allowed) - set(model_tiers))
        if unknown:
            known = ", ".join(sorted(model_tiers)) or "(none configured)"
            msg = (
                f"Profile '{profile_id}' allows unknown model tier(s): "
                f"{', '.join(unknown)}. Configured tiers: {known}."
            )
            raise ValueError(msg)
        if tier_name not in allowed:
            msg = (
                f"Profile '{profile_id}' has model_tier '{tier_name}', which is "
                f"not in its allowed_model_tiers ({', '.join(allowed)}). The "
                "default tier must itself be selectable."
            )
            raise ValueError(msg)

    return tier


def _reject_tier_on_pinned_runtime(
    profile_conf: ServiceProfile,
    tier_name: str,
    tier: ModelTierConfig,
) -> None:
    """Refuse a tier on a profile whose runtime is not model-interchangeable."""
    profile_id = profile_conf.id
    processing_config = profile_conf.processing_config

    if profile_conf.remote_a2a is not None:
        msg = (
            f"Profile '{profile_id}' is a remote A2A profile and cannot use "
            f"model_tier '{tier_name}': the remote agent chooses its own model."
        )
        raise ValueError(msg)
    if processing_config.enable_computer_use:
        msg = (
            f"Profile '{profile_id}' has enable_computer_use=True and cannot "
            f"use model_tier '{tier_name}': computer use requires the single "
            "Google GenAI client."
        )
        raise ValueError(msg)
    if processing_config.antigravity_config is not None:
        msg = (
            f"Profile '{profile_id}' has antigravity_config and cannot use "
            f"model_tier '{tier_name}': the managed agent is reached through "
            "the Interactions API, not a chat client."
        )
        raise ValueError(msg)

    agent_models = [
        entry.model
        for entry in tier.chain
        if entry.model and is_interactions_agent_model(entry.model)
    ]
    if agent_models:
        msg = (
            f"Profile '{profile_id}' uses model_tier '{tier_name}', whose chain "
            f"names Interactions API agent model(s): {', '.join(agent_models)}. "
            "Those run server-side rather than as chat models, so they are "
            "configured inline on a profile, not as an interchangeable tier."
        )
        raise ValueError(msg)
