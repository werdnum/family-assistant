"""Stored note provenance: how a note row's taint is written and read back.

A note's stored envelope decides two things: which taint an explicit read of
the note propagates into the turn, and whether the note may reach a prompt
unasked (see docs/design/ambient-note-admission-at-write-time.md). Both are
decided here so the repository, the notes context provider and the note tools
cannot disagree about what a row's provenance means.

After the rollout restamp every note row carries an envelope, so an absent one
is a write-path regression: it is read as ``unknown_external`` and logged at
ERROR, never parsed into an empty trusted state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    is_admissible_for_reuse,
    merge_taint_states,
    raise_taint_state_to,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

MISSING_NOTE_PROVENANCE_LABEL = "missing_note_provenance"


class NoteProvenanceMetadata(TypedDict):
    """The provenance envelope stored on a note row."""

    taint_metadata: TaintMetadata
    provenance_labels: list[str]


_PROVENANCE_LABELS_BY_TIER: dict[SourceTrustTier, str] = {
    SourceTrustTier.MACHINE_REVIEWED: "source_machine_reviewed",
    SourceTrustTier.KNOWN_CONTACT: "source_known_contact",
    SourceTrustTier.RECOGNIZED_MACHINE: "source_recognized_machine",
    SourceTrustTier.UNKNOWN_EXTERNAL: "source_unknown_external",
}


def note_provenance_metadata(state: TurnTaintState) -> NoteProvenanceMetadata:
    """Serialize a note's final taint state into its stored envelope."""
    label = _PROVENANCE_LABELS_BY_TIER.get(state.max_tier)
    return {
        "taint_metadata": state.to_metadata(),
        "provenance_labels": [label] if label is not None else [],
    }


def _missing_provenance_state(title: str) -> TurnTaintState:
    return TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id=title,
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({MISSING_NOTE_PROVENANCE_LABEL}),
            reason=f"Note '{title}' has no stored provenance envelope.",
        )
    )


def stored_note_state(
    provenance_metadata: Mapping[str, object] | None,
    *,
    title: str,
) -> TurnTaintState:
    """The taint a note row carries, with an absent envelope read as external."""
    raw = (
        provenance_metadata.get("taint_metadata")
        if provenance_metadata is not None
        else None
    )
    if raw is None:
        logger.error(
            "Note '%s' has no stored provenance envelope; treating it as "
            "unknown_external. Every note write stamps provenance, so this is a "
            "write-path regression or a row the rollout restamp did not cover.",
            title,
        )
        return _missing_provenance_state(title)
    return TurnTaintState.from_metadata(raw)


def stored_note_tier(
    provenance_metadata: Mapping[str, object] | None,
    *,
    title: str,
) -> SourceTrustTier:
    """The tier a note row carries, with an absent envelope read as external."""
    return stored_note_state(provenance_metadata, title=title).max_tier


def is_ambient_eligible(
    provenance_metadata: Mapping[str, object] | None,
    *,
    title: str,
) -> bool:
    """Whether a note's stored tier admits it to a prompt nobody asked it into.

    Applies to the full-content ambient surfaces: a prompt-included body and a
    database skill's catalog entry. Titles are not gated.
    """
    return is_admissible_for_reuse(stored_note_tier(provenance_metadata, title=title))


def note_read_taint(
    provenance_metadata: Mapping[str, object] | None,
    *,
    title: str,
    labels: frozenset[str],
    reason: str,
) -> TurnTaintState | None:
    """The taint an explicit read of a note contributes, or None for trusted rows.

    A trusted-pole note contributes nothing; any other note contributes its
    stored sources plus one source naming the note itself, unless the envelope
    already names it (an admitted note's envelope is exactly that source).
    """
    state = stored_note_state(provenance_metadata, title=title)
    if state.max_tier <= SourceTrustTier.TRUSTED_INTERNAL:
        return None
    if any(
        source.source_type is TaintSourceType.NOTE and source.source_id == title
        for source in state.sources
    ):
        return state
    return state.add_source(
        TaintSource(
            source_type=TaintSourceType.NOTE,
            source_id=title,
            tier=state.max_tier,
            labels=labels,
            reason=reason,
        )
    )


class NoteWriter(StrEnum):
    """Who is writing a note, which decides how its stamp is resolved."""

    USER = "user"
    """An authenticated user's own edit: stamps ``trusted_user``, whole."""
    MACHINE = "machine"
    """Composed by the system: floored at ``trusted_internal``, merged with
    whatever the write retains from the stored note."""
    ADMITTED = "admitted"
    """A candidate an admission decision promoted: replaces the envelope."""


@dataclass(frozen=True, slots=True)
class NoteProvenanceStamp:
    """The provenance a writer supplies for one note write.

    Required by every repository write. Construct through the class methods so
    the writer's trust is explicit at the call site.
    """

    writer: NoteWriter
    state: TurnTaintState
    floor: SourceTrustTier | None = None

    @classmethod
    def user_edit(cls) -> NoteProvenanceStamp:
        """An authenticated user's own edit, through the web API."""
        return cls(writer=NoteWriter.USER, state=TurnTaintState.empty())

    @classmethod
    def machine(
        cls,
        state: TurnTaintState,
        *,
        floor: SourceTrustTier | None = None,
    ) -> NoteProvenanceStamp:
        """Content the system composed, from a turn (or process) at ``state``."""
        return cls(writer=NoteWriter.MACHINE, state=state, floor=floor)

    @classmethod
    def internal(cls) -> NoteProvenanceStamp:
        """Deployment-authored structure with no turn behind it."""
        return cls.machine(TurnTaintState.empty())

    @classmethod
    def external(cls, *, source_id: str, reason: str) -> NoteProvenanceStamp:
        """Content authored outside the trust boundary, never reviewed."""
        return cls.machine(
            TurnTaintState.empty().add_source(
                TaintSource(
                    source_type=TaintSourceType.NOTE,
                    source_id=source_id,
                    tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                    labels=frozenset(),
                    reason=reason,
                )
            )
        )

    @classmethod
    def admitted(cls, *, title: str, decided_by: str) -> NoteProvenanceStamp:
        """A candidate admitted for unasked reuse; replaces the stored envelope."""
        return cls(
            writer=NoteWriter.ADMITTED,
            state=TurnTaintState.empty().add_source(
                TaintSource(
                    source_type=TaintSourceType.NOTE,
                    source_id=title,
                    tier=SourceTrustTier.MACHINE_REVIEWED,
                    labels=frozenset(),
                    reason=f"Admitted for reuse by {decided_by}.",
                )
            ),
        )


def resolve_note_stamp(
    stamp: NoteProvenanceStamp,
    *,
    retained: TurnTaintState | None,
) -> TurnTaintState:
    """The final taint state a note write persists.

    ``retained`` is the stored state of whatever the write keeps from the
    existing row (None for a new note). A machine write never lowers a stamp,
    because the title is always retained; a user edit and an admission replace
    the envelope outright.
    """
    if stamp.writer is NoteWriter.USER:
        return TurnTaintState.empty()
    if stamp.writer is NoteWriter.ADMITTED:
        return stamp.state
    state = stamp.state.with_authorship_floor()
    if retained is not None:
        state = merge_taint_states(state, retained)
    if stamp.floor is not None:
        state = raise_taint_state_to(
            state,
            stamp.floor,
            reason="Unadmitted external candidate floored below reusable tiers.",
        )
    return state
