"""The rollout restamp for note rows written before provenance stamping.

Ambient eligibility derives from a note's stored tier, and an absent envelope
reads as ``unknown_external``. Rows written before stamping existed therefore
need an envelope once, or the pre-rollout corpus becomes the largest new source
of taint in the system. The stored data cannot tell those rows apart in
general, so the classification is:

1. cohorts the data *can* identify as externally authored -- today, call
   transcripts -- are stamped ``unknown_external``, and adding a write path
   that stamps external provenance includes adding its cohort here;
2. rows the operator names (by title, or title pattern) are stamped
   ``unknown_external``, for imports they know about;
3. everything else is stamped ``trusted_internal``: a deliberate operator
   judgment that the indistinguishable remainder is household material.

Each restamp goes through the notes repository, so it enqueues the row's
indexing task like any write, and records a taint audit event naming the batch
and the rule that classified the row. See
docs/design/ambient-note-admission-at-write-time.md, "Existing rows".
"""

from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
)
from family_assistant.security.taint_audit import taint_audit_sources

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.storage.database import Database
    from family_assistant.storage.repositories.notes import UnstampedNote

CALL_TRANSCRIPT_TITLE_PREFIX = "Call Transcript:"
RESTAMP_EVENT_TYPE = "note_provenance_restamp"


class RestampRule(StrEnum):
    """Which rule classified a row."""

    CALL_TRANSCRIPT = "call_transcript"
    OPERATOR_EXCLUDED = "operator_excluded"
    HOUSEHOLD_DEFAULT = "household_default"


@dataclass(frozen=True)
class RestampExclusions:
    """What the operator knows to be external, beyond the identifiable cohorts."""

    titles: frozenset[str] = frozenset()
    title_patterns: tuple[str, ...] = ()
    transcript_labels: frozenset[str] = frozenset()
    """Visibility labels the call-transcript profile applies to its notes."""


@dataclass(frozen=True)
class RestampDecision:
    """How one unstamped row is classified."""

    note: UnstampedNote
    rule: RestampRule

    @property
    def tier(self) -> SourceTrustTier:
        if self.rule is RestampRule.HOUSEHOLD_DEFAULT:
            return SourceTrustTier.TRUSTED_INTERNAL
        return SourceTrustTier.UNKNOWN_EXTERNAL


def classify_unstamped_note(
    note: UnstampedNote, exclusions: RestampExclusions
) -> RestampDecision:
    """Classify one row with no provenance envelope."""
    if note.title.startswith(CALL_TRANSCRIPT_TITLE_PREFIX) or (
        exclusions.transcript_labels
        and exclusions.transcript_labels <= set(note.visibility_labels)
    ):
        return RestampDecision(note=note, rule=RestampRule.CALL_TRANSCRIPT)
    if note.title in exclusions.titles or any(
        fnmatch.fnmatchcase(note.title, pattern)
        for pattern in exclusions.title_patterns
    ):
        return RestampDecision(note=note, rule=RestampRule.OPERATOR_EXCLUDED)
    return RestampDecision(note=note, rule=RestampRule.HOUSEHOLD_DEFAULT)


def _restamp_state(decision: RestampDecision, *, batch_id: str) -> TurnTaintState:
    state = TurnTaintState.empty()
    if decision.tier is SourceTrustTier.TRUSTED_INTERNAL:
        return state.with_authorship_floor()
    return state.add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id=decision.note.title,
            tier=decision.tier,
            labels=frozenset({f"restamp:{decision.rule.value}"}),
            reason=(
                f"Rollout restamp {batch_id} classified this pre-stamping note "
                f"as external ({decision.rule.value})."
            ),
        )
    )


async def plan_note_restamp(
    db: Database, exclusions: RestampExclusions
) -> list[RestampDecision]:
    """Classify every note row that has no provenance envelope."""
    return [
        classify_unstamped_note(note, exclusions)
        for note in await db.notes.list_missing_provenance()
    ]


async def apply_note_restamp(
    db: Database,
    decisions: Iterable[RestampDecision],
    *,
    batch_id: str | None = None,
) -> list[RestampDecision]:
    """Stamp each decided row and audit it; returns the rows actually stamped.

    A row that gained an envelope between planning and applying is skipped.
    """
    batch = batch_id or f"note-restamp-{uuid.uuid4()}"
    applied: list[RestampDecision] = []
    for decision in decisions:
        state = _restamp_state(decision, batch_id=batch)
        stamped = await db.notes.restamp_provenance(
            decision.note.id, state=state, expected_title=decision.note.title
        )
        if not stamped:
            continue
        await db.taint_audit_events.add(
            event_id=str(uuid.uuid4()),
            event_type=RESTAMP_EVENT_TYPE,
            conversation_id=batch,
            turn_id=None,
            processing_profile_id=None,
            subconversation_id=None,
            tool_name="restamp_note_provenance",
            tool_call_id=None,
            sink_class=None,
            max_tier=state.max_tier.config_value,
            sources=taint_audit_sources(state),
            requested_outcome=None,
            effective_outcome=decision.rule.value,
            mode=None,
            reason=(
                f"Batch {batch} stamped a pre-stamping note "
                f"{state.max_tier.config_value} by rule {decision.rule.value}."
            ),
            arguments_summary=None,
            artifact_id=f"note:{decision.note.id}",
        )
        applied.append(decision)
    return applied
