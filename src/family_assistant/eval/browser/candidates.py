"""Candidate model configurations for browser task evals.

Every candidate builds a single-entry, no-fallback ``LLMInterface`` from the
shipped ``defaults.yaml`` tiers (optionally overriding ``llm_parameters`` such
as ``thinking_config`` or ``reasoning_effort``) so every trial's result belongs
unambiguously to that model and effort level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from family_assistant.llm.factory import LLMClientFactory
from family_assistant.llm.model_tiers import resolve_entry_client_config

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig
    from family_assistant.llm import LLMInterface

__all__ = [
    "BUILTIN_CANDIDATES",
    "BrowserEvalCandidate",
    "BrowserMode",
    "build_candidate_client",
    "resolve_candidates",
]

BrowserMode = Literal["dom", "visual"]


@dataclass(frozen=True)
class BrowserEvalCandidate:
    """One model + effort + profile mode recipe evaluated on browser tasks."""

    name: str
    tier_id: str
    entry_index: int = 0
    mode: BrowserMode = "dom"
    # ast-grep-ignore: no-dict-any - provider-specific llm_parameters override map
    llm_parameters_override: dict[str, Any] = field(default_factory=dict)
    description: str = ""


# Built-in candidates spanning the current browser tier, its fallback, and the
# Gemini 4 / 3.8 challengers.
BUILTIN_CANDIDATES: dict[str, BrowserEvalCandidate] = {
    "flash-3.8": BrowserEvalCandidate(
        name="flash-3.8",
        tier_id="gemini_3_8_flash",
        description="Gemini 3.8 Flash (default thinking; cheapest floor, current standard & visual model)",
    ),
    "hydrogen": BrowserEvalCandidate(
        name="hydrogen",
        tier_id="gemini_4_hydrogen",
        description="Gemini 4 Hydrogen (default high thinking; shipped gemini_4_hydrogen preset)",
    ),
    "hydrogen-low": BrowserEvalCandidate(
        name="hydrogen-low",
        tier_id="gemini_4_hydrogen",
        llm_parameters_override={"thinking_config": {"thinking_level": "low"}},
        description="Gemini 4 Hydrogen (low thinking effort)",
    ),
    "hydrogen-medium": BrowserEvalCandidate(
        name="hydrogen-medium",
        tier_id="gemini_4_hydrogen",
        llm_parameters_override={"thinking_config": {"thinking_level": "medium"}},
        description="Gemini 4 Hydrogen (medium thinking effort)",
    ),
    "neon": BrowserEvalCandidate(
        name="neon",
        tier_id="gemini_4_neon",
        description="Gemini 4 Neon (default high thinking; shipped gemini_4_neon preset)",
    ),
    "neon-low": BrowserEvalCandidate(
        name="neon-low",
        tier_id="gemini_4_neon",
        llm_parameters_override={"thinking_config": {"thinking_level": "low"}},
        description="Gemini 4 Neon (low thinking effort)",
    ),
    "neon-medium": BrowserEvalCandidate(
        name="neon-medium",
        tier_id="gemini_4_neon",
        llm_parameters_override={"thinking_config": {"thinking_level": "medium"}},
        description="Gemini 4 Neon (medium thinking effort)",
    ),
    "gpt-6.2-sol": BrowserEvalCandidate(
        name="gpt-6.2-sol",
        tier_id="browser",
        entry_index=0,
        description="GPT-6.2 Sol (medium reasoning effort; current shipped browser tier primary)",
    ),
    "sonnet-5.5": BrowserEvalCandidate(
        name="sonnet-5.5",
        tier_id="browser",
        entry_index=1,
        description="Claude Sonnet 5.5 (adaptive thinking medium; current shipped browser tier fallback)",
    ),
    "visual-flash-3.8": BrowserEvalCandidate(
        name="visual-flash-3.8",
        tier_id="gemini_3_8_flash",
        mode="visual",
        description="Shipped browser_visual_profile: Gemini 3.8 Flash with native Computer Use",
    ),
    "visual-hydrogen": BrowserEvalCandidate(
        name="visual-hydrogen",
        tier_id="gemini_4_hydrogen",
        mode="visual",
        description="Visual profile on Gemini 4 Hydrogen (default thinking) with native Computer Use",
    ),
    "visual-hydrogen-medium": BrowserEvalCandidate(
        name="visual-hydrogen-medium",
        tier_id="gemini_4_hydrogen",
        mode="visual",
        llm_parameters_override={"thinking_config": {"thinking_level": "medium"}},
        description="Visual profile on Gemini 4 Hydrogen (medium thinking) with native Computer Use",
    ),
    "visual-neon-medium": BrowserEvalCandidate(
        name="visual-neon-medium",
        tier_id="gemini_4_neon",
        mode="visual",
        llm_parameters_override={"thinking_config": {"thinking_level": "medium"}},
        description="Visual profile on Gemini 4 Neon (medium thinking) with native Computer Use",
    ),
}


def resolve_candidates(names: list[str]) -> list[BrowserEvalCandidate]:
    """Resolve candidate names from :data:`BUILTIN_CANDIDATES`."""
    resolved: list[BrowserEvalCandidate] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        if name not in BUILTIN_CANDIDATES:
            valid = ", ".join(sorted(BUILTIN_CANDIDATES))
            raise ValueError(f"Unknown browser eval candidate {name!r}. Valid: {valid}")
        resolved.append(BUILTIN_CANDIDATES[name])
        seen.add(name)
    return resolved


def build_candidate_client(
    config: AppConfig,
    candidate: BrowserEvalCandidate,
    # ast-grep-ignore: no-dict-any - provider-specific model_parameters dict
) -> tuple[LLMInterface, str, str, dict[str, Any]]:
    """Build a single-model client for ``candidate`` with zero fallback.

    Returns ``(client, provider, model, effective_llm_parameters)``.
    """
    tier = config.model_tiers.get(candidate.tier_id)
    if tier is None:
        raise KeyError(
            f"Candidate {candidate.name!r} references unknown model tier {candidate.tier_id!r}"
        )
    if candidate.entry_index < 0 or candidate.entry_index >= len(tier.chain):
        raise IndexError(
            f"Candidate {candidate.name!r} entry_index={candidate.entry_index} "
            f"out of range for tier {candidate.tier_id!r} (len={len(tier.chain)})"
        )
    entry = tier.chain[candidate.entry_index]
    if candidate.llm_parameters_override:
        merged_entry_params = dict(entry.llm_parameters or {})
        merged_entry_params.update(candidate.llm_parameters_override)
        entry = entry.model_copy(update={"llm_parameters": merged_entry_params})

    client_config = resolve_entry_client_config(entry, config.llm_parameters)
    if candidate.mode == "visual":
        client_config["enable_computer_use"] = True
    provider = str(client_config["provider"])
    model = str(client_config["model"])
    raw_model_params = client_config.get("model_parameters") or {}
    # ast-grep-ignore: no-dict-any - provider-specific resolved parameter map
    effective_params: dict[str, Any] = {}
    if isinstance(raw_model_params, dict):
        for pattern, params in raw_model_params.items():
            if isinstance(params, dict) and model.startswith(str(pattern)):
                effective_params.update(params)
    client = LLMClientFactory.create_client(client_config)
    return client, provider, model, effective_params
