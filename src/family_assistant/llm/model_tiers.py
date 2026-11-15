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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from family_assistant.llm.providers.google_genai_client import (
    is_interactions_agent_model,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from family_assistant.config_models import (
        AntigravityConfig,
        ModelTierConfig,
        ProcessingConfig,
        RetryModelConfig,
        ServiceProfile,
    )


def models_in_chain(
    entries: Sequence[RetryModelConfig],
    predicate: Callable[[str], bool],
) -> list[str]:
    """The model ids in a chain that satisfy ``predicate``.

    Chains reach here in two shapes -- a tier's ``chain`` list and a
    ``retry_config``'s primary/fallback pair -- and every model-class check
    over them asks the same question of each entry that names a model.
    """
    return [entry.model for entry in entries if entry.model and predicate(entry.model)]


def resolve_profile_llm_model(
    processing_config: ProcessingConfig,
    model_tier: ModelTierConfig | None,
    default_model: str,
) -> str:
    """The model a profile actually runs on.

    A tier's primary is that model, so service-class selection and the model
    validators see it rather than the global default. An Antigravity profile
    runs on its agent: the tier there names what the agent reasons with, not
    the model the request is sent to.
    """
    if processing_config.antigravity_config is not None:
        return processing_config.antigravity_config.agent
    if model_tier is None:
        return processing_config.llm_model or default_model
    primary_model = model_tier.chain[0].model
    if not primary_model:
        msg = "A model tier's primary chain entry must name its model."
        raise ValueError(msg)
    return primary_model


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
    if entry.flex:
        client_config["flex"] = True
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


def resolve_antigravity_tier_client_config(
    antigravity_config: AntigravityConfig,
    tier: ModelTierConfig,
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    global_llm_parameters: dict[str, dict[str, Any]],
    # ast-grep-ignore: no-dict-any - Factory config has varying provider keys.
) -> dict[str, Any]:
    """An Antigravity profile's client configuration at one tier.

    The request goes to the profile's agent, under its token cap and sandbox
    environment; the tier supplies only the reasoning model. That makes the
    client per profile rather than per tier, unlike a chat tier's.
    ``validate_profile_model_tier`` establishes beforehand that the tier is
    exactly one Google model.
    """
    return {
        "provider": "google",
        "model": antigravity_config.agent,
        "model_parameters": global_llm_parameters,
        "antigravity_model": tier.chain[0].model,
        "antigravity_max_total_tokens": antigravity_config.max_total_tokens,
        "antigravity_environment": antigravity_config.environment,
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
    tiers. Runtime compatibility is judged over every tier the profile may be
    run on, not just its default, because each of those gets a client built for
    it and can serve a turn.
    """
    profile_id = profile_conf.id
    processing_config = profile_conf.processing_config
    tier_name = processing_config.model_tier
    allowed = profile_conf.allowed_model_tiers
    auto = profile_conf.auto_model_tiers
    delegation = profile_conf.delegation_model_tiers

    # Once, before the branches: whether Auto has anything to choose from is
    # asked of the same two fields either way, and stating it on each exit is
    # how one of them comes to be missing it.
    _reject_auto_without_tiers(profile_conf)

    if tier_name is None:
        for field, value in (
            ("allowed_model_tiers", allowed),
            ("auto_model_tiers", auto),
            ("delegation_model_tiers", delegation),
        ):
            if value is not None:
                msg = (
                    f"Profile '{profile_id}' sets {field} without a model_tier. "
                    "A profile that names an inline model runs on that model "
                    "only; selection needs a default tier to fall back to."
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

    for field, value in (
        ("allowed_model_tiers", allowed),
        ("auto_model_tiers", auto),
        ("delegation_model_tiers", delegation),
    ):
        if value is None:
            continue
        unknown = sorted(set(value) - set(model_tiers))
        if unknown:
            known = ", ".join(sorted(model_tiers)) or "(none configured)"
            msg = (
                f"Profile '{profile_id}' lists unknown model tier(s) in {field}: "
                f"{', '.join(unknown)}. Configured tiers: {known}."
            )
            raise ValueError(msg)

    # Every tier the profile may be run on gets a client built for it, so a
    # runtime the tier system cannot serve is the same defect wherever it is
    # named -- checking only the default would let an alternate reach a
    # server-side agent runtime that silently drops the profile's tools and
    # history.
    for selectable in sorted(
        {*(allowed or ()), *(auto or ()), *(delegation or ())} - {tier_name}
    ):
        _reject_interactions_agent_tier(profile_id, selectable, model_tiers[selectable])

    if processing_config.claude_agent_config is not None:
        for reachable in sorted({
            tier_name,
            *(allowed or ()),
            *(auto or ()),
            *(delegation or ()),
        }):
            _reject_non_claude_agent_tier(profile_id, reachable, model_tiers[reachable])

    if processing_config.antigravity_config is not None:
        for reachable in sorted({
            tier_name,
            *(allowed or ()),
            *(auto or ()),
            *(delegation or ()),
        }):
            _reject_non_reasoning_model_tier(
                profile_id, reachable, model_tiers[reachable]
            )

    # A delegation preset promises "this model's answer or a visible failure",
    # which a fallback entry would quietly break: the answer would come from
    # the fallback while the run still names the preset.
    for preset in delegation or ():
        if len(model_tiers[preset].chain) != 1:
            msg = (
                f"Profile '{profile_id}' lists model tier '{preset}' in "
                "delegation_model_tiers, but its chain has a fallback. A "
                "delegation preset names exactly one model, so a delegation "
                "asking for it is never answered by another."
            )
            raise ValueError(msg)

    if allowed is not None and tier_name not in allowed:
        msg = (
            f"Profile '{profile_id}' has model_tier '{tier_name}', which is "
            f"not in its allowed_model_tiers ({', '.join(allowed)}). The "
            "default tier must itself be selectable."
        )
        raise ValueError(msg)

    if auto is not None:
        # A tier a model may pick on its own but a user may not explicitly ask
        # for describes no coherent policy: automatic selection is the weaker
        # authority of the two, so it cannot reach further. Absent
        # allowed_model_tiers means the profile runs on its default tier alone.
        selectable = allowed if allowed is not None else [tier_name]
        beyond_allowed = sorted(set(auto) - set(selectable))
        if beyond_allowed:
            msg = (
                f"Profile '{profile_id}' has auto_model_tiers "
                f"({', '.join(auto)}) reaching tier(s) outside what it may be "
                f"explicitly run on ({', '.join(selectable)}): "
                f"{', '.join(beyond_allowed)}. Automatic selection cannot reach "
                "a tier explicit selection may not."
            )
            raise ValueError(msg)

    return tier


def _reject_auto_without_tiers(profile_conf: ServiceProfile) -> None:
    """Refuse ``model_selection: auto`` on a profile Auto cannot choose for.

    Auto picks from ``auto_model_tiers``, so a profile that declares neither a
    default tier nor an automatic list has asked for a classifier with nothing
    to return. Left unchecked it would call a model on every turn and record
    an ``invalid`` outcome for each -- spend for a decision that cannot exist.
    """
    if profile_conf.processing_config.model_selection != "auto":
        return
    profile_id = profile_conf.id
    if profile_conf.processing_config.model_tier is None:
        msg = (
            f"Profile '{profile_id}' sets model_selection 'auto' without a "
            "model_tier. Auto resolves to a tier and falls back to the "
            "profile's own when routing fails, so it needs one."
        )
        raise ValueError(msg)
    if not profile_conf.auto_model_tiers:
        msg = (
            f"Profile '{profile_id}' sets model_selection 'auto' but its "
            "auto_model_tiers is empty. Auto chooses from that list; with "
            "nothing in it there is no decision to make."
        )
        raise ValueError(msg)


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
    _reject_interactions_agent_tier(profile_id, tier_name, tier)


def _reject_interactions_agent_tier(
    profile_id: str,
    tier_name: str,
    tier: ModelTierConfig,
) -> None:
    """Refuse a tier whose chain names a model that is not a chat model."""
    agent_models = models_in_chain(tier.chain, is_interactions_agent_model)
    if agent_models:
        msg = (
            f"Profile '{profile_id}' can run on model_tier '{tier_name}', whose "
            f"chain names Interactions API agent model(s): {', '.join(agent_models)}. "
            "Those run server-side rather than as chat models, so they are "
            "configured inline on a profile, not as an interchangeable tier."
        )
        raise ValueError(msg)


def _reject_non_claude_agent_tier(
    profile_id: str,
    tier_name: str,
    tier: ModelTierConfig,
) -> None:
    """Refuse a tier a Claude Agent SDK session cannot run as configured.

    A session takes one Claude model and an effort; it has no fallback, so a
    second entry would never run, and a non-Anthropic model or a Flex price
    class has no meaning there. Request parameters are checked when the model
    is resolved, against the merged global map.
    """
    entry = tier.chain[0]
    problem: str | None = None
    if len(tier.chain) != 1:
        problem = "its chain has a fallback, which the session cannot fall back to"
    elif entry.provider != "anthropic" or not entry.model:
        problem = (
            f"its model is on provider '{entry.provider}', and Claude Code runs "
            "only Anthropic models"
        )
    elif entry.flex:
        problem = "its entry sets flex, a price class Claude Code does not accept"
    if problem is not None:
        msg = (
            f"Profile '{profile_id}' runs on the Claude Agent SDK and can run "
            f"on model_tier '{tier_name}', but {problem}. A tier on this profile "
            "names the session's model, so it must be exactly one Anthropic model."
        )
        raise ValueError(msg)


ClaudeAgentEffort = Literal["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True)
class ClaudeAgentModel:
    """What one tier runs as in a Claude Agent SDK session."""

    model: str
    effort: ClaudeAgentEffort | None
    # Appended to the system prompt, as the chat clients do with an entry's
    # `custom_instructions`.
    custom_instructions: str | None


def resolve_claude_agent_model(
    tier_name: str,
    tier: ModelTierConfig,
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    global_llm_parameters: dict[str, dict[str, Any]],
) -> ClaudeAgentModel:
    """The model and effort a Claude Agent SDK session runs *tier* with.

    Parameters resolve the way the Anthropic client resolves them -- every
    global pattern the model id contains, in order, then the entry's own
    overlay -- so a tier means the same recipe on this profile as on a chat
    profile. Claude Code accepts only an effort and adaptive thinking, so any
    other parameter is refused rather than dropped: a run would otherwise be
    named for a recipe it did not execute.
    """
    entry = tier.chain[0]
    model = entry.model
    if not model:
        msg = f"Model tier '{tier_name}' must name its model."
        raise ValueError(msg)
    # ast-grep-ignore: no-dict-any - LLM params are provider-specific and genuinely arbitrary
    params: dict[str, Any] = {}
    for pattern, pattern_params in resolve_entry_model_parameters(
        entry, global_llm_parameters
    ).items():
        if pattern in model:
            params.update(pattern_params)

    effort: ClaudeAgentEffort | None = None
    custom_instructions: str | None = None
    for key, value in params.items():
        if key == "thinking" and value == {"type": "adaptive"}:
            continue
        if (
            key == "output_config"
            and isinstance(value, dict)
            and set(value) <= {"effort"}
        ):
            requested = value.get("effort")
            if requested is not None and requested not in get_args(ClaudeAgentEffort):
                msg = f"Model tier '{tier_name}' sets unknown effort {requested!r}."
                raise ValueError(msg)
            effort = cast("ClaudeAgentEffort | None", requested)
            continue
        if key == "custom_instructions" and isinstance(value, str):
            custom_instructions = value
            continue
        msg = (
            f"Model tier '{tier_name}' runs '{model}' with llm_parameters "
            f"'{key}': {value!r}, which a Claude Agent SDK session cannot apply. "
            "It accepts adaptive thinking, output_config.effort and "
            "custom_instructions."
        )
        raise ValueError(msg)
    return ClaudeAgentModel(
        model=model, effort=effort, custom_instructions=custom_instructions
    )


def _reject_non_reasoning_model_tier(
    profile_id: str,
    tier_name: str,
    tier: ModelTierConfig,
) -> None:
    """Refuse a tier an Antigravity profile cannot send as its reasoning model.

    The agent takes one Google model and nothing else: the API has no
    fallback, so a second entry would never run, and it accepts no request
    parameters, so an entry's ``llm_parameters`` would be dropped. Each would
    leave the run named for a recipe it did not execute.
    """
    entry = tier.chain[0]
    problem: str | None = None
    if len(tier.chain) != 1:
        problem = "its chain has a fallback, which the agent cannot fall back to"
    elif entry.provider != "google":
        problem = (
            f"its model is on provider '{entry.provider}', and the agent "
            "reasons only with Google models"
        )
    elif entry.llm_parameters:
        problem = "its entry sets llm_parameters, which the agent does not accept"
    elif entry.flex:
        problem = "its entry sets flex, a price class the agent does not accept"
    if problem is not None:
        msg = (
            f"Profile '{profile_id}' runs the Antigravity managed agent and can "
            f"run on model_tier '{tier_name}', but {problem}. A tier on this "
            "profile names the agent's reasoning model, so it must be exactly "
            "one Google model."
        )
        raise ValueError(msg)
