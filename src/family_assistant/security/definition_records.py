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

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any, TypedDict, cast

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

    from family_assistant.storage.types import ActionConfig, MatchConditions

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


class DefinitionArtifactKind(StrEnum):
    """Where an executable definition's record lives, for a late-arriving verdict."""

    SCHEDULE_AUTOMATION = "schedule_automation"
    EVENT_LISTENER = "event_listener"
    SCRIPT = "script"
    TASK_PAYLOAD = "task_payload"


@dataclass(frozen=True, slots=True)
class DefinitionWriteRef:
    """One executable definition a gated call wrote, and where it lives.

    Registered by the write itself, because only the write knows the identity
    the store assigned it. A verdict that lands later uses this to find the row
    it must attach to, and the write id to check the row still holds the write
    the verdict judged.
    """

    artifact_kind: DefinitionArtifactKind
    artifact_id: str


@dataclass
class PendingDefinitionReview:
    """A verdict being computed off the critical path, and the writes awaiting it.

    Under ``observe`` the reviewer deliberately does not block the call, so a
    definition is written before its verdict exists and resolves uncured until
    the verdict lands -- a latency-bounded window, seconds wide, in which a busy
    listener may fire more than once, every such firing biased conservative.
    """

    write_id: str
    writes: list[DefinitionWriteRef] = field(default_factory=list)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the gated call has finished, so every write it made is registered."""

    def register(self, ref: DefinitionWriteRef, stored_record: object) -> None:
        """Record a write this review's verdict should attach to."""
        record = definition_record_from_row(stored_record)
        if record is not None and record.pending_write_id == self.write_id:
            self.writes.append(ref)


@dataclass(frozen=True, slots=True)
class DefinitionGateOutcome:
    """How the gate that examined a write resolved, as the write path sees it.

    Deposited by the confirmation and adjudication chokepoints themselves, so a
    new scheduling or editing tool inherits the behaviour by forwarding one
    field rather than by reimplementing the mapping from verdicts to
    dispositions.
    """

    disposition: CreationDisposition | None
    """How the gate resolved, or ``None`` while an observe-mode review is still running."""

    gate: GateProvenance
    cure_permitted: bool = True
    """Whether this verdict was computed under the enforce-equivalent verdict space.

    A static ``review`` rule co-gating a call under ``observe`` omits the taint
    cell's constraints from the merged review, so a floored cell whose space is
    ``{confirm, deny}`` can be widened back to include ``allow`` by the static
    layer's presence -- an ``allow`` ``enforce`` could never have issued. Such a
    verdict is still recorded as the verdict it was, for audit and for the
    flip-time backlog listing; what it loses is the cure.
    """

    pending: PendingDefinitionReview | None = None
    """The still-running review whose verdict this write awaits, under ``observe``."""


class DefinitionRecordDict(TypedDict):
    """Serialized definition record, as persisted beside a definition."""

    version: str
    taint_metadata: TaintMetadata
    content_hash: str
    disposition: str | None
    gate: dict[str, str | None] | None
    pending_write_id: str | None
    cure_eligible: bool


@dataclass(frozen=True, slots=True)
class DefinitionRecord:
    """Authoring stamp, content hash, and creation disposition for one definition."""

    taint_metadata: TaintMetadata
    content_hash: str
    disposition: CreationDisposition | None
    gate: GateProvenance | None = None
    cure_eligible: bool = True
    """Whether the recorded decision *binds this content*, or is only recorded.

    Kept separate from the disposition because they are two different facts, and
    conflating them would make the audit record lie. A human who approves a
    patch of a legacy definition really did approve -- the record says
    ``human_confirmed``, because that is what happened -- but the confirmation
    showed them the fields the call changed and never the ones the stored row
    supplied, so their approval cannot vouch for the merged definition. The same
    holds for an ``allow`` issued under a wider verdict space than ``enforce``
    would offer. Both are recorded as the decisions they were, and neither
    cures.

    Legacy records, which predate the field, read as eligible: their
    dispositions were only ever written where they bound.
    """

    pending_write_id: str | None = None
    """Identifies this write to a verdict still being computed for it.

    Under ``observe`` the reviewer runs off the critical path, so the write
    executes before its verdict exists and the verdict attaches afterwards.
    It attaches to *this write*, not to this content: a same-content rewrite
    from another turn is a different write, judged against different authoring
    context, and must never inherit a verdict computed for someone else's
    request. Any replacement of the record therefore leaves the new write
    awaiting its own verdict, and until one lands the definition resolves
    uncured.
    """

    @property
    def cures(self) -> bool:
        """Whether this record's decision cures its authoring taint.

        Both halves are required: a curing disposition, and a decision that
        actually bound the content this record describes.
        """
        return (
            self.disposition is not None
            and self.disposition.cures
            and self.cure_eligible
        )

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
            "pending_write_id": self.pending_write_id,
            "cure_eligible": self.cure_eligible,
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
        pending_write_id = raw.get("pending_write_id")
        return cls(
            taint_metadata=taint_metadata,
            content_hash=content_hash,
            disposition=disposition,
            gate=GateProvenance.from_dict(raw.get("gate")),
            pending_write_id=(
                pending_write_id if isinstance(pending_write_id, str) else None
            ),
            cure_eligible=raw.get("cure_eligible") is not False,
        )

    def with_verdict(
        self,
        disposition: CreationDisposition,
        gate: GateProvenance,
    ) -> DefinitionRecord:
        """This record with a late-arriving verdict recorded against it.

        The authoring stamp and the content hash are untouched: a verdict is an
        additive record beside the stamp, never a mutation of it. ``cure_eligible``
        is untouched too -- the write already decided whether a verdict could bind
        it, and the verdict's arrival does not revisit that.
        """
        return replace(
            self,
            disposition=disposition,
            gate=gate,
            pending_write_id=None,
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
    gate_outcome: DefinitionGateOutcome | None = None,
    retains_uncured_content: bool = False,
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

    ``gate_outcome`` is how the gate that examined this write resolved, as
    deposited by the confirmation and adjudication chokepoints -- or, under
    ``observe``, that a verdict is still being computed for it, in which case
    the record carries the write id the verdict will attach to. Its decision is
    always recorded as the decision it was; whether that decision *binds* this
    content is recorded beside it. ``retains_uncured_content`` says a patch kept
    content whose own record is absent, void, or uncured, which the gate never
    examined, so whoever decided cannot vouch for the merged definition. A clean
    stamp needs no cure and records ``CLEAN`` when no gate engaged, so that "no
    gate was needed" stays distinguishable from "no gate ran".
    """
    state = taint_state if taint_state is not None else TurnTaintState.empty()
    metadata = (
        state.to_metadata() if human_direct else machine_authored_taint_metadata(state)
    )
    externally_authored = is_externally_authored(
        TurnTaintState.from_metadata(metadata).max_tier
    )
    disposition: CreationDisposition | None = None
    gate: GateProvenance | None = None
    pending_write_id: str | None = None
    # Two independent reasons a decision may fail to bind this content: the
    # gate was shown less than the record vouches for, or the verdict space it
    # was computed under was wider than ``enforce`` would have offered.
    cure_eligible = not retains_uncured_content
    if gate_outcome is not None:
        gate = gate_outcome.gate
        disposition = gate_outcome.disposition
        cure_eligible = cure_eligible and gate_outcome.cure_permitted
        if gate_outcome.pending is not None and externally_authored:
            # Only a write that needs curing waits for a verdict; a clean stamp
            # resolves on its own. A write no verdict could bind still waits,
            # so the verdict is recorded against it when it lands.
            pending_write_id = gate_outcome.pending.write_id
    if disposition is None and not externally_authored:
        disposition = CreationDisposition.CLEAN
    return DefinitionRecord(
        taint_metadata=metadata,
        content_hash=definition_content_hash(content),
        disposition=disposition,
        gate=gate,
        pending_write_id=pending_write_id,
        cure_eligible=cure_eligible,
    )


def register_definition_write(
    gate_outcome: DefinitionGateOutcome | None,
    definition_record: object,
    *,
    kind: DefinitionArtifactKind,
    artifact_id: str | int,
) -> None:
    """Tell a still-running review where the write it is judging ended up.

    Called by the write itself, because only the write knows the identity its
    store assigned. A no-op for every gate that resolved before the call ran,
    which is every gate under ``enforce``, and for a write the gate's verdict
    could not vouch for anyway.
    """
    if gate_outcome is None or gate_outcome.pending is None:
        return
    gate_outcome.pending.register(
        DefinitionWriteRef(artifact_kind=kind, artifact_id=str(artifact_id)),
        definition_record,
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


def automation_definition_content(
    *,
    name: str | None,
    description: str | None,
    recurrence_rule: str,
    action_type: str,
    action_config: ActionConfig | None,
) -> dict[str, object]:
    """The executable fields of a schedule automation, for hashing.

    Everything that determines *what runs* and *what the agent is told*: the
    schedule, the action, and the name and description a firing renders as
    intent. Management state (enabled, execution counts, next_scheduled_at) is
    excluded -- toggling an automation changes no content, so a valid record
    must survive it, per the design's activation rule.
    """
    return {
        "name": name,
        "description": description,
        "recurrence_rule": recurrence_rule,
        "action_type": action_type,
        "action_config": action_config,
    }


def listener_definition_content(
    *,
    name: str | None,
    description: str | None,
    source_id: str,
    match_conditions: MatchConditions,
    action_type: str,
    action_config: ActionConfig | None,
    condition_script: str | None,
) -> dict[str, object]:
    """The executable fields of an event listener, for hashing.

    What the listener matches on, what it then runs, and what a firing renders
    as intent. ``one_time``, ``enabled`` and the rate-limit counters are
    management state and excluded, so activation changes do not void a record.
    """
    return {
        "name": name,
        "description": description,
        "source_id": source_id,
        "match_conditions": match_conditions,
        "action_type": action_type,
        "action_config": action_config,
        "condition_script": condition_script,
    }


def script_definition_content(
    *,
    name: str,
    description: str,
    script_code: str,
    # ast-grep-ignore: no-dict-any - JSON Schema parameter is genuinely arbitrary
    parameters_schema: dict[str, Any] | None,
) -> dict[str, object]:
    """The executable fields of a stored script, for hashing.

    The body is the executable content; the name, description, and parameter
    schema shape how a caller invokes it and what a firing renders as intent.
    """
    return {
        "name": name,
        "description": description,
        "script_code": script_code,
        "parameters_schema": parameters_schema,
    }


def script_invocation_content(
    action_config: Mapping[str, object] | None,
) -> dict[str, object]:
    """The executable fields of a one-shot script action, for hashing.

    A ``schedule_action`` script invocation has no durable definition table:
    its definition is the action config the task payload carries, which names
    the body to run (inline code, or a stored script and the parameters to run
    it with) and the tool set and timeout to run it under. Hashing the config
    whole means a change to any of those voids the record, with nothing to
    enumerate and keep in step.

    The stored script a config *names* is a separate artifact with its own
    record; the closure walk resolves both, so an invocation cures only what
    the invoking turn is entitled to and never what the script body is.
    """
    return {"action_config": dict(action_config) if action_config is not None else None}


def callback_definition_content(definition: str | None) -> dict[str, object]:
    """The executable fields of a one-shot callback, for hashing.

    A reminder or future callback has no durable definition table: its
    definition is the text the firing turn is handed, and its record rides the
    enqueued payload.
    """
    return {"definition": definition}


def stamp_callback_definition(
    definition: str | None,
    *,
    tracker: TurnTaintTracker | None,
    gate_outcome: DefinitionGateOutcome | None = None,
) -> DefinitionRecordDict:
    """Stamp a one-shot callback's definition at enqueue, for its payload."""
    return stamp_definition(
        content=callback_definition_content(definition),
        taint_state=authoring_taint_state(tracker),
        gate_outcome=gate_outcome,
    ).to_dict()


@dataclass(frozen=True, slots=True)
class DefinitionResolution:
    """What a firing may make of one definition artifact's stored record.

    Resolution is a pure function of the stored record and the content it is
    checked against: a record whose hash no longer matches its content is void,
    and a void record is indistinguishable from an absent one. Both leave the
    definition unresolved, which is the fail-closed state -- the firing renders
    a stub and seeds the turn as an unattended external trigger, exactly as
    before this design.

    ``taint_metadata`` is what a resolved definition renders *as*: for a
    trusted-pole stamp it is the stamp itself; for a cured one it is the clean
    machine-authored baseline the definition would have had if authored
    untainted, since the cure restores that baseline and nothing more. Either
    way it is never ``trusted_user`` unless a human typed the definition, so
    the reviewer's human-words consumers stay honest.
    """

    taint_metadata: TaintMetadata | None
    disposition: CreationDisposition | None = None

    @property
    def resolved(self) -> bool:
        """Whether the definition may render as trusted intent."""
        return self.taint_metadata is not None

    @property
    def tier(self) -> SourceTrustTier:
        """The tier a resolved definition renders at; the untrusted floor when unresolved."""
        if self.taint_metadata is None:
            return SourceTrustTier.UNKNOWN_EXTERNAL
        return TurnTaintState.from_metadata(self.taint_metadata).max_tier

    def combine(self, other: DefinitionResolution) -> DefinitionResolution:
        """Weakest of two resolutions, for the executable closure walk.

        A firing executes or renders more than one artifact -- an automation and
        the stored script it names -- and each carries its own record, because
        cross-artifact hashes would rot the moment either side is edited. The
        weakest resolution therefore governs the whole firing: editing a shared
        script from a tainted turn un-cures every automation that references it
        until the script's own gate cures it again.

        The disposition combines separately from the tier, because two cured
        artifacts resolve to the same tier while making different claims. A
        closure claims a human's attestation only when *every* artifact in it
        carries one -- otherwise a human-confirmed automation naming a
        judge-cured script would render that script's code as human-attested,
        and hand it the destination echo the attestation earns.
        """
        weaker = other if other.tier > self.tier else self
        if not weaker.resolved:
            return weaker
        return DefinitionResolution(
            taint_metadata=weaker.taint_metadata,
            disposition=_weakest_claim(self.disposition, other.disposition),
        )


def _weakest_claim(
    first: CreationDisposition | None,
    second: CreationDisposition | None,
) -> CreationDisposition | None:
    """The strongest claim a closure of two artifacts may make about itself.

    ``human_confirmed`` is the only disposition that unlocks anything further
    (the destination echo), so it survives only unanimously. ``judge_allowed``
    otherwise surfaces, because a closure containing judge-cured content is not
    describable as clean.
    """
    both = (first, second)
    if all(item is CreationDisposition.HUMAN_CONFIRMED for item in both):
        return CreationDisposition.HUMAN_CONFIRMED
    if CreationDisposition.JUDGE_ALLOWED in both:
        return CreationDisposition.JUDGE_ALLOWED
    if all(item is None for item in both):
        return None
    return CreationDisposition.CLEAN


UNRESOLVED_DEFINITION = DefinitionResolution(taint_metadata=None)
"""A definition with no usable record: absent, unreadable, void, or uncured."""


def resolve_definition_record(
    stored_record: object,
    content: Mapping[str, object],
) -> DefinitionResolution:
    """Resolve a stored record against the content it is supposed to describe.

    Two things resolve a definition as trusted intent: a stamp at the trusted
    pole, meaning the authoring turn held nothing externally authored; or a
    curing disposition, meaning a gate made a real decision about this exact
    content. Everything else -- a tainted stamp no gate cured, an escalation
    nobody approved, a denial, a legacy row, content that changed under the
    record -- is unresolved and fails closed.
    """
    record = definition_record_from_row(stored_record)
    if record is None or not record.matches(content):
        return UNRESOLVED_DEFINITION
    stamp_tier = TurnTaintState.from_metadata(record.taint_metadata).max_tier
    if not is_externally_authored(stamp_tier):
        return DefinitionResolution(
            taint_metadata=record.taint_metadata,
            disposition=record.disposition,
        )
    if record.cures:
        return DefinitionResolution(
            taint_metadata=machine_authored_taint_metadata(TurnTaintState.empty()),
            disposition=record.disposition,
        )
    return UNRESOLVED_DEFINITION


@dataclass(frozen=True, slots=True)
class RetainedDefinition:
    """What a partial update kept, and what that costs the updating turn."""

    state: TurnTaintState
    """The updating turn's taint, with the retained content merged in."""

    uncured: bool
    """Whether the retained content's own record was absent, void, or uncured.

    Uncured retained content is content **no gate examined this turn**: the call
    named the fields it changed, and the stored row supplied the rest. So the
    verdict that admitted the call cannot vouch for the merged definition, and
    the write records it without curing.
    """


def merge_retained_definition(
    state: TurnTaintState,
    *,
    stored_record: object,
    retained_content: Mapping[str, object],
) -> RetainedDefinition:
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
    if resolve_definition_record(stored_record, retained_content).resolved:
        return RetainedDefinition(state=state, uncured=False)
    return RetainedDefinition(
        state=state.add_source(
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
        ),
        uncured=True,
    )


def definition_record_from_row(raw: object) -> DefinitionRecord | None:
    """Read the record stored on a definition row."""
    if isinstance(raw, str):
        try:
            raw = cast("object", json.loads(raw))
        except json.JSONDecodeError:
            return None
    return DefinitionRecord.from_dict(raw)
