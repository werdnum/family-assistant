"""Definition records for executable definitions.

An automation, event listener, or stored script is stored intent: an
instruction written today that wakes an agent tomorrow with no human present.
The firing turn cannot judge that intent -- the authoring conversation is long
gone -- so the judgment happens at the creation chokepoint and its outcome is
recorded here. See ``docs/design/executable-definition-taint.md``.

A record has three parts:

* the **authoring stamp**, the authoring turn's taint metadata, written
  deterministically at every executable-persistence write;
* the **content hash**, over the definition's executable fields, binding the
  record to the content it describes -- a record that outlives its content is a
  laundering primitive, so a mismatch voids the record and fails closed;
* the **creation disposition**, how the write left the gate.

This module owns the record's shape, its canonical hash, and the single helper
every write path must obtain a record from. It deliberately holds no storage or
policy: resolution at firing time is a pure function of a stored record.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, TypedDict, cast

from family_assistant.security.taint import (
    SourceTrustTier,
    TaintMetadata,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    TurnTaintTracker,
    coerce_taint_metadata,
    is_externally_authored,
    machine_authored_taint_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFINITION_RECORD_VERSION = "definition_v1"


class GateLayer(StrEnum):
    """Which layer gated an executable-persistence write."""

    TAINT_CELL = "taint_cell"
    STATIC_RULE = "static_rule"
    CONFIRMATION = "confirmation"


class CreationDisposition(StrEnum):
    """How an executable-persistence write left its gate.

    Every verdict is recorded, for audit and for the flip-time backlog listing,
    but only a **real decision about this definition** cures the authoring
    taint. ``JUDGE_ALLOWED`` and ``HUMAN_CONFIRMED`` cure; the escalation
    verdicts resolve exactly as an absent record.
    """

    CLEAN = "clean"
    """The authoring turn held no externally authored taint; no gate engaged.

    Not itself a cure -- a clean-stamped definition resolves trusted on its
    stamp. Recorded so that "no gate was needed" is distinguishable from "no
    gate ran".
    """

    HUMAN_CONFIRMED = "human_confirmed"
    """A durable confirmation rendered the executable fields in full and a human approved."""

    JUDGE_ALLOWED = "judge_allowed"
    """The reviewer returned ``allow`` for the write.

    Cures in **every** ``taint_policy.mode``: the reviewer that runs at an
    ``adjudicate`` cell under ``observe`` is the same reviewer, over the same
    records, with the same authoring context, computing the same verdict as
    under ``enforce``. The mode changes what happens on a deny, and nothing
    about what an ``allow`` means.
    """

    JUDGE_CONFIRM_REQUIRED = "judge_confirm_required"
    """The reviewer returned ``confirm`` and no human approved it.

    An unapproved escalation decided nothing about the definition, so it does
    not cure. An approval that follows is recorded as ``HUMAN_CONFIRMED``.
    """

    JUDGE_DENIED = "judge_denied"
    """The reviewer returned ``deny``.

    Never cures in any mode: under ``enforce`` the write does not happen, and
    under ``observe`` it happened *against* the verdict -- the one population a
    recorded verdict argues about, in the uncured direction.
    """

    @property
    def cures(self) -> bool:
        """Whether this disposition cures the authoring taint for future firings."""
        return self in {
            CreationDisposition.HUMAN_CONFIRMED,
            CreationDisposition.JUDGE_ALLOWED,
        }


@dataclass(frozen=True, slots=True)
class GateProvenance:
    """Which gate produced a disposition, recorded for audit only.

    Resolution never reads these: a disposition validly granted stands until the
    content changes, and is not re-evaluated against the configuration or the
    reviewer in effect at the firing. They exist so that applying an operator
    floor, recalibrating the reviewer, or approaching the flip to ``enforce``
    comes with a filterable review of the existing judge-cured estate rather
    than an audit.
    """

    layer: GateLayer
    mode: str
    reviewer_revision: str | None = None
    verdict_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialize for storage."""
        return {
            "layer": self.layer.value,
            "mode": self.mode,
            "reviewer_revision": self.reviewer_revision,
            "verdict_id": self.verdict_id,
        }

    @classmethod
    def from_dict(cls, raw: object) -> GateProvenance | None:
        """Parse stored gate provenance, or ``None`` when it is absent or unreadable."""
        if not isinstance(raw, dict):
            return None
        try:
            layer = GateLayer(raw.get("layer"))
        except ValueError:
            return None
        mode = raw.get("mode")
        if not isinstance(mode, str):
            return None
        reviewer_revision = raw.get("reviewer_revision")
        verdict_id = raw.get("verdict_id")
        return cls(
            layer=layer,
            mode=mode,
            reviewer_revision=(
                reviewer_revision if isinstance(reviewer_revision, str) else None
            ),
            verdict_id=verdict_id if isinstance(verdict_id, str) else None,
        )


class DefinitionRecordDict(TypedDict):
    """Serialized definition record, as persisted beside a definition."""

    version: str
    taint_metadata: TaintMetadata
    content_hash: str
    disposition: str | None
    gate: dict[str, str | None] | None


@dataclass(frozen=True, slots=True)
class DefinitionRecord:
    """Authoring stamp, content hash, and creation disposition for one definition."""

    taint_metadata: TaintMetadata
    content_hash: str
    disposition: CreationDisposition | None
    gate: GateProvenance | None = None

    @property
    def cures(self) -> bool:
        """Whether this record's disposition cures its authoring taint."""
        return self.disposition is not None and self.disposition.cures

    def to_dict(self) -> DefinitionRecordDict:
        """Serialize for storage."""
        return {
            "version": DEFINITION_RECORD_VERSION,
            "taint_metadata": self.taint_metadata,
            "content_hash": self.content_hash,
            "disposition": (
                self.disposition.value if self.disposition is not None else None
            ),
            "gate": self.gate.to_dict() if self.gate is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: object) -> DefinitionRecord | None:
        """Parse a stored record, or ``None`` when it cannot be trusted to mean anything.

        Every malformed shape returns ``None`` rather than a partially-read
        record: an absent record and an unreadable one get identical
        fail-closed treatment at resolution time, so there is nothing for a
        half-parsed record to express.
        """
        if not isinstance(raw, dict):
            return None
        if raw.get("version") != DEFINITION_RECORD_VERSION:
            return None
        taint_metadata = coerce_taint_metadata(raw.get("taint_metadata"))
        content_hash = raw.get("content_hash")
        if taint_metadata is None or not isinstance(content_hash, str):
            return None
        raw_disposition = raw.get("disposition")
        disposition: CreationDisposition | None = None
        if raw_disposition is not None:
            try:
                disposition = CreationDisposition(raw_disposition)
            except ValueError:
                return None
        return cls(
            taint_metadata=taint_metadata,
            content_hash=content_hash,
            disposition=disposition,
            gate=GateProvenance.from_dict(raw.get("gate")),
        )

    def matches(self, content: Mapping[str, object]) -> bool:
        """Whether this record still describes *content*."""
        return self.content_hash == definition_content_hash(content)


def _canonical_scalar(value: object) -> object:
    """Canonicalize a value JSON cannot encode, or refuse it.

    Enum members are the one case that matters here: a column read back from
    the database yields ``EventActionType.wake_llm`` where the write passed the
    string ``"wake_llm"``, and a hash that told those apart would report a
    mismatch on content nobody changed. Anything else raises, so a field that
    cannot be canonicalized fails at the write rather than silently at a firing.
    """
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Enum):
        return _canonical_scalar(value.value)
    msg = f"Definition content is not canonicalizable: {type(value).__name__}"
    raise TypeError(msg)


def definition_content_hash(content: Mapping[str, object]) -> str:
    """Hash the executable fields that determine what runs and what the agent is told.

    Canonicalized by sorting keys and rejecting non-JSON values, so the same
    definition hashes identically however the caller assembled the mapping, and
    a field that cannot be canonicalized fails loudly at the write rather than
    silently at the firing.
    """
    canonical = json.dumps(
        content,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=_canonical_scalar,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def stamp_definition(
    *,
    content: Mapping[str, object],
    taint_state: TurnTaintState | None,
    disposition: CreationDisposition | None = None,
    gate: GateProvenance | None = None,
    human_direct: bool = False,
) -> DefinitionRecord:
    """Obtain a fresh record for an executable-persistence write.

    The single chokepoint every write path routes through, so that a new
    scheduling or editing tool gets the behaviour by construction. ``content``
    is the *complete post-mutation* definition -- a patch-style caller merges
    omitted fields from the stored row first, so what a gate saw and what the
    hash covers are the same content.

    ``human_direct`` marks a write with no model in the loop (the authenticated
    web UI), which stamps ``trusted_user`` by construction. Everything else is
    machine-composed and floors at ``trusted_internal``, so a model-composed
    definition never reads back as the human's own words.
    """
    state = taint_state if taint_state is not None else TurnTaintState.empty()
    metadata = (
        state.to_metadata() if human_direct else machine_authored_taint_metadata(state)
    )
    return DefinitionRecord(
        taint_metadata=metadata,
        content_hash=definition_content_hash(content),
        disposition=disposition,
        gate=gate,
    )


def authoring_taint_state(tracker: TurnTaintTracker | None) -> TurnTaintState:
    """The authoring turn's taint, as a definition record's stamp.

    A turn with no tracker cannot prove it was clean, so it stamps as an
    unknown-external authoring rather than as the trusted pole: a definition
    written without provenance must not fire as trusted intent.
    """
    if tracker is None:
        return TurnTaintState.empty().add_source(
            TaintSource(
                source_type=TaintSourceType.MANUAL,
                source_id=None,
                tier=SourceTrustTier.UNKNOWN_EXTERNAL,
                labels=frozenset({"untracked_authoring_turn"}),
                reason="Definition authored in a turn with no taint tracker.",
            )
        )
    return tracker.snapshot()


def merge_retained_definition(
    state: TurnTaintState,
    *,
    stored_record: object,
    retained_content: Mapping[str, object],
) -> TurnTaintState:
    """Merge what a partial update *retains* into the updating turn.

    A patch-style update keeps the fields it does not mention, so the prior
    definition's content survives into the new one. Reading it back is an
    artifact read like any other: a clean-turn patch to a legacy or uncured
    definition must not launder it, so the retained content's effective tier
    enters the turn and the new record's stamp carries the result.

    ``retained_content`` is the *prior* definition, so the stored record is
    checked against the content it actually described -- a record whose hash no
    longer matches is void, and voids resolve as uncured.
    """
    record = definition_record_from_row(stored_record)
    resolved_clean = (
        record is not None
        and record.matches(retained_content)
        and (
            record.cures
            or not is_externally_authored(
                TurnTaintState.from_metadata(record.taint_metadata).max_tier
            )
        )
    )
    if resolved_clean:
        return state
    return state.add_source(
        TaintSource(
            source_type=TaintSourceType.MANUAL,
            source_id=None,
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset({"retained_definition_content"}),
            reason=(
                "Updated definition retains content whose stored record is "
                "absent, void, or uncured."
            ),
        )
    )


def definition_record_from_row(raw: object) -> DefinitionRecord | None:
    """Read the record stored on a definition row."""
    if isinstance(raw, str):
        try:
            raw = cast("object", json.loads(raw))
        except json.JSONDecodeError:
            return None
    return DefinitionRecord.from_dict(raw)
