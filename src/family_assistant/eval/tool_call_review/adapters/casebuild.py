"""Shared helpers for constructing browser visibility-ablation cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.eval.tool_call_review.adapters.base import (
    AdaptedCase,
    AdaptedLineage,
)
from family_assistant.eval.tool_call_review.schema import (
    HIDDEN_ENVIRONMENT_MARKER,
    EvalCase,
    browser_action_discriminator,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "FULL_VERDICT_CONSTRAINTS",
    "build_browser_ablation_cases",
    "build_natural_benign_case",
]

# Adapted cases state the full verdict space with a fail-closed ``confirm``
# fallback; the runner never invents a verdict space for a corpus case.
FULL_VERDICT_CONSTRAINTS = {
    "available_verdicts": ["allow", "confirm", "deny"],
    "fallback_verdict": "confirm",
}


def _browser_case(
    *,
    case_id: str,
    source: str,
    label: str,
    attack_class: str | None,
    source_group: str,
    matched_group: str | None,
    control_kind: str,
    visibility: str,
    objective: str,
    damage_envelope: str,
    proposed_action: Mapping[str, object],
    environment: str,
    recent_actions: Sequence[Mapping[str, object]],
    mitigation_guidance: str,
    policy_contexts: Sequence[Mapping[str, object]],
    lineage: AdaptedLineage,
    obfuscation: str | None = None,
    placement: str | None = None,
    language: str | None = None,
) -> AdaptedCase:
    """Build one schema-validated browser case."""
    case = EvalCase.model_validate({
        "id": case_id,
        "boundary": "browser",
        "label": label,
        "attack_class": attack_class,
        "source": source,
        "source_group": source_group,
        "matched_group": matched_group,
        "visibility": visibility,
        "control_kind": control_kind,
        "obfuscation": obfuscation,
        "placement": placement,
        "language": language,
        "constraints": FULL_VERDICT_CONSTRAINTS,
        "payload": {
            "objective": objective,
            "damage_envelope": damage_envelope,
            "proposed_action": dict(proposed_action),
            "environment": environment,
            "recent_actions": [dict(action) for action in recent_actions],
            "mitigation_guidance": mitigation_guidance,
            "policy_contexts": [dict(context) for context in policy_contexts],
        },
    })
    return AdaptedCase(case=case, lineage=lineage)


def build_browser_ablation_cases(
    *,
    case_id_prefix: str,
    source: str,
    source_group: str,
    matched_group: str,
    attack_class: str,
    attack_objective: str,
    benign_objective: str,
    damage_envelope: str,
    attack_action: Mapping[str, object],
    benign_action: Mapping[str, object],
    attack_environment: str,
    benign_environment: str,
    attack_lineage: AdaptedLineage,
    benign_lineage: AdaptedLineage,
    obfuscation: str | None = None,
    placement: str | None = None,
    language: str | None = None,
    recent_actions: Sequence[Mapping[str, object]] = (),
    mitigation_guidance: str = "",
    policy_contexts: Sequence[Mapping[str, object]] = (),
) -> list[AdaptedCase]:
    """Build the exact attack/benign by hidden/full four-case matrix.

    The helper enforces the visibility contract at construction time: all
    fields except ``environment`` are copied identically within each control
    pair, and the two controls expose the same proposed-action kind.
    """
    if not attack_environment.strip() or not benign_environment.strip():
        raise ValueError("browser ablation environments must be non-empty")
    attack_discriminator = browser_action_discriminator(attack_action)
    benign_discriminator = browser_action_discriminator(benign_action)
    if attack_discriminator != benign_discriminator:
        raise ValueError(
            "browser ablation attack and benign actions must have the same "
            "action discriminator"
        )
    if set(attack_action) != set(benign_action):
        raise ValueError(
            "browser ablation attack and benign actions must have the same key shape"
        )
    if attack_environment == HIDDEN_ENVIRONMENT_MARKER:
        raise ValueError("attack full environment cannot use the hidden marker")
    if benign_environment == HIDDEN_ENVIRONMENT_MARKER:
        raise ValueError("benign full environment cannot use the hidden marker")

    common = {
        "source": source,
        "source_group": source_group,
        "matched_group": matched_group,
        "attack_class": attack_class,
        "damage_envelope": damage_envelope,
        "recent_actions": recent_actions,
        "mitigation_guidance": mitigation_guidance,
        "policy_contexts": policy_contexts,
        "obfuscation": obfuscation,
        "placement": placement,
        "language": language,
    }
    cases: list[AdaptedCase] = []
    for control_kind, label, objective, action, environment, lineage in (
        (
            "attack",
            "attack",
            attack_objective,
            attack_action,
            attack_environment,
            attack_lineage,
        ),
        (
            "benign_twin",
            "benign",
            benign_objective,
            benign_action,
            benign_environment,
            benign_lineage,
        ),
    ):
        for visibility, visible_environment in (
            ("hidden", HIDDEN_ENVIRONMENT_MARKER),
            ("full", environment),
        ):
            cases.append(
                _browser_case(
                    case_id=f"{case_id_prefix}-{control_kind}-{visibility}",
                    label=label,
                    control_kind=control_kind,
                    visibility=visibility,
                    objective=objective,
                    proposed_action=action,
                    environment=visible_environment,
                    lineage=lineage,
                    **common,
                )
            )
    return cases


def build_natural_benign_case(
    *,
    case_id: str,
    source: str,
    source_group: str,
    attack_class: str | None,
    objective: str,
    damage_envelope: str,
    proposed_action: Mapping[str, object],
    environment: str,
    lineage: AdaptedLineage,
    obfuscation: str | None = None,
    placement: str | None = None,
    language: str | None = None,
    recent_actions: Sequence[Mapping[str, object]] = (),
    mitigation_guidance: str = "",
    policy_contexts: Sequence[Mapping[str, object]] = (),
) -> AdaptedCase:
    """Build one full-only natural-benign browser control."""
    if not environment.strip() or environment == HIDDEN_ENVIRONMENT_MARKER:
        raise ValueError("natural-benign environment must be visible content")
    return _browser_case(
        case_id=case_id,
        source=source,
        label="benign",
        attack_class=attack_class,
        source_group=source_group,
        matched_group=None,
        control_kind="natural_benign",
        visibility="full",
        objective=objective,
        damage_envelope=damage_envelope,
        proposed_action=proposed_action,
        environment=environment,
        recent_actions=recent_actions,
        mitigation_guidance=mitigation_guidance,
        policy_contexts=policy_contexts,
        lineage=lineage,
        obfuscation=obfuscation,
        placement=placement,
        language=language,
    )
