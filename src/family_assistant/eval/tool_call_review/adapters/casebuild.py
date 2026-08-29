"""Shared helpers for turning upstream rows into paired reviewer cases.

Every adapter must pair injected upstream text with the gated call it argues
for, wrapped in valid taint provenance: the injected content sits in a
non-trusted-tier message row, its taint metadata comes from
:meth:`TurnTaintState.to_metadata`, and the turn's taint state carries the same
untrusted tier so the reviewer sees the boundary it must rule across. These
helpers centralize that construction so the two adapters cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from family_assistant.eval.tool_call_review.adapters.base import (
    AdaptedCase,
    AdaptedLineage,
    normalized_text_key,
)
from family_assistant.eval.tool_call_review.schema import EvalCase
from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from family_assistant.security.taint import TaintMetadata

__all__ = [
    "FULL_VERDICT_CONSTRAINTS",
    "build_adapted_case",
    "untrusted_source_metadata",
]

# Adapted cases state the full verdict space with a fail-closed ``confirm``
# fallback: the delegating taint cell for an untrusted-tier egress adjudicates,
# and the runner never invents a verdict space.
FULL_VERDICT_CONSTRAINTS = {
    "available_verdicts": ["allow", "confirm", "deny"],
    "fallback_verdict": "confirm",
}


def untrusted_source_metadata(
    *,
    source_type: TaintSourceType,
    source_id: str,
    reason: str,
    tier: SourceTrustTier = SourceTrustTier.UNKNOWN_EXTERNAL,
) -> TaintMetadata:
    """Return taint metadata for one untrusted content row.

    The metadata is produced by :meth:`TurnTaintState.to_metadata` rather than
    hand-written, so it always matches the runtime serialization the loader
    deserializes with :meth:`TurnTaintState.from_metadata`.
    """
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=source_type,
            source_id=source_id,
            tier=tier,
            labels=frozenset(),
            reason=reason,
        )
    )
    return state.to_metadata()


def build_adapted_case(
    *,
    case_id: str,
    source: str,
    label: str,
    attack_class: str,
    tool_name: str,
    sink_class: str,
    messages: list[dict[str, object]],
    arguments: Mapping[str, object],
    turn_taint: TaintMetadata,
    policy_identifier: str,
    policy_description: str,
    lineage: AdaptedLineage,
    obfuscation: str | None = None,
    placement: str | None = None,
    language: str | None = None,
    deployment_guidance: str = "",
) -> AdaptedCase:
    """Validate and pair one conversation case with its lineage.

    ``attack_class`` is carried on benign twins as the matched class too, which
    the schema permits, so a twin can be sliced alongside the attack it mirrors.
    """
    case = EvalCase.model_validate({
        "id": case_id,
        "boundary": "conversation",
        "label": label,
        "attack_class": attack_class,
        "source": source,
        "obfuscation": obfuscation,
        "placement": placement,
        "language": language,
        "constraints": FULL_VERDICT_CONSTRAINTS,
        "payload": {
            "tool_name": tool_name,
            "sink_class": sink_class,
            "messages": messages,
            "arguments": arguments,
            "taint_state": turn_taint,
            "deployment_guidance": deployment_guidance,
            "policy_contexts": [
                {
                    "kind": "taint_cell",
                    "identifier": policy_identifier,
                    "description": policy_description,
                }
            ],
        },
    })
    return AdaptedCase(case=case, lineage=lineage)


def text_key(text: str) -> str:
    """Re-export the normalized dedup key for adapter convenience."""
    return normalized_text_key(text)
