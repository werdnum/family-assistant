"""Rendering a turn's taint state into the audit trail's bounded shape.

The audit table is read by people and by diagnostics, and what goes into it is
attacker-influenced text: a source id, a label, the reason a source was
recorded. Normalising it, bounding its length, and stubbing the details of an
externally authored source are the same three rules wherever an audit event is
written -- a tool call the taint matrix gated, a memory review skipped before
its model call -- so they live here rather than at each writer.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from family_assistant.security.taint import is_externally_authored

if TYPE_CHECKING:
    from family_assistant.security.taint import TurnTaintState
    from family_assistant.storage.types import TaintAuditSourceSummary


def normalize_audit_text(value: str) -> str:
    """Normalize one audit string, stripping controls and direction tricks."""
    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in normalized
    )
    return " ".join(cleaned.split()).strip()


def bounded_audit_text(value: str, limit: int) -> str:
    """Return normalized audit text within a fixed display bound."""
    cleaned = normalize_audit_text(value)
    if len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def taint_audit_sources(
    state: TurnTaintState,
    *,
    max_sources: int = 12,
) -> list[TaintAuditSourceSummary]:
    """The turn's sources as the audit table stores them.

    Externally authored source metadata is deliberately stubbed, just as it is
    in the reviewer prompt's provenance digest: the audit says that an
    untrusted source was present, not what it said.
    """
    summaries: list[TaintAuditSourceSummary] = []
    for source in state.sources[:max_sources]:
        trusted = not is_externally_authored(source.tier)
        summaries.append({
            # These are enum-backed, closed-vocabulary provenance fields.
            "source_type": source.source_type.value,
            "tier": source.tier.config_value,
            "source_id": bounded_audit_text(source.source_id, 256)
            if trusted and source.source_id is not None
            else None,
            "labels": (
                sorted(bounded_audit_text(label, 64) for label in source.labels)[:16]
                if trusted
                else []
            ),
            "reason": (
                bounded_audit_text(source.reason, 256)
                if trusted
                else "Externally authored source details omitted from audit."
            ),
        })
    return summaries
